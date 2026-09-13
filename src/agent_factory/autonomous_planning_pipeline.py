"""Durable multi-role Autonomous Mission planning pipeline."""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .autonomous_authorization import (
    AuthorizationOperation,
    AuthorizationOutcome,
    AutonomousAuthorizationRequest,
    AutonomousAuthorizationService,
    PlanningAction,
)
from .autonomous_mission import MissionDisposition, MissionPhase
from .autonomous_planning import (
    AutonomousPlanningService,
    PlanningContextEnvelope,
    PlanningRoleAssignment,
    PlanningRoleModelManifest,
)
from .backlog import (
    BacklogManifestError,
    BacklogProposal,
    ProposedItem,
    proposal_from_document,
)
from .backlog_revisions import (
    BacklogRevision,
    BacklogRevisionOrigin,
    BacklogRevisionService,
)
from .mission_intake import AutonomousMissionIntakeService
from .models import (
    Agent,
    Budget,
    ProviderCapabilities,
    ProviderExecutionAuthorization,
    ProviderResult,
    WorkItem,
)
from .roles import RoleRegistry
from .runtime import AgentRuntime, ExecutionMode
from .software_roles import AUTONOMOUS_PLANNING_ROLE_IDS
from .storage import SQLiteStorage


class PlanningArtifactKind(StrEnum):
    MISSION_ANALYSIS = "MISSION_ANALYSIS"
    NORMALIZED_REQUIREMENTS = "NORMALIZED_REQUIREMENTS"
    ARCHITECTURE_PROPOSAL = "ARCHITECTURE_PROPOSAL"
    BACKLOG_PROPOSAL = "BACKLOG_PROPOSAL"
    REVIEW_REPORT = "REVIEW_REPORT"


ARTIFACT_KIND_BY_ROLE = {
    "mission_analyst": PlanningArtifactKind.MISSION_ANALYSIS,
    "product_requirements_analyst": PlanningArtifactKind.NORMALIZED_REQUIREMENTS,
    "software_architect": PlanningArtifactKind.ARCHITECTURE_PROPOSAL,
    "backlog_planner": PlanningArtifactKind.BACKLOG_PROPOSAL,
    "backlog_reviewer": PlanningArtifactKind.REVIEW_REPORT,
}

DIGEST_FIELD_BY_ROLE = {
    "mission_analyst": "analysis_digest",
    "product_requirements_analyst": "requirements_digest",
    "software_architect": "architecture_digest",
    "backlog_planner": "backlog_digest",
    "backlog_reviewer": "review_digest",
}


class PlanningPipelineCommandConflictError(ValueError):
    """Raised when a pipeline idempotency key is rebound to other input."""


class PlanningRoleOutputError(ValueError):
    """Raised when a local planning role returns invalid structured output."""


class PlanningPipelineFailedError(RuntimeError):
    def __init__(self, role_id: str, errors: tuple[str, ...]):
        self.role_id = role_id
        self.errors = errors
        detail = "; ".join(errors)[:1000]
        super().__init__(f"Planning role {role_id!r} exhausted repair attempts: {detail}")


@dataclass(frozen=True)
class PlanningInvocationRequest:
    mission_id: int
    run_id: int
    assignment: PlanningRoleAssignment
    context: PlanningContextEnvelope
    authorization: ProviderExecutionAuthorization
    attempt_number: int
    validation_feedback: tuple[str, ...]


class PlanningProviderInvoker(Protocol):
    def invoke(self, request: PlanningInvocationRequest) -> ProviderResult: ...


class RuntimePlanningInvoker:
    """Adapt the standard provider runtime to a non-persisted planning task."""

    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    def invoke(self, request: PlanningInvocationRequest) -> ProviderResult:
        from .planning_output_contracts import guidance
        assignment = request.assignment
        limits = dict(assignment.limits)
        agent = Agent(
            id=assignment.logical_agent_id,
            name=assignment.role_id.replace("_", " ").title(),
            role=assignment.role_id,
            enabled=True,
            provider=assignment.provider_id,
            model=assignment.model,
            permissions=list(assignment.permissions),
            instructions=(
                "Return one JSON object with exactly 'output' and 'evidence'. "
                "Follow the role contract in the context. Do not use Markdown fences, "
                "mutate the repository, or rely on prior conversation state."
                + guidance(assignment.role_id)
            ),
        )
        task = WorkItem(
            id=request.context.id,
            project_id=request.mission_id,
            kind="research",
            title=f"Autonomous planning role: {assignment.role_id}",
            description=(
                "Produce the role's typed planning artifact. Validation feedback: "
                + json.dumps(request.validation_feedback, ensure_ascii=False)
            ),
            acceptance_criteria=[
                "The response is one schema-valid JSON planning envelope.",
                "The response remains inside bounded read-only planning authority.",
            ],
            permissions=list(assignment.permissions),
            budget=Budget(
                max_tokens=int(limits.get("max_output_tokens", 8_000)),
                max_seconds=int(limits.get("max_seconds", 600)),
                max_cost_usd=0.0,
            ),
        )
        return self.runtime.run(
            agent,
            task,
            request.context.document,
            request.authorization,
            allow_fallback=False,
            mode=ExecutionMode.LIVE,
        )


@dataclass(frozen=True)
class PlanningInvocationAttempt:
    id: int
    run_id: int
    role_id: str
    invocation_order: int
    attempt_number: int
    context_id: int
    authorization_decision_id: int
    provider_id: str
    model: str
    logical_agent_id: str
    provider_ok: bool
    response_digest: str
    provider_metadata: dict[str, Any]
    valid: bool
    validation_errors: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class PlanningPipelineArtifact:
    id: int
    run_id: int
    manifest_id: int
    invocation_id: int
    role_id: str
    invocation_order: int
    artifact_kind: PlanningArtifactKind
    content: dict[str, Any]
    output_digest: str
    evidence: dict[str, Any]
    evidence_digest: str
    artifact_digest: str
    created_at: str

    def upstream_reference(self) -> dict[str, Any]:
        return {
            "artifact_id": self.id,
            "artifact_type": self.artifact_kind.value,
            "role_id": self.role_id,
            "digest": self.output_digest,
            "content": self.content,
        }


@dataclass(frozen=True)
class PlanningPipelineRun:
    id: int
    identity: str
    mission_id: int
    manifest_id: int
    planning_authorization_id: int
    proposal_key: str
    requested_action: PlanningAction
    max_attempts_per_role: int
    created_by: str
    command_id: str
    request_digest: str
    created_at: str
    status: str
    artifacts: tuple[PlanningPipelineArtifact, ...]
    revision_id: int | None
    revision_digest: str | None
    completion_digest: str | None
    failed_role_id: str | None
    failure_errors: tuple[str, ...]


