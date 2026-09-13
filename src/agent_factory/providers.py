from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import (
    Agent,
    ExecutionApproval,
    ExecutionAuthorizationMode,
    ExecutionLocation,
    ProviderCapabilities,
    ProviderExecutionAuthorization,
    ProviderResult,
    WorkItem,
)

SENSITIVE_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "AUTH")
PASSING_VERDICTS = ("COMPLETE", "PASS", "ALIGNED", "CONDITIONALLY_ALIGNED")


class BoundedCapture:
    """Drain both text streams concurrently while retaining a combined hard limit."""

    def __init__(self, proc: subprocess.Popen[str], max_chars: int):
        self.proc = proc
        self.max_chars = max_chars
        self.overflow = threading.Event()
        self._lock = threading.Lock()
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._retained_chars = 0
        self._observed_chars = 0
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, stream in (("stdout", self.proc.stdout), ("stderr", self.proc.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._drain,
                args=(name, stream),
                daemon=True,
                name=f"provider-{name}-capture",
            )
            thread.start()
            self._threads.append(thread)

    def _drain(self, name: str, stream: Any) -> None:
        destination = self._stdout if name == "stdout" else self._stderr
        with suppress(OSError, ValueError):
            while chunk := stream.read(4096):
                with self._lock:
                    self._observed_chars += len(chunk)
                    remaining = max(0, self.max_chars - self._retained_chars)
                    if remaining:
                        retained = chunk[:remaining]
                        destination.append(retained)
                        self._retained_chars += len(retained)
                    if self._observed_chars > self.max_chars:
                        self.overflow.set()

    def join(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stdout": "".join(self._stdout),
                "stderr": "".join(self._stderr),
                "retained_chars": self._retained_chars,
                "observed_chars": self._observed_chars,
            }


class ProcessSupervisor:
    """Start a provider in an isolated process group and terminate its full tree."""

    def __init__(self, *, windows: bool | None = None):
        self.windows = os.name == "nt" if windows is None else windows

    def spawn(self, command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        if self.windows:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, shell=False, **kwargs)

    def terminate_tree(self, proc: subprocess.Popen[str]) -> dict[str, Any]:
        pid = int(proc.pid)
        if pid <= 0:
            return {"tree_terminated": False, "cleanup_error": "invalid process id"}
        if proc.poll() is not None:
            return {"tree_terminated": True, "already_exited": True}
        try:
            if self.windows:
                cleanup = subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                verified = cleanup.returncode == 0
                if not verified and proc.poll() is None:
                    proc.kill()
                return {"tree_terminated": verified, "cleanup_returncode": cleanup.returncode}
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return {"tree_terminated": True}
        except (OSError, subprocess.SubprocessError) as exc:
            if proc.poll() is None:
                with suppress(OSError):
                    proc.kill()
            return {"tree_terminated": False, "cleanup_error": type(exc).__name__}

    def cancel_tree(
        self, proc: subprocess.Popen[str], *, graceful_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Request graceful group termination, then enforce bounded tree cleanup."""

        if proc.poll() is not None:
            return {"tree_terminated": True, "graceful": True}
        graceful = False
        try:
            if self.windows:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=max(0.1, graceful_seconds))
            graceful = True
        except (OSError, subprocess.SubprocessError):
            graceful = False
        if graceful:
            return {**self.terminate_tree(proc), "tree_terminated": True, "graceful": True}
        return {**self.terminate_tree(proc), "graceful": False}


class Provider(ABC):
    name: str
    capabilities = ProviderCapabilities()

    @property
    def autonomous_local_eligible(self) -> bool:
        return self.capabilities.autonomous_local_eligible

    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    @abstractmethod
    def execute(
        self,
        agent: Agent,
        item: WorkItem,
        context: dict[str, Any],
        approval: ExecutionApproval | ProviderExecutionAuthorization | None = None,
    ) -> ProviderResult: ...


class CLIProvider(Provider):
    def __init__(
        self,
        name: str,
        executable: str,
        args: list[str],
        *,
        model_namespace: str = "",
        model_ids: list[str] | None = None,
        executable_candidates: list[str] | None = None,
        version_args: list[str] | None = None,
        prompt_transport: str = "stdin",
        prompt_file_args: list[str] | None = None,
        allow_execution: bool = False,
        max_timeout: int = 180,
        max_output_chars: int = 100_000,
        max_prompt_chars: int = 50_000,
        allowed_roles: list[str] | None = None,
        allowed_sensitive_env: list[str] | None = None,
        protected_paths: list[str] | None = None,
        safety_rules: list[str] | None = None,
        workspace: Path | None = None,
        supervisor: ProcessSupervisor | None = None,
        capabilities: ProviderCapabilities | None = None,
    ):
        self.name = name
        self.executable = executable
        self.executable_candidates = tuple(executable_candidates or [])
        self.args = tuple(args)
        self.model_namespace = model_namespace
        self.model_ids = tuple(model_ids or [])
        self.version_args = tuple(version_args or ["--version"])
        self.prompt_transport = prompt_transport
        self.prompt_file_args = tuple(prompt_file_args or ["--message-file"])
        self.allow_execution = allow_execution
        self.max_timeout = max(1, int(max_timeout))
        self.max_output_chars = max(1, int(max_output_chars))
        self.max_prompt_chars = max(1, int(max_prompt_chars))
        self.allowed_roles = frozenset(allowed_roles or [])
        self.allowed_sensitive_env = frozenset(allowed_sensitive_env or [])
        self.protected_paths = tuple(protected_paths or [])
        self.safety_rules = tuple(safety_rules or [])
        self.workspace = (workspace or Path.cwd()).resolve()
        self.supervisor = supervisor or ProcessSupervisor()
        self.capabilities = capabilities or ProviderCapabilities()

    def model_request(self, requested: str) -> tuple[list[str], str | None]:
        """Bind a canonical model ID to one reviewed argument, never shell text."""
        if not self.model_ids:
            # The existing Gemini agent uses this explicit alias for the CLI's
            # own default. It is not evidence of a particular effective model.
            if self.name == 'gemini' and requested == 'google:gemini':
                return list(self.args), None
            if requested or "{model}" in self.args:
                raise ValueError("Selected model has no qualified CLI binding for this provider")
            # Legacy model-free tools may run, but cannot attest a model identity.
            return list(self.args), None
        if (
            not isinstance(self.model_namespace, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", self.model_namespace)
            or self.args.count("{model}") != 1
            or any(not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value
            ) for value in self.model_ids)
        ):
            raise ValueError("Invalid qualified model configuration")
        allowed = {f"{self.model_namespace}:{value}": value for value in self.model_ids}
        if not isinstance(requested, str) or requested not in allowed:
            raise ValueError("Selected model is unknown or unsupported; choose a configured model")
        return [allowed[requested] if arg == "{model}" else arg for arg in self.args], requested

    def _executable_paths(self) -> list[str]:
        """Resolve only configured launchers, preferring stable native binaries."""

        resolved: list[str] = []
        seen: set[str] = set()
        for raw in (*self.executable_candidates, self.executable):
            expanded = os.path.expandvars(os.path.expanduser(raw))
            candidate = Path(expanded)
            path: str | None
            if candidate.is_absolute():
                path = str(candidate) if candidate.is_file() else None
            elif candidate.parent != Path("."):
                workspace_candidate = (self.workspace / candidate).resolve()
                path = str(workspace_candidate) if workspace_candidate.is_file() else None
            else:
                path = shutil.which(expanded)
            if not path:
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                resolved.append(path)
        if (self.name in {'codex','gemini','ollama'} and self.executable == self.name
                and (not resolved or all(Path(p).suffix.lower() in {'.cmd','.bat','.ps1'} for p in resolved))):
            from .ai_setup import launcher
            selected = launcher(self.name, self.workspace)
            if selected and selected[0] not in resolved:
                resolved.insert(0, selected[0])
        return resolved

    def _executable_path(self) -> str | None:
        paths = self._executable_paths()
        return paths[0] if paths else None

    def _launch_command(self, path: str, arguments: list[str]) -> list[str]:
        command = [path]
        if self.name in {'codex','gemini','ollama'} and self.executable == self.name:
            from .ai_setup import launcher
            selected = launcher(self.name, self.workspace)
            if selected and os.path.normcase(selected[0]) == os.path.normcase(path):
                command = selected
        return [*command, *arguments]

    def health(self) -> dict[str, Any]:
        paths = self._executable_paths()
        if not paths:
            return {
                "provider": self.name,
                "healthy": False,
                "capabilities": self.capabilities.to_dict(),
                "error": "allowlisted executable not found",
            }
        failures: list[str] = []
        for path in paths:
            try:
                proc = subprocess.run(
                    self._launch_command(path, list(self.version_args)),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    check=False,
                )
                output = (proc.stdout or proc.stderr).strip()
                if proc.returncode == 0:
                    return {
                        "provider": self.name,
                        "healthy": True,
                        "path": path,
                        "version": output[:1000],
                        "execution_enabled": self.allow_execution,
                        "capabilities": self.capabilities.to_dict(),
                        "error": None,
                    }
                failures.append(f"{Path(path).name}: exit {proc.returncode}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{Path(path).name}: {exc}")
        return {
            "provider": self.name,
            "healthy": False,
            "path": paths[0],
            "execution_enabled": self.allow_execution,
            "capabilities": self.capabilities.to_dict(),
            "error": "; ".join(failures)[:1000],
        }

    def _approval_error(
        self,
        agent: Agent,
        item: WorkItem,
        approval: ExecutionApproval | ProviderExecutionAuthorization | None,
    ) -> str | None:
        if not self.allow_execution:
            return "provider execution disabled by configuration"
        if self.allowed_roles and agent.role not in self.allowed_roles:
            return f"agent role {agent.role!r} is not allowlisted for provider {self.name}"
        compatibility_error = self.capabilities.role_model_error(agent.role, agent.model)
        if compatibility_error:
            return compatibility_error
        if approval is None:
            return "operator provider-execution approval required"
        expected = (self.name, agent.id, item.id)
        actual = (approval.provider, approval.agent_id, approval.task_id)
        if expected != actual:
            return f"approval scope mismatch: expected provider/agent/task {expected}"
        if isinstance(approval, ProviderExecutionAuthorization):
            if not self.capabilities.autonomous_local_eligible:
                return "autonomous provider authority requires explicitly LOCAL text generation"
            expected_operation = {
                ExecutionAuthorizationMode.AUTONOMOUS_LOCAL: "LOCAL_INFERENCE",
                ExecutionAuthorizationMode.BOUNDED_LOCAL_PLANNING: "PLANNING_INFERENCE",
            }.get(approval.mode)
            if expected_operation is None:
                return "autonomous provider authority mode is invalid"
            if approval.operation != expected_operation:
                return "autonomous provider authority operation mismatch"
            if "execute_provider" not in approval.permissions:
                return "autonomous provider authority lacks execute_provider permission"
            digest = approval.evidence_digest
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                return "autonomous provider authority evidence digest is invalid"
            if approval.decision_id < 1 or approval.authorization_id < 1:
                return "autonomous provider authority identity is invalid"
            if not str(approval.authorized_by).strip():
                return "autonomous provider authority issuer is required"
            return None
        if not str(approval.approved_by).strip():
            return "approval issuer is required"
        return None

    @staticmethod
    def _approval_metadata(
        approval: ExecutionApproval | ProviderExecutionAuthorization | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "gate_id": approval.gate_id if approval else None,
        }
        if isinstance(approval, ProviderExecutionAuthorization):
            metadata.update(
                {
                    "authorization_mode": approval.mode.value,
                    "authorization_id": approval.authorization_id,
                    "authorization_decision_id": approval.decision_id,
                    "authorization_evidence_digest": approval.evidence_digest,
                    "mission_id": approval.mission_id,
                    "backlog_revision_id": approval.backlog_revision_id,
                    "execution_epoch_id": approval.execution_epoch_id,
                    "planning_request_id": approval.planning_request_id,
                }
            )
        return metadata

    def _safe_environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key in self.allowed_sensitive_env
            or not any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS)
        } | {"NO_COLOR": "1", "TERM": "dumb"}

    def _prompt(self, agent: Agent, item: WorkItem, context: dict[str, Any]) -> str:
        protected = ", ".join(self.protected_paths) if self.protected_paths else "none configured"
        rules = [
            "Work only on the requested task and return a reviewable artifact.",
            "Do not reveal credentials, merge changes, close work items, or contact external parties.",
            *self.safety_rules,
        ]
        prompt = (
            f"Role: {agent.role}\n"
            f"Instructions: {agent.instructions}\n"
            f"Task: {item.title}\n{item.description}\n"
            f"Acceptance criteria: {json.dumps(item.acceptance_criteria, ensure_ascii=False)}\n"
            f"Context: {json.dumps(context, ensure_ascii=False, sort_keys=True)}\n"
            f"Protected paths (read-only): {protected}\n\n"
            "Execution policy:\n- "
            + "\n- ".join(rules)
        )
        if len(prompt) > self.max_prompt_chars:
            raise ValueError(f"provider prompt exceeds {self.max_prompt_chars:,} character policy limit")
        return prompt

    @staticmethod
    def _launcher_failure(path: str, exc: OSError) -> dict[str, Any]:
        """Return useful diagnostics without persisting paths or exception messages."""

        return {
            "launcher": Path(path).name,
            "error_type": type(exc).__name__,
            "errno": exc.errno,
            "winerror": getattr(exc, "winerror", None),
        }

    @staticmethod
    def _write_stdin(proc: subprocess.Popen[str], content: str) -> None:
        if proc.stdin is None:
            return
        with suppress(BrokenPipeError, OSError, ValueError):
            proc.stdin.write(content)
            proc.stdin.flush()
        with suppress(OSError, ValueError):
            proc.stdin.close()

    def execute(
        self,
        agent: Agent,
        item: WorkItem,
        context: dict[str, Any],
        approval: ExecutionApproval | ProviderExecutionAuthorization | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ProviderResult:
        if error := self._approval_error(agent, item, approval):
            return ProviderResult(False, provider=self.name, error=error, metadata={"blocked": True})
        try:
            model_args, effective_model = self.model_request(agent.model)
        except ValueError as exc:
            return ProviderResult(False, provider=self.name, error=str(exc), metadata={
                "blocked": True, "model_binding_error": True, "effective_model": None,
            })
        model_metadata = {
            "requested_model": agent.model or None,
            "effective_model": effective_model,
            "model_identity_source": "qualified_request" if effective_model else "unknown",
        }
        paths = self._executable_paths()
        if not paths:
            return ProviderResult(False, provider=self.name, error="allowlisted executable not found",
                                  metadata=model_metadata)
        try:
            prompt = self._prompt(agent, item, context)
        except ValueError as exc:
            return ProviderResult(False, provider=self.name, error=str(exc),
                                  metadata={**model_metadata, "blocked": True})

        command_suffix = list(model_args)
        stdin = None
        prompt_file: Path | None = None
        if self.prompt_transport == "stdin":
            stdin = prompt
        elif self.prompt_transport == "argument":
            command_suffix.append(prompt)
        elif self.prompt_transport == "file":
            prompt_dir = self.workspace / ".agent-factory" / "provider-prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".txt", dir=prompt_dir, delete=False
            ) as handle:
                handle.write(prompt)
                prompt_file = Path(handle.name)
            command_suffix.extend([*self.prompt_file_args, str(prompt_file)])
        else:
            return ProviderResult(
                False,
                provider=self.name,
                error=f"unsupported prompt transport: {self.prompt_transport}",
            )

        timeout = min(max(1, item.budget.max_seconds), self.max_timeout)
        started = time.monotonic()
        proc: subprocess.Popen[str] | None = None
        path: str | None = None
        launcher_failures: list[dict[str, Any]] = []
        try:
            provider_environment = self._safe_environment()
            if self.name == 'gemini':
                from .ai_setup import connected_environment
                try:
                    provider_environment.update(connected_environment(self.workspace, approval.approved_by))
                except Exception:
                    return ProviderResult(False, provider=self.name,
                        error='Gemini credential unavailable; reconnect in AI access',
                        metadata={**model_metadata, 'blocked': True})
                command_suffix.extend(['--skip-trust', '--extensions', 'none',
                    '--allowed-mcp-server-names=__lokvetia_no_server__', '--admin-policy',
                    str(Path(__file__).parent / 'defaults/gemini-no-tools.toml')])
            for candidate in paths:
                try:
                    proc = self.supervisor.spawn(
                        self._launch_command(candidate, command_suffix),
                        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=self.workspace,
                        env=provider_environment,
                    )
                except OSError as exc:
                    launcher_failures.append(self._launcher_failure(candidate, exc))
                    continue
                path = candidate
                break

            if proc is None or path is None:
                return ProviderResult(
                    False,
                    provider=self.name,
                    error="all allowlisted launchers failed before process start",
                    metadata={
                        **self._approval_metadata(approval),
                        **model_metadata,
                        "launcher_failures": launcher_failures,
                    },
                )

            capture = BoundedCapture(proc, self.max_output_chars)
            capture.start()
            writer: threading.Thread | None = None
            if stdin is not None:
                writer = threading.Thread(
                    target=self._write_stdin,
                    args=(proc, stdin),
                    daemon=True,
                    name="provider-stdin-writer",
                )
                writer.start()

            deadline = started + timeout
            cleanup: dict[str, Any] = {}
            timed_out = False
            output_limit_exceeded = False
            cancelled = False
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    cleanup = self.supervisor.cancel_tree(proc)
                    break
                if capture.overflow.is_set():
                    output_limit_exceeded = True
                    cleanup = self.supervisor.terminate_tree(proc)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    cleanup = self.supervisor.terminate_tree(proc)
                    break
                try:
                    proc.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue

            if timed_out or output_limit_exceeded or cancelled:
                with suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=5)
            capture_complete = capture.join(5)
            if capture.overflow.is_set() and not output_limit_exceeded:
                output_limit_exceeded = True
                cleanup = self.supervisor.terminate_tree(proc)
            if writer is not None:
                writer.join(0.2)
            if capture_complete:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        with suppress(OSError, ValueError):
                            stream.close()

            captured = capture.snapshot()
            stdout = captured["stdout"]
            stderr = captured["stderr"]
            # A provider may echo its environment in an error or tool response.
            if self.name == 'gemini' and provider_environment.get('GEMINI_API_KEY'):
                secret = provider_environment['GEMINI_API_KEY']
                stdout = stdout.replace(secret, '[REDACTED]')
                stderr = stderr.replace(secret, '[REDACTED]')
            elapsed = round(time.monotonic() - started, 3)
            metadata = {
                **self._approval_metadata(approval),
                **model_metadata,
                "executable": Path(path).name,
                "args": model_args,
                "timeout_seconds": timeout,
                "elapsed_seconds": elapsed,
                "returncode": proc.returncode,
                "output_truncated": output_limit_exceeded,
                "output_limit_chars": self.max_output_chars,
                "retained_output_chars": captured["retained_chars"],
                "observed_output_chars": captured["observed_chars"],
                "stdout_retained_chars": len(stdout),
                "stderr_retained_chars": len(stderr),
                "stderr_ansi_sequences": len(re.findall(r"\x1b\[[0-?]*[ -/]*[@-~]", stderr)),
                "capture_complete": capture_complete,
                "process_group_contained": True,
                "launcher_failures": launcher_failures,
            }
            if cancelled:
                return ProviderResult(
                    False,
                    provider=self.name,
                    error="provider execution cancelled",
                    metadata={**metadata, "cancelled": True, **cleanup},
                )
            if output_limit_exceeded:
                return ProviderResult(
                    False,
                    provider=self.name,
                    error=f"provider output exceeded combined {self.max_output_chars} character limit",
                    metadata={**metadata, "output_limit_exceeded": True, **cleanup},
                )
            if timed_out:
                return ProviderResult(
                    False,
                    provider=self.name,
                    error=f"provider timed out after {timeout}s",
                    metadata={**metadata, "timed_out": True, **cleanup},
                )
            if proc.returncode:
                error = (stderr or f"exit {proc.returncode}").strip()
                return ProviderResult(False, provider=self.name, error=error, metadata=metadata)
            return ProviderResult(
                True,
                stdout.strip(),
                self.name,
                metadata=metadata,
            )
        except OSError as exc:
            return ProviderResult(
                False,
                provider=self.name,
                error=f"provider process failed after start: {type(exc).__name__}",
                metadata={
                    **self._approval_metadata(approval),
                    **model_metadata,
                    "launcher_failures": launcher_failures,
                },
            )
        finally:
            if prompt_file:
                prompt_file.unlink(missing_ok=True)


class DeterministicProvider(Provider):
    """Offline provider that emits typed, reproducible workflow artifacts."""

    name = "deterministic"
    capabilities = ProviderCapabilities(
        execution_location=ExecutionLocation.LOCAL,
        location_declared=True,
    )

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "healthy": True,
            "version": "1",
            "capabilities": self.capabilities.to_dict(),
        }

    @staticmethod
    def _verdict(item: WorkItem) -> str:
        contract = item.inputs.get("stage_contract", item.inputs.get("contract", {}))
        allowed = contract.get("allowed_verdicts", []) if isinstance(contract, dict) else []
        stage = str(item.inputs.get("stage", "implementation"))
        preferred = {
            "policy-precheck": "ALIGNED",
            "implementation": "COMPLETE",
            "validation": "PASS",
            "policy-postcheck": "ALIGNED",
        }.get(stage, "COMPLETE")
        if not allowed or preferred in allowed:
            return preferred
        return next((value for value in allowed if value in PASSING_VERDICTS), str(allowed[0]))

    def execute(
        self,
        agent: Agent,
        item: WorkItem,
        context: dict[str, Any],
        approval: ExecutionApproval | ProviderExecutionAuthorization | None = None,
    ) -> ProviderResult:
        verdict = self._verdict(item)
        evidence = {
            criterion: "Satisfied by the deterministic offline simulation."
            for criterion in item.acceptance_criteria
        }
        payload = {
            "verdict": verdict,
            "criteria_evidence": evidence,
            "summary": f"Reproducible proposal for {item.title}.",
        }
        return ProviderResult(
            True,
            json.dumps(payload, indent=2, ensure_ascii=False),
            self.name,
            metadata={"deterministic": True, "agent": agent.id},
        )
