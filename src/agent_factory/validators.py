"""Shell-free, allowlisted project-pack validator execution."""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .sandbox import (
    SandboxManager,
    SandboxPolicy,
    SandboxUnavailableError,
    _canonical_command,
)
from .storage import SQLiteStorage, _sha256_snapshot


VALIDATOR_CATEGORIES = ("test", "lint", "type_check", "build", "security_scan")


@dataclass(frozen=True)
class ValidatorPack:
    pack_id: str
    commands: Mapping[str, tuple[str, ...]]

    @classmethod
    def create(cls, pack_id: str, commands: Mapping[str, Sequence[str]]) -> "ValidatorPack":
        if not pack_id.strip() or set(commands) != set(VALIDATOR_CATEGORIES):
            raise ValueError("Validator pack requires an ID and all five command categories")
        normalized: dict[str, tuple[str, ...]] = {}
        for category in VALIDATOR_CATEGORIES:
            vector = commands[category]
            if isinstance(vector, (str, bytes)):
                raise ValueError("Validator commands must be argument vectors, never shell strings")
            values = tuple(str(value) for value in vector)
            if not values or any(not value or "\x00" in value for value in values):
                raise ValueError(f"Validator command {category} is not a fixed vector")
            normalized[category] = values
        return cls(pack_id.strip(), normalized)

    @property
    def digest(self) -> str:
        value = json.dumps(
            {"pack_id": self.pack_id, "commands": self.commands},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_validator_pack(path: Path, pack_id: str) -> ValidatorPack:
    payload = json.loads(path.read_text(encoding="utf-8"))
    packs = payload.get("packs", []) if isinstance(payload, dict) else []
    for value in packs:
        if isinstance(value, dict) and value.get("id") == pack_id:
            commands = value.get("commands")
            if not isinstance(commands, dict):
                raise ValueError(f"Validator pack {pack_id} has no command map")
            return ValidatorPack.create(pack_id, commands)
    raise KeyError(f"Unknown validator pack: {pack_id}")


@dataclass(frozen=True)
class ValidatorResult:
    id: int
    category: str
    status: str
    exit_code: int | None
    command_digest: str
    evidence_digest: str
    criterion_mappings: tuple[str, ...]


@dataclass(frozen=True)
class ValidationSuiteResult:
    candidate_digest: str
    passed: bool
    results: tuple[ValidatorResult, ...]


class ValidatorRunner:
    def __init__(
        self,
        storage: SQLiteStorage,
        workspace: Path,
        sandbox: SandboxManager,
    ):
        self.storage = storage
        self.workspace = workspace.resolve()
        self.sandbox = sandbox

    def run(
        self,
        *,
        assignment_id: int,
        fencing_token: int,
        attempt_id: int,
        worktree_id: int,
        candidate_digest: str,
        pack: ValidatorPack,
        criterion_mappings: Mapping[str, Sequence[str]],
        max_seconds: int = 120,
        max_output_chars: int = 100_000,
    ) -> ValidationSuiteResult:
        _sha256_snapshot(candidate_digest, "candidate digest")
        if set(criterion_mappings) != set(VALIDATOR_CATEGORIES):
            raise ValueError("Every validator category requires criterion mappings")
        worktree = self.storage.managed_worktree(worktree_id)
        attempt = self.storage.db.execute(
            "SELECT assignment_id,status FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if (
            int(worktree["assignment_id"]) != assignment_id
            or int(worktree["attempt_id"] or 0) != attempt_id
            or int(worktree["fencing_token"] or 0) != fencing_token
            or str(worktree["status"]) not in {"ready", "dirty"}
            or not attempt
            or int(attempt["assignment_id"]) != assignment_id
        ):
            raise PermissionError("Validator worktree is not owned by the live attempt")
        task = self.storage.get_task(int(worktree["task_id"]))
        accepted = set(task.acceptance_criteria)
        normalized_mappings: dict[str, tuple[str, ...]] = {}
        for category, values in criterion_mappings.items():
            mapped = tuple(sorted({str(value) for value in values if str(value)}))
            if not mapped or not set(mapped) <= accepted:
                raise ValueError(f"Validator {category} must map to declared acceptance criteria")
            normalized_mappings[category] = mapped
        worktree_path = Path(str(worktree["path"])).resolve()
        scratch = (
            self.workspace / ".agent-factory" / "sandbox-temp" / "validator"
            / str(attempt_id) / candidate_digest[:16]
        )
        recorded: list[ValidatorResult] = []
        for category in VALIDATOR_CATEGORIES:
            # Without a declared scratch path the sandbox points TMP at the candidate
            # worktree, so a tool's cache write would be read back as the validator
            # modifying the candidate it is meant to judge.
            policy = SandboxPolicy.create(
                self.workspace, worktree_path, [scratch / category],
                max_seconds=max_seconds, max_output_chars=max_output_chars,
            )
            command = pack.commands[category]
            try:
                result = self.sandbox.execute(
                    assignment_id, fencing_token, policy, command
                )
            except SandboxUnavailableError as error:
                # A boundary reject (backend unavailable, or this command resolves
                # outside the configured tool roots) must not lose evidence for the
                # remaining categories. Record the honest block and keep going, the
                # same way a real command failure is recorded rather than raised.
                recorded.append(self._record_blocked(
                    task_id=task.id, assignment_id=assignment_id,
                    attempt_id=attempt_id, worktree_id=worktree_id,
                    candidate_digest=candidate_digest, pack=pack, category=category,
                    command=command, policy=policy,
                    criterion_mappings=normalized_mappings[category], reason=str(error),
                ))
                continue
            effective_status = "failed" if result.changed_files else result.status
            effective_stderr = result.stderr
            if result.changed_files:
                effective_stderr = (
                    effective_stderr + "\nValidator modified candidate files: "
                    + ", ".join(result.changed_files)
                ).strip()
            environment = {
                "platform": platform.system().casefold(),
                "python": platform.python_version(),
                "backend": result.backend,
                "cwd_scope": "candidate_worktree",
                "network": policy.network,
                "max_seconds": policy.max_seconds,
                "max_output_chars": policy.max_output_chars,
            }
            with self.storage.db:
                cursor = self.storage.db.execute(
                    """INSERT INTO validator_results(
                           identity,task_id,assignment_id,attempt_id,worktree_id,
                           candidate_digest,pack_id,pack_digest,category,command_json,
                           command_digest,status,exit_code,stdout,stderr,
                           environment_json,criterion_mappings_json,
                           evidence_directory,evidence_digest
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.storage._identity("validator-result"), task.id,
                        assignment_id, attempt_id, worktree_id, candidate_digest,
                        pack.pack_id, pack.digest, category,
                        json.dumps(command, separators=(",", ":")),
                        result.command_digest, effective_status, result.returncode,
                        result.stdout, effective_stderr,
                        json.dumps(environment, sort_keys=True),
                        json.dumps(normalized_mappings[category], separators=(",", ":")),
                        str(Path(result.evidence_directory).relative_to(self.workspace)),
                        result.evidence_digest,
                    ),
                )
                result_id = int(cursor.lastrowid)
                self.storage._event(
                    f"validator.{effective_status}", "validator_result", result_id,
                    {
                        "task_id": task.id, "attempt_id": attempt_id,
                        "worktree_id": worktree_id, "candidate_digest": candidate_digest,
                        "category": category, "command_digest": result.command_digest,
                        "criterion_mappings": list(normalized_mappings[category]),
                    },
                )
            recorded.append(ValidatorResult(
                result_id, category, effective_status, result.returncode,
                result.command_digest, result.evidence_digest,
                normalized_mappings[category],
            ))
        return ValidationSuiteResult(
            candidate_digest,
            all(result.status == "succeeded" for result in recorded),
            tuple(recorded),
        )

    def _record_blocked(
        self,
        *,
        task_id: int,
        assignment_id: int,
        attempt_id: int,
        worktree_id: int,
        candidate_digest: str,
        pack: ValidatorPack,
        category: str,
        command: tuple[str, ...],
        policy: SandboxPolicy,
        criterion_mappings: tuple[str, ...],
        reason: str,
    ) -> ValidatorResult:
        """Record a category the sandbox refused to run, never a fabricated pass.

        The backend rejected this command before any process started (unavailable
        boundary, or the command resolves outside the configured tool roots), so
        there is no execution to report. The category is marked ``failed`` because
        the validator produced no passing verdict; the stderr makes clear this was a
        pre-execution block, not an observed tool failure.
        """
        command_json = json.dumps(list(command), separators=(",", ":"))
        # Digest the command the way the sandbox does for an executed one, so a
        # blocked row and a later executed row of the same command compare equal.
        try:
            canonical = list(_canonical_command(command))
        except (ValueError, OSError):
            canonical = list(command)
        command_digest = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
            .encode("utf-8")
        ).hexdigest()
        stderr = f"sandbox blocked this category before execution: {reason}"
        evidence_digest = hashlib.sha256(stderr.encode("utf-8")).hexdigest()
        environment = {
            "platform": platform.system().casefold(),
            "python": platform.python_version(),
            "backend": self.sandbox.backend.name,
            "cwd_scope": "candidate_worktree",
            "network": policy.network,
            "max_seconds": policy.max_seconds,
            "max_output_chars": policy.max_output_chars,
        }
        with self.storage.db:
            cursor = self.storage.db.execute(
                """INSERT INTO validator_results(
                       identity,task_id,assignment_id,attempt_id,worktree_id,
                       candidate_digest,pack_id,pack_digest,category,command_json,
                       command_digest,status,exit_code,stdout,stderr,
                       environment_json,criterion_mappings_json,
                       evidence_directory,evidence_digest
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.storage._identity("validator-result"), task_id,
                    assignment_id, attempt_id, worktree_id, candidate_digest,
                    pack.pack_id, pack.digest, category, command_json,
                    command_digest, "failed", None, "", stderr,
                    json.dumps(environment, sort_keys=True),
                    json.dumps(list(criterion_mappings), separators=(",", ":")),
                    "", evidence_digest,
                ),
            )
            result_id = int(cursor.lastrowid)
            self.storage._event(
                "validator.blocked", "validator_result", result_id,
                {
                    "task_id": task_id, "attempt_id": attempt_id,
                    "worktree_id": worktree_id, "candidate_digest": candidate_digest,
                    "category": category, "command_digest": command_digest,
                    "criterion_mappings": list(criterion_mappings),
                    "reason": reason,
                },
            )
        return ValidatorResult(
            result_id, category, "failed", None, command_digest, evidence_digest,
            criterion_mappings,
        )