class PlanningArtifactSchemas:
    """Deterministic nested schemas shared by generation and verification."""

    REQUIREMENT_ID = re.compile(r"^[A-Z][A-Z0-9._-]{1,63}$")
    IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
    OBSERVABLE = re.compile(
        r"\b(?:returns?|rejects?|persists?|records?|equals?|contains?|completes?|"
        r"passes?|fails?|emits?|creates?|remains?|prevents?|requires?|within|"
        r"at\s+least|no\s+|only\s+)\b",
        re.IGNORECASE,
    )
    GAME_MEASUREMENT = re.compile(
        r"\b(?:moves?|collects?|rotates?|jumps?|travels?|scores?)\b.{0,100}"
        r"\b\d+(?:\.\d+)?\s*(?:pixels?|frames?|coins?|points?|degrees?|meters?|metres?)\b",
        re.IGNORECASE,
    )
    RUNTIME_CHECK = re.compile(
        r"\b(?:loads?|runs?|starts?|launches?)\b.{0,80}\b(?:without errors?|exit code\s+0)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _object(value: Any, label: str, fields: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlanningRoleOutputError(f"{label} must be an object")
        missing = fields - set(value)
        unknown = set(value) - fields
        if missing or unknown:
            raise PlanningRoleOutputError(
                f"{label} fields invalid: missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )
        return value

    @staticmethod
    def _text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PlanningRoleOutputError(f"{label} must be a non-empty string")
        return value.strip()

    @classmethod
    def _strings(
        cls, value: Any, label: str, *, allow_empty: bool = False
    ) -> tuple[str, ...]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise PlanningRoleOutputError(
                f"{label} must be a list of non-empty strings"
            )
        result = tuple(item.strip() for item in value)
        if not allow_empty and not result:
            raise PlanningRoleOutputError(f"{label} cannot be empty")
        if len(result) != len(set(result)):
            raise PlanningRoleOutputError(f"{label} values must be unique")
        return result

    @classmethod
    def mission_analysis(cls, value: Any) -> None:
        document = cls._object(
            value,
            "mission_analysis",
            {"summary", "outcomes", "constraints", "ambiguities", "source_references"},
        )
        cls._text(document["summary"], "mission_analysis.summary")
        cls._strings(document["outcomes"], "mission_analysis.outcomes")
        cls._strings(
            document["constraints"], "mission_analysis.constraints", allow_empty=True
        )
        cls._strings(
            document["ambiguities"], "mission_analysis.ambiguities", allow_empty=True
        )
        cls._strings(
            document["source_references"], "mission_analysis.source_references"
        )

    @classmethod
    def normalized_requirements(cls, value: Any) -> set[str]:
        document = cls._object(
            value,
            "normalized_requirements",
            {"functional", "non_functional"},
        )
        identifiers: set[str] = set()
        total = 0
        for category in ("functional", "non_functional"):
            entries = document[category]
            if not isinstance(entries, list):
                raise PlanningRoleOutputError(
                    f"normalized_requirements.{category} must be an array"
                )
            for index, raw in enumerate(entries):
                item = cls._object(
                    raw,
                    f"normalized_requirements.{category}[{index}]",
                    {
                        "id",
                        "statement",
                        "acceptance_criteria",
                        "source_references",
                        "priority",
                    },
                )
                identifier = cls._text(item["id"], f"requirement[{index}].id")
                if not cls.REQUIREMENT_ID.fullmatch(identifier):
                    raise PlanningRoleOutputError(
                        f"Requirement id {identifier!r} is invalid"
                    )
                if identifier in identifiers:
                    raise PlanningRoleOutputError(
                        f"Requirement id {identifier!r} is duplicated"
                    )
                identifiers.add(identifier)
                cls._text(item["statement"], f"requirement[{identifier}].statement")
                criteria = cls._strings(
                    item["acceptance_criteria"],
                    f"requirement[{identifier}].acceptance_criteria",
                )
                for criterion in criteria:
                    cls._measurable(criterion, f"requirement[{identifier}]")
                cls._strings(
                    item["source_references"],
                    f"requirement[{identifier}].source_references",
                )
                if item["priority"] not in {"P0", "P1", "P2", "P3"}:
                    raise PlanningRoleOutputError(
                        f"Requirement {identifier!r} has invalid priority"
                    )
                total += 1
        if total == 0:
            raise PlanningRoleOutputError("At least one normalized requirement is required")
        return identifiers

    @classmethod
    def architecture(cls, value: Any, requirement_ids: set[str]) -> set[str]:
        document = cls._object(
            value,
            "architecture_proposal",
            {"summary", "components", "interfaces", "infrastructure", "decisions"},
        )
        cls._text(document["summary"], "architecture_proposal.summary")
        component_ids: set[str] = set()
        components = document["components"]
        if not isinstance(components, list) or not components:
            raise PlanningRoleOutputError("Architecture requires at least one component")
        for index, raw in enumerate(components):
            item = cls._object(
                raw,
                f"architecture.components[{index}]",
                {"id", "name", "responsibilities", "requirement_ids"},
            )
            identifier = cls._identifier(item["id"], f"component[{index}].id")
            if identifier in component_ids:
                raise PlanningRoleOutputError(f"Component {identifier!r} is duplicated")
            component_ids.add(identifier)
            cls._text(item["name"], f"component[{identifier}].name")
            cls._strings(
                item["responsibilities"], f"component[{identifier}].responsibilities"
            )
            cls._references(
                item["requirement_ids"],
                requirement_ids,
                f"component[{identifier}].requirement_ids",
            )
        interfaces = document["interfaces"]
        if not isinstance(interfaces, list):
            raise PlanningRoleOutputError("architecture.interfaces must be an array")
        interface_ids: set[str] = set()
        for index, raw in enumerate(interfaces):
            item = cls._object(
                raw,
                f"architecture.interfaces[{index}]",
                {"id", "from_component", "to_component", "contract"},
            )
            identifier = cls._identifier(item["id"], f"interface[{index}].id")
            if identifier in interface_ids:
                raise PlanningRoleOutputError(f"Interface {identifier!r} is duplicated")
            interface_ids.add(identifier)
            if (
                item["from_component"] not in component_ids
                or item["to_component"] not in component_ids
            ):
                raise PlanningRoleOutputError(
                    f"Interface {identifier!r} references an unknown component"
                )
            cls._text(item["contract"], f"interface[{identifier}].contract")
        infrastructure = document["infrastructure"]
        if not isinstance(infrastructure, list):
            raise PlanningRoleOutputError("architecture.infrastructure must be an array")
        infrastructure_names: set[str] = set()
        infrastructure_ids: set[str] = set()
        for index, raw in enumerate(infrastructure):
            item = cls._object(
                raw,
                f"architecture.infrastructure[{index}]",
                {"id", "name", "purpose", "bootstrap_required", "requirement_ids"},
            )
            identifier = cls._identifier(item["id"], f"infrastructure[{index}].id")
            if identifier in infrastructure_ids:
                raise PlanningRoleOutputError(
                    f"Infrastructure {identifier!r} is duplicated"
                )
            infrastructure_ids.add(identifier)
            name = cls._text(item["name"], f"infrastructure[{identifier}].name")
            if name.casefold() in infrastructure_names:
                raise PlanningRoleOutputError(f"Infrastructure {name!r} is duplicated")
            infrastructure_names.add(name.casefold())
            cls._text(item["purpose"], f"infrastructure[{identifier}].purpose")
            if not isinstance(item["bootstrap_required"], bool):
                raise PlanningRoleOutputError(
                    f"Infrastructure {identifier!r} bootstrap_required must be boolean"
                )
            cls._references(
                item["requirement_ids"],
                requirement_ids,
                f"infrastructure[{identifier}].requirement_ids",
            )
        decisions = document["decisions"]
        if not isinstance(decisions, list) or not decisions:
            raise PlanningRoleOutputError("Architecture requires at least one decision")
        decision_ids: set[str] = set()
        for index, raw in enumerate(decisions):
            item = cls._object(
                raw,
                f"architecture.decisions[{index}]",
                {"id", "decision", "rationale", "requirement_ids"},
            )
            identifier = cls._identifier(item["id"], f"decision[{index}].id")
            if identifier in decision_ids:
                raise PlanningRoleOutputError(f"Decision {identifier!r} is duplicated")
            decision_ids.add(identifier)
            cls._text(item["decision"], f"decision[{identifier}].decision")
            cls._text(item["rationale"], f"decision[{identifier}].rationale")
            cls._references(
                item["requirement_ids"],
                requirement_ids,
                f"decision[{identifier}].requirement_ids",
            )
        return infrastructure_names

    @classmethod
    def _identifier(cls, value: Any, label: str) -> str:
        result = cls._text(value, label)
        if not cls.IDENTIFIER.fullmatch(result):
            raise PlanningRoleOutputError(f"{label} is invalid")
        return result

    @classmethod
    def _references(
        cls, value: Any, known: set[str], label: str
    ) -> tuple[str, ...]:
        references = cls._strings(value, label)
        unknown = set(references) - known
        if unknown:
            raise PlanningRoleOutputError(
                f"{label} contains unknown requirements: {sorted(unknown)}"
            )
        return references

    @classmethod
    def _measurable(cls, value: str, label: str) -> None:
        normalized = value.strip()
        if len(normalized) < 12 or not (
            cls.OBSERVABLE.search(normalized) or cls.GAME_MEASUREMENT.search(normalized)
            or cls.RUNTIME_CHECK.search(normalized)
        ):
            raise PlanningRoleOutputError(
                f"{label} acceptance criterion is not deterministically measurable: "
                f"{normalized!r}"
            )

    @staticmethod
    def canonical_item_ids(items: tuple[ProposedItem, ...]) -> tuple[str, ...]:
        by_id = {item.stable_id: item for item in items}
        prerequisites = {
            item.stable_id: set(item.dependencies)
            | ({item.parent_id} if item.parent_id else set())
            for item in items
        }
        dependents: dict[str, set[str]] = {item_id: set() for item_id in by_id}
        for item_id, required in prerequisites.items():
            for dependency in required:
                dependents[dependency].add(item_id)
        ready = [item_id for item_id, required in prerequisites.items() if not required]
        heapq.heapify(ready)
        result: list[str] = []
        while ready:
            item_id = heapq.heappop(ready)
            result.append(item_id)
            for dependent in sorted(dependents[item_id]):
                prerequisites[dependent].discard(item_id)
                if not prerequisites[dependent]:
                    heapq.heappush(ready, dependent)
        if len(result) != len(items):
            raise PlanningRoleOutputError("Backlog dependencies are cyclic")
        return tuple(result)

    @classmethod
    def backlog(
        cls,
        value: Any,
        *,
        source_path: str,
        source_sha256: str,
        source_name: str,
        requirement_ids: set[str],
        architecture: dict[str, Any],
    ) -> BacklogProposal:
        document = cls._object(value, "backlog_proposal", {"schema_version", "items"})
        try:
            proposal = proposal_from_document(
                document,
                source_path=source_path,
                source_sha256=source_sha256,
                source_name=source_name,
            )
        except BacklogManifestError as exc:
            raise PlanningRoleOutputError(str(exc)) from exc
        if proposal.schema_version != 2:
            raise PlanningRoleOutputError("Autonomous backlog proposals require schema v2")
        if not any(item.executable for item in proposal.items):
            raise PlanningRoleOutputError(
                "Autonomous backlog requires at least one executable item"
            )
        expected_order = cls.canonical_item_ids(proposal.items)
        actual_order = tuple(item.stable_id for item in proposal.items)
        if actual_order != expected_order:
            raise PlanningRoleOutputError(
                f"Backlog items are not in canonical dependency order: {expected_order}"
            )
        covered_requirements: set[str] = set()
        for item in proposal.items:
            references = set(item.source_references)
            if item.executable and not (references & requirement_ids):
                raise PlanningRoleOutputError(
                    f"Executable item {item.stable_id!r} lacks requirement traceability"
                )
            unknown = {
                reference
                for reference in references
                if reference.startswith("REQ-") and reference not in requirement_ids
            }
            if unknown:
                raise PlanningRoleOutputError(
                    f"Item {item.stable_id!r} references unknown requirements: "
                    f"{sorted(unknown)}"
                )
            covered_requirements.update(references & requirement_ids)
            if item.executable:
                if len(item.acceptance_criteria) != len(set(item.acceptance_criteria)):
                    raise PlanningRoleOutputError(
                        f"Item {item.stable_id!r} duplicates acceptance criteria"
                    )
                for criterion in item.acceptance_criteria:
                    cls._measurable(criterion, f"item[{item.stable_id}]")
        uncovered = requirement_ids - covered_requirements
        if uncovered:
            raise PlanningRoleOutputError(
                f"Backlog does not cover requirements: {sorted(uncovered)}"
            )
        cls._infrastructure_order(proposal.items, architecture)
        return proposal

    @classmethod
    def _infrastructure_order(
        cls, items: tuple[ProposedItem, ...], architecture: dict[str, Any]
    ) -> None:
        bootstrap_names = {
            str(value["name"]).strip().casefold()
            for value in architecture["infrastructure"]
            if value["bootstrap_required"]
        }
        if not bootstrap_names:
            return
        by_id = {item.stable_id: item for item in items}

        def is_infrastructure(item: ProposedItem) -> bool:
            labels = {label.casefold() for label in item.labels}
            role = item.assigned_role.casefold()
            return bool(
                labels & {"infrastructure", "bootstrap"}
                or "infrastructure" in role
                or "bootstrap" in role
            )

        infrastructure_tasks = [
            item for item in items if item.executable and is_infrastructure(item)
        ]
        for name in sorted(bootstrap_names):
            candidates = [
                item
                for item in infrastructure_tasks
                if name in item.title.casefold()
                or name in item.description.casefold()
                or name
                in {value.casefold() for value in item.required_infrastructure}
            ]
            if not candidates:
                raise PlanningRoleOutputError(
                    f"Bootstrap-required infrastructure {name!r} has no setup task"
                )
            candidate_ids = {item.stable_id for item in candidates}
            for item in items:
                if not item.executable or item.stable_id in candidate_ids:
                    continue
                if name not in {
                    value.casefold() for value in item.required_infrastructure
                }:
                    continue
                ancestors: set[str] = set()
                stack = list(item.dependencies)
                while stack:
                    dependency = stack.pop()
                    if dependency in ancestors:
                        continue
                    ancestors.add(dependency)
                    stack.extend(by_id[dependency].dependencies)
                if not (ancestors & candidate_ids):
                    raise PlanningRoleOutputError(
                        f"Item {item.stable_id!r} must depend on infrastructure "
                        f"setup for {name!r}"
                    )

    @classmethod
    def review_report(cls, value: Any) -> None:
        document = cls._object(
            value, "review_report", {"verdict", "summary", "findings"}
        )
        if document["verdict"] not in {"READY", "NEEDS_REPAIR"}:
            raise PlanningRoleOutputError("Review verdict must be READY or NEEDS_REPAIR")
        cls._text(document["summary"], "review_report.summary")
        findings = document["findings"]
        if not isinstance(findings, list):
            raise PlanningRoleOutputError("review_report.findings must be an array")
        identifiers: set[str] = set()
        for index, raw in enumerate(findings):
            item = cls._object(
                raw,
                f"review_report.findings[{index}]",
                {
                    "id",
                    "severity",
                    "status",
                    "message",
                    "artifact_references",
                    "display_to_human",
                },
            )
            identifier = cls._identifier(item["id"], f"finding[{index}].id")
            if identifier in identifiers:
                raise PlanningRoleOutputError(f"Review finding {identifier!r} is duplicated")
            identifiers.add(identifier)
            if item["severity"] not in {"BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO"}:
                raise PlanningRoleOutputError(
                    f"Review finding {identifier!r} has invalid severity"
                )
            if item["status"] not in {"OPEN", "RESOLVED", "ACCEPTED_RISK"}:
                raise PlanningRoleOutputError(
                    f"Review finding {identifier!r} has invalid status"
                )
            cls._text(item["message"], f"finding[{identifier}].message")
            cls._strings(
                item["artifact_references"],
                f"finding[{identifier}].artifact_references",
            )
            if not isinstance(item["display_to_human"], bool):
                raise PlanningRoleOutputError(
                    f"Review finding {identifier!r} display_to_human must be boolean"
                )


class AutonomousPlanningPipelineService:
    """Invoke five local roles sequentially and persist a proposed revision."""

    MAX_RESPONSE_CHARS = 1_000_000
    PREAPPROVAL_PHASES = frozenset(
        {
            MissionPhase.SPECIFICATION_ANALYSIS,
            MissionPhase.BACKLOG_GENERATION,
            MissionPhase.WAITING_FOR_BACKLOG_APPROVAL,
        }
    )

    def __init__(
        self,
        storage: SQLiteStorage,
        invoker: PlanningProviderInvoker,
        provider_capabilities: Mapping[str, ProviderCapabilities] | None = None,
    ):
        self.storage = storage
        self.invoker = invoker
        self.provider_capabilities = dict(provider_capabilities or {})
        self.planning = AutonomousPlanningService(
            storage, self.provider_capabilities
        )
        self.authorizations = AutonomousAuthorizationService(
            storage, self.provider_capabilities
        )
        self.intake = AutonomousMissionIntakeService(storage)
        self.roles = RoleRegistry(storage)
        self.revisions = BacklogRevisionService(storage)

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Planning pipeline values must be canonical JSON") from exc

    @classmethod
    def _digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _required(value: str, label: str) -> str:
        result = str(value).strip()
        if not result:
            raise ValueError(f"{label} is required")
        return result

    def _existing_run(
        self, command_id: str, request_digest: str
    ) -> PlanningPipelineRun | None:
        row = self.storage.db.execute(
            "SELECT id,request_digest FROM autonomous_planning_pipeline_runs WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if not row:
            return None
        if row["request_digest"] != request_digest:
            raise PlanningPipelineCommandConflictError(
                f"Planning pipeline command {command_id!r} is already bound"
            )
        result = self.get_run(int(row["id"]))
        if result.status == "FAILED":
            raise PlanningPipelineFailedError(
                result.failed_role_id or "unknown", result.failure_errors
            )
        return result

    def _create_run(
        self,
        *,
        mission_id: int,
        manifest: PlanningRoleModelManifest,
        planning_authorization_id: int,
        actor: str,
        command_id: str,
        max_attempts_per_role: int,
    ) -> PlanningPipelineRun:
        authorization = self.authorizations.get_planning_authorization(
            planning_authorization_id
        )
        mission = self.planning.missions.get(mission_id)
        request = {
            "type": "run_autonomous_planning_pipeline",
            "mission_id": mission_id,
            "manifest_id": manifest.id,
            "manifest_digest": manifest.manifest_digest,
            "planning_authorization_id": planning_authorization_id,
            "planning_authorization_digest": authorization.authorization_digest,
            "proposal_key": manifest.proposal_key,
            "requested_action": authorization.requested_action.value,
            "actor": actor,
            "max_attempts_per_role": max_attempts_per_role,
        }
        request_digest = self._digest(request)
        replay = self._existing_run(command_id, request_digest)
        if replay:
            return replay
        if actor != mission.mission_owner or actor != authorization.authorized_by:
            raise PermissionError("Planning pipeline requires the authorizing mission owner")
        if mission.phase not in self.PREAPPROVAL_PHASES:
            raise PermissionError("Planning pipeline is available only before approval")
        if mission.disposition is not MissionDisposition.RUNNING:
            raise PermissionError("Planning pipeline is fenced by mission disposition")
        if manifest.mission_id != mission_id or manifest.stale:
            raise ValueError("Planning manifest does not bind the current mission source")
        if authorization.mission_id != mission_id:
            raise PermissionError("Planning authorization belongs to another mission")
        if authorization.closed:
            raise PermissionError("Planning authorization is already closed")
        if authorization.planning_request_id != manifest.proposal_key:
            raise ValueError("Planning request must match the manifest proposal key")
        assignment_models = {
            value.role_id: value.model for value in manifest.assignments
        }
        if assignment_models != authorization.role_models:
            raise ValueError("Planning authorization role/model bindings changed")
        if not {
            value.provider_id for value in manifest.assignments
        } <= set(authorization.provider_ids):
            raise PermissionError("Manifest provider is outside planning authorization")
        created_at = self._timestamp()
        with self.storage.db:
            self.storage._begin_immediate()
            replay = self._existing_run(command_id, request_digest)
            if replay:
                return replay
            current_manifest = self.planning.get_manifest(manifest.id)
            current_mission = self.planning.missions.get(mission_id)
            current_authorization = self.authorizations.get_planning_authorization(
                planning_authorization_id
            )
            if current_manifest.stale or current_manifest.manifest_digest != manifest.manifest_digest:
                raise ValueError("Planning manifest changed before run commit")
            if (
                current_mission.phase not in self.PREAPPROVAL_PHASES
                or current_mission.disposition is not MissionDisposition.RUNNING
                or current_authorization.closed
            ):
                raise PermissionError("Planning scope closed before run commit")
            cursor = self.storage.db.execute(
                """INSERT INTO autonomous_planning_pipeline_runs(
                       identity,mission_id,manifest_id,planning_authorization_id,
                       proposal_key,requested_action,max_attempts_per_role,
                       created_by,command_id,request_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.storage._identity("autonomous-planning-pipeline-run"),
                    mission_id,
                    manifest.id,
                    planning_authorization_id,
                    manifest.proposal_key,
                    authorization.requested_action.value,
                    max_attempts_per_role,
                    actor,
                    command_id,
                    request_digest,
                    created_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            self.storage._event(
                "autonomous_planning.pipeline_started",
                "autonomous_mission",
                mission_id,
                {
                    "run_id": run_id,
                    "manifest_id": manifest.id,
                    "manifest_digest": manifest.manifest_digest,
                    "planning_authorization_id": planning_authorization_id,
                    "proposal_key": manifest.proposal_key,
                    "requested_action": authorization.requested_action.value,
                    "actor": actor,
                },
            )
        return self.get_run(run_id)

    def execute(
        self,
        mission_id: int,
        *,
        manifest_id: int,
        planning_authorization_id: int,
        actor: str,
        command_id: str,
        max_attempts_per_role: int = 2,
    ) -> PlanningPipelineRun:
        actor = self._required(actor, "Pipeline actor")
        command_id = self._required(command_id, "Command id")
        if not 1 <= int(max_attempts_per_role) <= 5:
            raise ValueError("Role repair attempts must be between one and five")
        manifest = self.planning.get_manifest(manifest_id)
        run = self._create_run(
            mission_id=mission_id,
            manifest=manifest,
            planning_authorization_id=planning_authorization_id,
            actor=actor,
            command_id=command_id,
            max_attempts_per_role=int(max_attempts_per_role),
        )
        if run.status == "COMPLETED":
            return run
        artifacts = {artifact.role_id: artifact for artifact in run.artifacts}
        current_role_id = next(
            (
                assignment.role_id
                for assignment in manifest.assignments
                if assignment.role_id not in artifacts
            ),
            "backlog_reviewer",
        )
        try:
            for assignment in manifest.assignments:
                if assignment.role_id in artifacts:
                    continue
                current_role_id = assignment.role_id
                artifact = self._execute_role(
                    run,
                    manifest,
                    assignment,
                    prior_artifacts=tuple(
                        artifacts[role_id]
                        for role_id in AUTONOMOUS_PLANNING_ROLE_IDS
                        if role_id in artifacts
                    ),
                )
                artifacts[assignment.role_id] = artifact
            self.authorizations.close_planning_authority(
                planning_authorization_id,
                actor=actor,
                command_id=f"{command_id}:close-planning",
                reason="All five bounded planning roles completed",
            )
            return self._complete(run, manifest, artifacts, actor=actor)
        except PlanningPipelineFailedError:
            raise
        except Exception as exc:
            if self.planning.get_manifest(manifest.id).stale:
                errors = (
                    "The authoritative specification changed before the planning "
                    "proposal committed",
                )
                attempt_count = max(
                    1,
                    sum(
                        attempt.role_id == current_role_id
                        for attempt in self.attempts(run.id)
                    ),
                )
                self._close_and_fail(
                    run,
                    current_role_id,
                    attempt_count,
                    errors,
                    reason="Specification source changed during planning",
                )
                raise PlanningPipelineFailedError(current_role_id, errors) from exc
            raise

    def _execute_role(
        self,
        run: PlanningPipelineRun,
        manifest: PlanningRoleModelManifest,
        assignment: PlanningRoleAssignment,
        *,
        prior_artifacts: tuple[PlanningPipelineArtifact, ...],
    ) -> PlanningPipelineArtifact:
        attempts = self.storage.db.execute(
            """SELECT * FROM autonomous_planning_pipeline_invocations
                WHERE run_id=? AND role_id=? ORDER BY attempt_number""",
            (run.id, assignment.role_id),
        ).fetchall()
        feedback = (
            tuple(json.loads(attempts[-1]["validation_errors_json"]))
            if attempts
            else ()
        )
        next_attempt = len(attempts) + 1
        while next_attempt <= run.max_attempts_per_role:
            upstream = [artifact.upstream_reference() for artifact in prior_artifacts]
            if feedback:
                feedback_content = {
                    "role_id": assignment.role_id,
                    "attempt": next_attempt - 1,
                    "validation_errors": list(feedback),
                }
                upstream.append(
                    {
                        "artifact_type": "VALIDATION_FEEDBACK",
                        "role_id": assignment.role_id,
                        "digest": self._digest(feedback_content),
                        "content": feedback_content,
                    }
                )
            context = self.planning.create_context(
                manifest.id,
                assignment.role_id,
                actor=f"planning-pipeline:{run.id}",
                command_id=(
                    f"{run.command_id}:context:{assignment.invocation_order}:"
                    f"{next_attempt}"
                ),
                upstream_artifacts=tuple(upstream),
            )
            permissions = tuple(
                sorted(
                    {
                        *assignment.permissions,
                        "execute_provider",
                        "structured_artifacts",
                    }
                )
            )
            authorization_record = self.authorizations.get_planning_authorization(
                run.planning_authorization_id
            )
            decision = self.authorizations.resolve(
                AutonomousAuthorizationRequest(
                    mission_id=run.mission_id,
                    operation=AuthorizationOperation.PLANNING_INFERENCE,
                    provider_id=assignment.provider_id,
                    agent_id=assignment.logical_agent_id,
                    task_id=context.id,
                    role=assignment.role_id,
                    model=assignment.model,
                    repository_path=authorization_record.repository_path,
                    tool_profile=authorization_record.tool_profile,
                    permissions=permissions,
                    planning_authorization_id=run.planning_authorization_id,
                    planning_request_id=run.proposal_key,
                    requested_action=run.requested_action,
                )
            )
            if decision.outcome is not AuthorizationOutcome.ALLOW_PLANNING:
                raise PermissionError(
                    f"Planning role {assignment.role_id!r} authorization denied: "
                    f"{decision.reason}"
                )
            provider_authorization = self.authorizations.provider_authorization(decision)
            request = PlanningInvocationRequest(
                mission_id=run.mission_id,
                run_id=run.id,
                assignment=assignment,
                context=context,
                authorization=provider_authorization,
                attempt_number=next_attempt,
                validation_feedback=feedback,
            )
            try:
                result = self.invoker.invoke(request)
            except Exception as exc:  # Provider failures are bounded input evidence.
                result = ProviderResult(
                    False,
                    provider=assignment.provider_id,
                    error=f"Planning invoker failed: {type(exc).__name__}",
                )
            output, evidence, errors = self._validate_response(
                assignment.role_id,
                result,
                prior_artifacts=prior_artifacts,
                manifest=manifest,
            )
            artifact = self._record_invocation(
                run=run,
                manifest=manifest,
                assignment=assignment,
                attempt_number=next_attempt,
                context=context,
                decision_id=decision.id,
                result=result,
                output=output,
                evidence=evidence,
                errors=errors,
            )
            if artifact is not None:
                return artifact
            feedback = errors
            next_attempt += 1
        self._close_and_fail(
            run,
            assignment.role_id,
            run.max_attempts_per_role,
            feedback,
            reason=f"Planning role {assignment.role_id} exhausted repair attempts",
        )
        raise PlanningPipelineFailedError(assignment.role_id, feedback)

    @staticmethod
    def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise PlanningRoleOutputError(
                    f"Planning response contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def _parse_response(self, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise PlanningRoleOutputError("Planning provider returned empty output")
        if len(content) > self.MAX_RESPONSE_CHARS:
            raise PlanningRoleOutputError("Planning provider response exceeds 1,000,000 chars")
        try:
            value = json.loads(
                content,
                object_pairs_hook=self._pairs,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite number {constant}")
                ),
            )
        except PlanningRoleOutputError:
            raise
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise PlanningRoleOutputError(
                "Planning provider response is not strict JSON"
            ) from exc
        if not isinstance(value, dict) or set(value) != {"output", "evidence"}:
            raise PlanningRoleOutputError(
                "Planning response must contain exactly output and evidence"
            )
        if not isinstance(value["output"], dict) or not isinstance(value["evidence"], dict):
            raise PlanningRoleOutputError("Planning output and evidence must be objects")
        return value

    def _validate_response(
        self,
        role_id: str,
        result: ProviderResult,
        *,
        prior_artifacts: tuple[PlanningPipelineArtifact, ...],
        manifest: PlanningRoleModelManifest,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, tuple[str, ...]]:
        if not result.ok:
            return None, None, (
                f"Provider execution failed: {str(result.error or 'unknown error')[:500]}",
            )
        try:
            envelope = self._parse_response(result.content)
            output = envelope["output"]
            evidence = dict(envelope["evidence"])
            output_digest = self._digest(output)
            evidence[DIGEST_FIELD_BY_ROLE[role_id]] = output_digest
            self.roles.validate_output(
                role_id,
                next(
                    value.role_version
                    for value in manifest.assignments
                    if value.role_id == role_id
                ),
                output,
            )
            self.roles.validate_evidence(
                role_id,
                next(
                    value.role_version
                    for value in manifest.assignments
                    if value.role_id == role_id
                ),
                evidence,
            )
            self._validate_semantics(
                role_id, output, evidence, prior_artifacts=prior_artifacts
            )
            return output, evidence, ()
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            return None, None, (str(exc)[:1000] or type(exc).__name__,)

    def _validate_semantics(
        self,
        role_id: str,
        output: dict[str, Any],
        evidence: dict[str, Any],
        *,
        prior_artifacts: tuple[PlanningPipelineArtifact, ...],
    ) -> None:
        by_role = {artifact.role_id: artifact for artifact in prior_artifacts}
        if role_id == "mission_analyst":
            PlanningArtifactSchemas.mission_analysis(output["mission_analysis"])
            PlanningArtifactSchemas._strings(
                evidence["source_trace"], "mission analyst source_trace"
            )
            return
        requirements = output.get("normalized_requirements")
        if role_id == "product_requirements_analyst":
            PlanningArtifactSchemas.normalized_requirements(requirements)
            matrix = evidence["traceability_matrix"]
            if not isinstance(matrix, list) or not matrix:
                raise PlanningRoleOutputError("Requirements traceability matrix is required")
            return
        requirements_document = by_role["product_requirements_analyst"].content[
            "normalized_requirements"
        ]
        requirement_ids = PlanningArtifactSchemas.normalized_requirements(
            requirements_document
        )
        if role_id == "software_architect":
            PlanningArtifactSchemas.architecture(
                output["architecture_proposal"], requirement_ids
            )
            trace = evidence["decision_trace"]
            if not isinstance(trace, list) or not trace:
                raise PlanningRoleOutputError("Architecture decision trace is required")
            return
        architecture = by_role["software_architect"].content[
            "architecture_proposal"
        ]
        PlanningArtifactSchemas.architecture(architecture, requirement_ids)
        bound_manifest = self.planning.get_manifest(
            by_role["software_architect"].manifest_id
        )
        source = self.intake.get_source(bound_manifest.specification_source_id)
        if role_id == "backlog_planner":
            PlanningArtifactSchemas.backlog(
                output["backlog_proposal"],
                source_path="autonomous://specification",
                source_sha256=source.raw_digest,
                source_name=source.source_name,
                requirement_ids=requirement_ids,
                architecture=architecture,
            )
            dependency_evidence = evidence["dependency_evidence"]
            if not isinstance(dependency_evidence, list):
                raise PlanningRoleOutputError("Backlog dependency evidence must be an array")
            return
        backlog = by_role["backlog_planner"].content["backlog_proposal"]
        PlanningArtifactSchemas.backlog(
            backlog,
            source_path="autonomous://specification",
            source_sha256=source.raw_digest,
            source_name=source.source_name,
            requirement_ids=requirement_ids,
            architecture=architecture,
        )
        PlanningArtifactSchemas.review_report(output["review_report"])
        if evidence["findings"] != output["review_report"]["findings"]:
            raise PlanningRoleOutputError(
                "Reviewer evidence findings must match the review report"
            )

    def _safe_metadata(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            canonical = self._json(value)
        except ValueError:
            return {"metadata_serializable": False}
        if len(canonical) > 50_000:
            return {"metadata_truncated": True, "metadata_digest": self._digest(value)}
        return json.loads(canonical)

    def _record_invocation(
        self,
        *,
        run: PlanningPipelineRun,
        manifest: PlanningRoleModelManifest,
        assignment: PlanningRoleAssignment,
        attempt_number: int,
        context: PlanningContextEnvelope,
        decision_id: int,
        result: ProviderResult,
        output: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        errors: tuple[str, ...],
    ) -> PlanningPipelineArtifact | None:
        response = str(result.content or "")
        retained_response = response[: self.MAX_RESPONSE_CHARS]
        response_digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
        metadata = self._safe_metadata(dict(result.metadata or {}))
        created_at = self._timestamp()
        valid = output is not None and evidence is not None and not errors
        with self.storage.db:
            self.storage._begin_immediate()
            current_manifest = self.planning.get_manifest(manifest.id)
            if (
                current_manifest.stale
                or current_manifest.manifest_digest != manifest.manifest_digest
            ):
                raise PermissionError(
                    "Planning manifest became stale before role output commit"
                )
            existing = self.storage.db.execute(
                """SELECT * FROM autonomous_planning_pipeline_invocations
                    WHERE run_id=? AND role_id=? AND attempt_number=?""",
                (run.id, assignment.role_id, attempt_number),
            ).fetchone()
            if existing:
                if bool(existing["valid"]):
                    artifact_row = self.storage.db.execute(
                        """SELECT a.*,i.authorization_decision_id,c.context_digest
                             FROM autonomous_planning_pipeline_artifacts a
                             JOIN autonomous_planning_pipeline_invocations i
                               ON i.id=a.invocation_id
                             JOIN autonomous_planning_contexts c ON c.id=i.context_id
                            WHERE a.invocation_id=?""",
                        (existing["id"],),
                    ).fetchone()
                    return self._artifact(artifact_row)
                return None
            cursor = self.storage.db.execute(
                """INSERT INTO autonomous_planning_pipeline_invocations(
                       identity,run_id,role_id,invocation_order,attempt_number,
                       context_id,authorization_decision_id,provider_id,model,
                       logical_agent_id,provider_ok,response_text,response_digest,
                       provider_metadata_json,output_json,evidence_json,valid,
                       validation_errors_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.storage._identity("autonomous-planning-invocation"),
                    run.id,
                    assignment.role_id,
                    assignment.invocation_order,
                    attempt_number,
                    context.id,
                    decision_id,
                    assignment.provider_id,
                    assignment.model,
                    assignment.logical_agent_id,
                    int(bool(result.ok)),
                    retained_response,
                    response_digest,
                    self._json(metadata),
                    self._json(output) if valid else None,
                    self._json(evidence) if valid else None,
                    int(valid),
                    self._json(list(errors)),
                    created_at,
                ),
            )
            invocation_id = int(cursor.lastrowid)
            artifact_id = None
            if valid:
                output_digest = self._digest(output)
                evidence_digest = self._digest(evidence)
                kind = ARTIFACT_KIND_BY_ROLE[assignment.role_id]
                artifact_binding = {
                    "run_id": run.id,
                    "manifest_id": manifest.id,
                    "invocation_id": invocation_id,
                    "context_digest": context.context_digest,
                    "authorization_decision_id": decision_id,
                    "role_id": assignment.role_id,
                    "invocation_order": assignment.invocation_order,
                    "artifact_kind": kind.value,
                    "output_digest": output_digest,
                    "evidence_digest": evidence_digest,
                }
                artifact_digest = self._digest(artifact_binding)
                artifact_cursor = self.storage.db.execute(
                    """INSERT INTO autonomous_planning_pipeline_artifacts(
                           identity,run_id,manifest_id,invocation_id,role_id,
                           invocation_order,artifact_kind,content_json,output_digest,
                           evidence_json,evidence_digest,artifact_digest,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.storage._identity("autonomous-planning-artifact"),
                        run.id,
                        manifest.id,
                        invocation_id,
                        assignment.role_id,
                        assignment.invocation_order,
                        kind.value,
                        self._json(output),
                        output_digest,
                        self._json(evidence),
                        evidence_digest,
                        artifact_digest,
                        created_at,
                    ),
                )
                artifact_id = int(artifact_cursor.lastrowid)
            self.storage._event(
                "autonomous_planning.role_output_valid"
                if valid
                else "autonomous_planning.role_output_invalid",
                "autonomous_mission",
                run.mission_id,
                {
                    "run_id": run.id,
                    "manifest_id": manifest.id,
                    "invocation_id": invocation_id,
                    "artifact_id": artifact_id,
                    "role_id": assignment.role_id,
                    "attempt_number": attempt_number,
                    "context_id": context.id,
                    "context_digest": context.context_digest,
                    "authorization_decision_id": decision_id,
                    "provider_id": assignment.provider_id,
                    "model": assignment.model,
                    "valid": valid,
                    "validation_errors": list(errors),
                },
            )
        if not valid:
            return None
        row = self.storage.db.execute(
            """SELECT a.*,i.authorization_decision_id,c.context_digest
                 FROM autonomous_planning_pipeline_artifacts a
                 JOIN autonomous_planning_pipeline_invocations i
                   ON i.id=a.invocation_id
                 JOIN autonomous_planning_contexts c ON c.id=i.context_id
                WHERE a.id=?""",
            (artifact_id,),
        ).fetchone()
        return self._artifact(row)

    def _fail_run(
        self, run: PlanningPipelineRun, role_id: str, attempts: int, errors: tuple[str, ...]
    ) -> None:
        binding = {
            "run_id": run.id,
            "role_id": role_id,
            "attempt_count": attempts,
            "validation_errors": list(errors),
        }
        with self.storage.db:
            self.storage._begin_immediate()
            if self.storage.db.execute(
                "SELECT 1 FROM autonomous_planning_pipeline_failures WHERE run_id=?",
                (run.id,),
            ).fetchone():
                return
            self.storage.db.execute(
                """INSERT INTO autonomous_planning_pipeline_failures(
                       identity,run_id,role_id,attempt_count,
                       validation_errors_json,failure_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    self.storage._identity("autonomous-planning-failure"),
                    run.id,
                    role_id,
                    attempts,
                    self._json(list(errors)),
                    self._digest(binding),
                    self._timestamp(),
                ),
            )
            self.storage._event(
                "autonomous_planning.pipeline_failed",
                "autonomous_mission",
                run.mission_id,
                {**binding, "failure_digest": self._digest(binding)},
            )

    def _close_and_fail(
        self,
        run: PlanningPipelineRun,
        role_id: str,
        attempts: int,
        errors: tuple[str, ...],
        *,
        reason: str,
    ) -> None:
        authorization = self.authorizations.get_planning_authorization(
            run.planning_authorization_id
        )
        if not authorization.closed:
            try:
                self.authorizations.close_planning_authority(
                    run.planning_authorization_id,
                    actor=run.created_by,
                    command_id=f"{run.command_id}:close-planning-failed",
                    reason=reason,
                )
            except ValueError:
                if not self.authorizations.get_planning_authorization(
                    run.planning_authorization_id
                ).closed:
                    raise
        self._fail_run(run, role_id, attempts, errors)

    def _complete(
        self,
        run: PlanningPipelineRun,
        manifest: PlanningRoleModelManifest,
        artifacts: dict[str, PlanningPipelineArtifact],
        *,
        actor: str,
    ) -> PlanningPipelineRun:
        existing = self.get_run(run.id)
        if existing.status == "COMPLETED":
            return existing
        source = self.intake.get_source(manifest.specification_source_id)
        requirements = artifacts["product_requirements_analyst"].content[
            "normalized_requirements"
        ]
        architecture = artifacts["software_architect"].content[
            "architecture_proposal"
        ]
        requirement_ids = PlanningArtifactSchemas.normalized_requirements(requirements)
        PlanningArtifactSchemas.architecture(architecture, requirement_ids)
        backlog_document = artifacts["backlog_planner"].content["backlog_proposal"]
        parsed = PlanningArtifactSchemas.backlog(
            backlog_document,
            source_path="autonomous://specification",
            source_sha256=source.raw_digest,
            source_name=source.source_name,
            requirement_ids=requirement_ids,
            architecture=architecture,
        )
        artifact_bindings = {
            role_id: {
                "artifact_id": artifact.id,
                "artifact_digest": artifact.artifact_digest,
                "output_digest": artifact.output_digest,
            }
            for role_id, artifact in artifacts.items()
        }
        proposal = BacklogProposal(
            source_path="autonomous://specification",
            source_sha256=source.raw_digest,
            source_name=source.source_name,
            items=parsed.items,
            schema_version=2,
            source_metadata={
                "name": source.source_name,
                "specification_source_id": source.id,
                "specification_source_digest": source.source_digest,
                "version": source.version,
                "provenance": source.provenance,
            },
            extension_schema="agentfactory.autonomous-planning/v1",
            planning_contract={
                "schema_version": 1,
                "planning_run_id": run.id,
                "proposal_key": run.proposal_key,
                "manifest_id": manifest.id,
                "manifest_digest": manifest.manifest_digest,
                "planning_authorization_id": run.planning_authorization_id,
                "artifacts": artifact_bindings,
            },
            extensions={
                "requirements_artifact_digest": artifacts[
                    "product_requirements_analyst"
                ].artifact_digest,
                "architecture_artifact_digest": artifacts[
                    "software_architect"
                ].artifact_digest,
                "review_artifact_digest": artifacts[
                    "backlog_reviewer"
                ].artifact_digest,
            },
        )
        planner = next(
            value for value in manifest.assignments if value.role_id == "backlog_planner"
        )
        revision = self.revisions.create_revision(
            mission_id=run.mission_id,
            proposal=proposal,
            origin=BacklogRevisionOrigin.AGENT_MATERIAL,
            created_by=planner.logical_agent_id,
            command_id=f"{run.command_id}:revision",
            rationale=f"Generated by planning proposal {run.proposal_key}",
        )
        binding = self.planning.bind_revision(
            manifest.id,
            revision.id,
            actor=actor,
            command_id=f"{run.command_id}:bind-revision",
        )
        final_artifact = artifacts["backlog_reviewer"]
        completion_binding = {
            "run_id": run.id,
            "mission_id": run.mission_id,
            "manifest_id": manifest.id,
            "manifest_digest": manifest.manifest_digest,
            "manifest_revision_binding_id": binding.id,
            "revision_id": revision.id,
            "revision_digest": revision.revision_digest,
            "final_artifact_id": final_artifact.id,
            "final_artifact_digest": final_artifact.artifact_digest,
            "artifact_digests": [
                artifacts[role_id].artifact_digest
                for role_id in AUTONOMOUS_PLANNING_ROLE_IDS
            ],
        }
        completion_digest = self._digest(completion_binding)
        with self.storage.db:
            self.storage._begin_immediate()
            existing_row = self.storage.db.execute(
                "SELECT id FROM autonomous_planning_pipeline_completions WHERE run_id=?",
                (run.id,),
            ).fetchone()
            if not existing_row:
                self.storage.db.execute(
                    """INSERT INTO autonomous_planning_pipeline_completions(
                           identity,run_id,mission_id,manifest_id,
                           manifest_revision_binding_id,revision_id,revision_digest,
                           final_artifact_id,completion_digest,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.storage._identity("autonomous-planning-completion"),
                        run.id,
                        run.mission_id,
                        manifest.id,
                        binding.id,
                        revision.id,
                        revision.revision_digest,
                        final_artifact.id,
                        completion_digest,
                        self._timestamp(),
                    ),
                )
                self.storage._event(
                    "autonomous_planning.pipeline_completed",
                    "autonomous_mission",
                    run.mission_id,
                    {**completion_binding, "completion_digest": completion_digest},
                )
        return self.get_run(run.id)

    @classmethod
    def _artifact(cls, row: Any) -> PlanningPipelineArtifact:
        if not row:
            raise RuntimeError("Planning artifact row is missing")
        result = PlanningPipelineArtifact(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            manifest_id=int(row["manifest_id"]),
            invocation_id=int(row["invocation_id"]),
            role_id=str(row["role_id"]),
            invocation_order=int(row["invocation_order"]),
            artifact_kind=PlanningArtifactKind(row["artifact_kind"]),
            content=json.loads(row["content_json"]),
            output_digest=str(row["output_digest"]),
            evidence=json.loads(row["evidence_json"]),
            evidence_digest=str(row["evidence_digest"]),
            artifact_digest=str(row["artifact_digest"]),
            created_at=str(row["created_at"]),
        )
        if cls._digest(result.content) != result.output_digest:
            raise RuntimeError("Planning artifact output digest is corrupt")
        if cls._digest(result.evidence) != result.evidence_digest:
            raise RuntimeError("Planning artifact evidence digest is corrupt")
        artifact_binding = {
            "run_id": result.run_id,
            "manifest_id": result.manifest_id,
            "invocation_id": result.invocation_id,
            "context_digest": str(row["context_digest"]),
            "authorization_decision_id": int(row["authorization_decision_id"]),
            "role_id": result.role_id,
            "invocation_order": result.invocation_order,
            "artifact_kind": result.artifact_kind.value,
            "output_digest": result.output_digest,
            "evidence_digest": result.evidence_digest,
        }
        if cls._digest(artifact_binding) != result.artifact_digest:
            raise RuntimeError("Planning artifact binding digest is corrupt")
        return result

    def artifacts(self, run_id: int) -> tuple[PlanningPipelineArtifact, ...]:
        return tuple(
            self._artifact(row)
            for row in self.storage.db.execute(
                """SELECT a.*,i.authorization_decision_id,c.context_digest
                     FROM autonomous_planning_pipeline_artifacts a
                     JOIN autonomous_planning_pipeline_invocations i
                       ON i.id=a.invocation_id
                     JOIN autonomous_planning_contexts c ON c.id=i.context_id
                    WHERE a.run_id=? ORDER BY a.invocation_order""",
                (run_id,),
            )
        )

    def attempts(self, run_id: int) -> tuple[PlanningInvocationAttempt, ...]:
        return tuple(
            PlanningInvocationAttempt(
                id=int(row["id"]),
                run_id=int(row["run_id"]),
                role_id=str(row["role_id"]),
                invocation_order=int(row["invocation_order"]),
                attempt_number=int(row["attempt_number"]),
                context_id=int(row["context_id"]),
                authorization_decision_id=int(row["authorization_decision_id"]),
                provider_id=str(row["provider_id"]),
                model=str(row["model"]),
                logical_agent_id=str(row["logical_agent_id"]),
                provider_ok=bool(row["provider_ok"]),
                response_digest=str(row["response_digest"]),
                provider_metadata=json.loads(row["provider_metadata_json"]),
                valid=bool(row["valid"]),
                validation_errors=tuple(
                    json.loads(row["validation_errors_json"])
                ),
                created_at=str(row["created_at"]),
            )
            for row in self.storage.db.execute(
                """SELECT * FROM autonomous_planning_pipeline_invocations
                    WHERE run_id=? ORDER BY invocation_order,attempt_number""",
                (run_id,),
            )
        )

    def get_run(self, run_id: int) -> PlanningPipelineRun:
        row = self.storage.db.execute(
            "SELECT * FROM autonomous_planning_pipeline_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown Autonomous Planning pipeline run: {run_id}")
        completion = self.storage.db.execute(
            "SELECT * FROM autonomous_planning_pipeline_completions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        failure = self.storage.db.execute(
            "SELECT * FROM autonomous_planning_pipeline_failures WHERE run_id=?",
            (run_id,),
        ).fetchone()
        status = "COMPLETED" if completion else "FAILED" if failure else "RUNNING"
        return PlanningPipelineRun(
            id=int(row["id"]),
            identity=str(row["identity"]),
            mission_id=int(row["mission_id"]),
            manifest_id=int(row["manifest_id"]),
            planning_authorization_id=int(row["planning_authorization_id"]),
            proposal_key=str(row["proposal_key"]),
            requested_action=PlanningAction(row["requested_action"]),
            max_attempts_per_role=int(row["max_attempts_per_role"]),
            created_by=str(row["created_by"]),
            command_id=str(row["command_id"]),
            request_digest=str(row["request_digest"]),
            created_at=str(row["created_at"]),
            status=status,
            artifacts=self.artifacts(run_id),
            revision_id=int(completion["revision_id"]) if completion else None,
            revision_digest=(
                str(completion["revision_digest"]) if completion else None
            ),
            completion_digest=(
                str(completion["completion_digest"]) if completion else None
            ),
            failed_role_id=str(failure["role_id"]) if failure else None,
            failure_errors=(
                tuple(json.loads(failure["validation_errors_json"]))
                if failure
                else ()
            ),
        )

    def runs(self, mission_id: int) -> tuple[PlanningPipelineRun, ...]:
        return tuple(
            self.get_run(int(row["id"]))
            for row in self.storage.db.execute(
                """SELECT id FROM autonomous_planning_pipeline_runs
                    WHERE mission_id=? ORDER BY id""",
                (mission_id,),
            )
        )

    def revision(self, run_id: int) -> BacklogRevision:
        run = self.get_run(run_id)
        if run.revision_id is None:
            raise ValueError("Planning pipeline run has no completed revision")
        return self.revisions.get_revision(run.revision_id)
