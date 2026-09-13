"""Signed application updates with backup and rollback, and a safe uninstall.

An update may fix the factory; it must never quietly take a creator's game with
it. So an update is signed by an approved trust root, checked against the
projects on this machine before anything moves, backed up before it is applied,
and rolled back as a whole if any step fails. Moving a pinned engine or model is
a separate decision that the update alone cannot make, and uninstalling the
application leaves user projects in place unless someone says otherwise twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
UPDATE_OUTCOMES = ("applied", "rolled_back", "refused")
ISSUE_KINDS = ("engine_pin", "model_pin", "schema", "version")


class UpdateRefused(PermissionError):
    """Raised when an update or uninstall must not proceed."""


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _version(value: str) -> tuple[int, int, int]:
    text = str(value).strip()
    if not SEMVER.fullmatch(text):
        raise ValueError(f"Invalid semantic version: {value!r}")
    first, second, third = text.split(".")
    return int(first), int(second), int(third)


@dataclass(frozen=True)
class UpdateSignature:
    key_id: str
    algorithm: str = "hmac-sha256"
    value: str = ""


@dataclass(frozen=True)
class PinnedRequirement:
    name: str
    version: str


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    minimum_current_version: str
    schema_target: int
    engines: tuple[PinnedRequirement, ...]
    models: tuple[PinnedRequirement, ...]
    migrations: tuple[str, ...]
    notes: str
    signature: UpdateSignature

    @classmethod
    def create(
        cls,
        *,
        version: str,
        minimum_current_version: str,
        schema_target: int,
        key_id: str,
        engines: Sequence[PinnedRequirement] = (),
        models: Sequence[PinnedRequirement] = (),
        migrations: Sequence[str] = (),
        notes: str = "",
    ) -> "UpdateManifest":
        _version(version)
        _version(minimum_current_version)
        if int(schema_target) <= 0:
            raise ValueError("An update declares the schema version it targets")
        if not str(key_id).strip():
            raise ValueError("An update must name the trust root that signed it")
        for requirements in (engines, models):
            names = [item.name for item in requirements]
            if len(names) != len(set(names)):
                raise ValueError("Pinned requirements must be unique by name")
        return cls(
            str(version), str(minimum_current_version), int(schema_target),
            tuple(engines), tuple(models),
            tuple(str(value) for value in migrations), str(notes)[:2000],
            UpdateSignature(str(key_id).strip()),
        )

    @property
    def unsigned(self) -> str:
        payload = asdict(self)
        payload["signature"] = {
            "key_id": self.signature.key_id,
            "algorithm": self.signature.algorithm,
        }
        return _canonical(payload)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.unsigned.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectPin:
    """What one project on this machine currently depends on."""

    project_key: str
    engine: str = ""
    engine_version: str = ""
    model: str = ""
    model_version: str = ""


@dataclass(frozen=True)
class CompatibilityIssue:
    project_key: str
    kind: str
    detail: str


@dataclass(frozen=True)
class PinnedChange:
    kind: str
    name: str
    current: str
    proposed: str


@dataclass(frozen=True)
class UpdatePlan:
    manifest: UpdateManifest
    current_version: str
    current_schema: int
    signature_verified: bool
    issues: tuple[CompatibilityIssue, ...]
    pinned_changes: tuple[PinnedChange, ...]
    separate_plan: str

    @property
    def requires_separate_plan(self) -> bool:
        return bool(self.pinned_changes) and not self.separate_plan.strip()

    @property
    def blocking(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.signature_verified:
            reasons.append("the update is not signed by an approved trust root")
        for issue in self.issues:
            # A pin mismatch is described per project for the preview, but what
            # blocks is the single decision below - one approved plan covers all
            # the projects that share that pin.
            if issue.kind in {"engine_pin", "model_pin"}:
                continue
            reasons.append(f"{issue.project_key}: {issue.detail}")
        if self.requires_separate_plan:
            changed = ", ".join(
                f"{change.kind} {change.name} {change.current} -> {change.proposed}"
                for change in self.pinned_changes
            )
            reasons.append(
                "moving a pinned engine or model needs its own approved plan: " + changed
            )
        return tuple(reasons)

    @property
    def allowed(self) -> bool:
        return not self.blocking

    def preview(self) -> dict[str, Any]:
        return {
            "from_version": self.current_version,
            "to_version": self.manifest.version,
            "signature_verified": self.signature_verified,
            "schema": {"current": self.current_schema, "target": self.manifest.schema_target},
            "migrations": list(self.manifest.migrations),
            "pinned_changes": [asdict(change) for change in self.pinned_changes],
            "separate_plan": self.separate_plan,
            "issues": [asdict(issue) for issue in self.issues],
            "blocking": list(self.blocking),
            "allowed": self.allowed,
        }


@dataclass(frozen=True)
class UpdateReceipt:
    outcome: str
    version_before: str
    version_after: str
    backup: str
    completed_steps: tuple[str, ...]
    failed_step: str
    reason: str
    actor: str
    recorded_at: str

    @property
    def record(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "backup": self.backup,
            "completed_steps": list(self.completed_steps),
            "failed_step": self.failed_step,
            "reason": self.reason,
            "actor": self.actor,
            "recorded_at": self.recorded_at,
        }


class ApplicationUpdater:
    """Verifies, previews, applies and rolls back one application update."""

    def __init__(
        self,
        *,
        current_version: str,
        current_schema: int,
        trust_material: Mapping[str, bytes] | None = None,
    ):
        _version(current_version)
        self.current_version = str(current_version)
        self.current_schema = int(current_schema)
        self._trust = dict(trust_material or {})

    @staticmethod
    def sign(manifest: UpdateManifest, secret: bytes) -> UpdateManifest:
        value = hmac.new(
            secret, manifest.unsigned.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return replace(manifest, signature=replace(manifest.signature, value=value))

    def verify(self, manifest: UpdateManifest) -> bool:
        secret = self._trust.get(manifest.signature.key_id)
        if not secret or manifest.signature.algorithm != "hmac-sha256":
            return False
        expected = hmac.new(
            secret, manifest.unsigned.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, manifest.signature.value or "")

    def plan(
        self,
        manifest: UpdateManifest,
        *,
        projects: Sequence[ProjectPin] = (),
        separate_plan: str = "",
    ) -> UpdatePlan:
        issues: list[CompatibilityIssue] = []
        if _version(manifest.version) <= _version(self.current_version):
            issues.append(CompatibilityIssue(
                "application", "version",
                f"update {manifest.version} is not newer than the installed "
                f"{self.current_version}",
            ))
        if _version(self.current_version) < _version(manifest.minimum_current_version):
            issues.append(CompatibilityIssue(
                "application", "version",
                f"this update requires {manifest.minimum_current_version} or newer; "
                f"{self.current_version} is installed",
            ))
        if manifest.schema_target < self.current_schema:
            issues.append(CompatibilityIssue(
                "application", "schema",
                f"update targets schema {manifest.schema_target} but schema "
                f"{self.current_schema} is already applied",
            ))
        engines = {item.name: item.version for item in manifest.engines}
        models = {item.name: item.version for item in manifest.models}
        changes: dict[tuple[str, str], PinnedChange] = {}
        for project in projects:
            for kind, pinned, name, version in (
                ("engine_pin", engines, project.engine, project.engine_version),
                ("model_pin", models, project.model, project.model_version),
            ):
                if not name or name not in pinned:
                    continue
                proposed = pinned[name]
                if proposed == version:
                    continue
                changes[(kind, name)] = PinnedChange(kind, name, version, proposed)
                issues.append(CompatibilityIssue(
                    project.project_key, kind,
                    f"{name} is pinned at {version}; this update would move it to "
                    f"{proposed}",
                ))
        return UpdatePlan(
            manifest, self.current_version, self.current_schema,
            self.verify(manifest), tuple(issues),
            tuple(changes[key] for key in sorted(changes)), str(separate_plan).strip(),
        )

    def apply(
        self,
        plan: UpdatePlan,
        *,
        actor: str,
        backup: Callable[[], str],
        restore: Callable[[str], None],
        steps: Sequence[tuple[str, Callable[[], None]]],
    ) -> UpdateReceipt:
        """Back up, run every step, and roll the whole update back on any failure."""
        who = str(actor).strip()
        if not who:
            raise ValueError("An update records the person who approved it")
        if not plan.allowed:
            return UpdateReceipt(
                "refused", self.current_version, self.current_version, "", (), "",
                "; ".join(plan.blocking), who, _stamp(),
            )
        if not steps:
            raise ValueError("An update must declare the steps it performs")
        backup_path = str(backup())
        if not backup_path:
            raise UpdateRefused("An update never runs without a recorded backup")
        completed: list[str] = []
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # Any failure rolls the whole update back.
                restore(backup_path)
                return UpdateReceipt(
                    "rolled_back", self.current_version, self.current_version,
                    backup_path, tuple(completed), str(name),
                    f"{type(exc).__name__}: {exc}", who, _stamp(),
                )
            completed.append(str(name))
        return UpdateReceipt(
            "applied", self.current_version, plan.manifest.version, backup_path,
            tuple(completed), "", "update applied", who, _stamp(),
        )


# ------------------------------------------------------------------ uninstall

@dataclass(frozen=True)
class UninstallPlan:
    workspace: str
    application_paths: tuple[str, ...]
    project_paths: tuple[str, ...]
    removes_projects: bool

    @property
    def preserved(self) -> tuple[str, ...]:
        return () if self.removes_projects else self.project_paths

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical({
            "workspace": self.workspace,
            "application_paths": list(self.application_paths),
            "project_paths": list(self.project_paths),
            "removes_projects": self.removes_projects,
        }).encode("utf-8")).hexdigest()

    def preview(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "removes": list(self.application_paths) + (
                list(self.project_paths) if self.removes_projects else []
            ),
            "preserves": list(self.preserved),
            "removes_projects": self.removes_projects,
            "plan_digest": self.digest,
            "note": (
                "Your games are removed by this plan."
                if self.removes_projects
                else "Your games are kept. Uninstalling the application does not "
                     "delete them."
            ),
        }


APPLICATION_DIRECTORIES = (".agent-factory",)


def uninstall_plan(
    workspace: Path,
    *,
    project_paths: Sequence[Path] = (),
    remove_projects: bool = False,
) -> UninstallPlan:
    root = Path(workspace)
    application = tuple(
        str(root / name) for name in APPLICATION_DIRECTORIES if (root / name).exists()
    )
    projects = tuple(str(Path(path)) for path in project_paths)
    overlapping = [
        path for path in projects
        if any(Path(path).resolve().is_relative_to(Path(item).resolve()) for item in application)
    ]
    if overlapping:
        raise UpdateRefused(
            "A project inside the application's own state cannot be separated "
            "safely: " + ", ".join(sorted(overlapping))
        )
    return UninstallPlan(str(root), application, projects, bool(remove_projects))


def uninstall(
    plan: UninstallPlan,
    *,
    actor: str,
    confirm_project_removal: bool = False,
    remove: Callable[[str], None],
) -> dict[str, Any]:
    """Remove the application. User projects go only on a second, explicit yes."""
    who = str(actor).strip()
    if not who:
        raise ValueError("An uninstall records who asked for it")
    if plan.removes_projects and not confirm_project_removal:
        raise UpdateRefused(
            "Removing your games needs a separate confirmation; nothing was removed"
        )
    removed: list[str] = []
    for path in plan.application_paths:
        remove(path)
        removed.append(path)
    if plan.removes_projects and confirm_project_removal:
        for path in plan.project_paths:
            remove(path)
            removed.append(path)
    return {
        "removed": removed,
        "preserved": list(plan.preserved),
        "actor": who,
        "recorded_at": _stamp(),
    }


def load_manifest(payload: Mapping[str, Any]) -> UpdateManifest:
    """Read an update manifest, keeping its recorded signature for verification."""
    if not isinstance(payload, Mapping):
        raise ValueError("An update manifest must be a JSON object")
    signature = payload.get("signature") or {}
    if not isinstance(signature, Mapping):
        raise ValueError("An update manifest carries a signature object")
    manifest = UpdateManifest.create(
        version=str(payload.get("version", "")),
        minimum_current_version=str(payload.get("minimum_current_version", "")),
        schema_target=int(payload.get("schema_target", 0)),
        key_id=str(signature.get("key_id", "")),
        engines=tuple(
            PinnedRequirement(str(item["name"]), str(item["version"]))
            for item in payload.get("engines", ())
        ),
        models=tuple(
            PinnedRequirement(str(item["name"]), str(item["version"]))
            for item in payload.get("models", ())
        ),
        migrations=tuple(str(value) for value in payload.get("migrations", ())),
        notes=str(payload.get("notes", "")),
    )
    return replace(
        manifest,
        signature=replace(
            manifest.signature,
            algorithm=str(signature.get("algorithm", "hmac-sha256")),
            value=str(signature.get("value", "")),
        ),
    )


def parse_project_pin(value: str) -> ProjectPin:
    """`key:engine:version` or `key:engine:version:model:model_version`."""
    parts = [part.strip() for part in str(value).split(":")]
    if len(parts) not in {3, 5} or not parts[0]:
        raise ValueError(
            "A project pin is key:engine:version or key:engine:version:model:version"
        )
    if len(parts) == 3:
        return ProjectPin(parts[0], engine=parts[1], engine_version=parts[2])
    return ProjectPin(
        parts[0], engine=parts[1], engine_version=parts[2],
        model=parts[3], model_version=parts[4],
    )
