"""Nested planning contracts presented to a model before it generates output.

RoleRegistry describes only the outer object fields. The deterministic
validators remain authoritative; these instructions expose the nested fields
that previously became visible only after a failed inference.
"""
import json


OUTPUT_SHAPES = {
    "mission_analyst": {"mission_analysis": {
        "summary": "string", "outcomes": ["string"], "constraints": ["string"],
        "ambiguities": ["string"], "source_references": ["exact supplied source name"],
    }},
    "product_requirements_analyst": {"normalized_requirements": {
        "functional": [{"id": "REQ-001", "statement": "string",
            "acceptance_criteria": ["Observable testable outcome"],
            "source_references": ["exact supplied source name"], "priority": "P1"}],
        "non_functional": [{"id": "REQ-002", "statement": "string",
            "acceptance_criteria": ["Observable testable outcome"],
            "source_references": ["exact supplied source name"], "priority": "P1"}],
    }},
    "software_architect": {"architecture_proposal": {
        "summary": "string", "components": [{"id": "component-id", "name": "string",
            "responsibilities": ["string"], "requirement_ids": ["existing requirement ID"]}],
        "interfaces": [{"id": "interface-id", "from_component": "existing component ID",
            "to_component": "existing component ID", "contract": "string"}],
        "infrastructure": [{"id": "infrastructure-id", "name": "string", "purpose": "string",
            "bootstrap_required": True, "requirement_ids": ["existing requirement ID"]}],
        "decisions": [{"id": "ADR-001", "decision": "string", "rationale": "string",
            "requirement_ids": ["existing requirement ID"]}],
    }},
    "backlog_planner": {"backlog_proposal": {"schema_version": 2, "items": [{
        "stable_id": "DEV-001", "kind": "task", "title": "string", "description": "string",
        "parent_id": None, "dependencies": [], "priority": "P1",
        "acceptance_criteria": ["Observable testable outcome"],
        "validation_method": ["Exact deterministic check"], "required_components": ["component"],
        "required_infrastructure": ["required tool or runtime from the architecture"],
        "expected_artifacts": ["artifact"],
        "definition_of_done": ["Observable evidence"], "assigned_role": "Developer",
        "source_references": ["existing requirement ID"], "review_notes": [], "labels": [],
    }]}},
    "backlog_reviewer": {"review_report": {"verdict": "READY or NEEDS_REPAIR",
        "summary": "string", "findings": [{"id": "FINDING-001",
            "severity": "BLOCKER, HIGH, MEDIUM, LOW or INFO", "status": "OPEN, RESOLVED or ACCEPTED_RISK",
            "message": "string", "artifact_references": ["existing artifact"], "display_to_human": True}]}},
}

# Evidence has its own nested contract too. For example, an array of source
# objects is not a source_trace array of strings. The host computes digests;
# asking a model to invent them adds no evidence.
EVIDENCE_SHAPES = {
    "mission_analyst": {"source_trace": ["exact supplied source name"]},
    "product_requirements_analyst": {"traceability_matrix": [
        {"requirement_id": "existing requirement ID", "source": "exact supplied source name"}]},
    "software_architect": {"decision_trace": ["decision and its source-grounded rationale"]},
    "backlog_planner": {"dependency_evidence": []},
    "backlog_reviewer": {"findings": []},
}


def guidance(role):
    shape = OUTPUT_SHAPES.get(role)
    if shape is None:
        return ""
    return (
        " Complete response shape (both output and evidence must be JSON objects): "
        + json.dumps({"output": shape, "evidence": EVIDENCE_SHAPES[role]},
                     ensure_ascii=False, separators=(",", ":"))
        + ". Use exactly these field names. Replace descriptive string placeholders with source-grounded values. "
        "Arrays show the shape of an entry; supply the actual entries. Constraints, ambiguities, interfaces "
        "infrastructure and review findings may be empty when not needed. Include every required field. "
        "Do not invent requirement IDs or components absent from prior artifacts. "
        "Use 'Environment Bootstrap' for prerequisite infrastructure tasks and 'Developer' for development. "
        "Executable backlog items require non-empty validation_method, required_components, "
        "required_infrastructure, expected_artifacts and definition_of_done. Use actual tools/runtime "
        "and exact architecture infrastructure names, even if already installed. Put parents and dependencies "
        "before dependent items; among available items use ascending stable_id order. Every bootstrap-required "
        "infrastructure entry needs a setup task, and its consumers depend on that task. "
        "Acceptance criteria must name an observable check, using returns, rejects, equals, contains, "
        "completes, passes, fails, creates or within. For example: 'After the fifth coin the counter equals 5 "
        "and the level completes.' Avoid subjective promises or wording such as 'should work'. "
        "Keep the separate 'evidence' object and its named fields. source_trace entries must be strings, "
        "never objects. The host computes evidence digest fields; do not invent checksums. "
        "For a reviewer, evidence.findings must exactly match output.review_report.findings."
    )
