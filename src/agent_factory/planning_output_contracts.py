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
        "required_infrastructure": [], "expected_artifacts": ["artifact"],
        "definition_of_done": ["Observable evidence"], "assigned_role": "Developer",
        "source_references": ["existing requirement ID"], "review_notes": [], "labels": [],
    }]}},
    "backlog_reviewer": {"review_report": {"verdict": "READY or NEEDS_REPAIR",
        "summary": "string", "findings": [{"id": "FINDING-001",
            "severity": "BLOCKER, HIGH, MEDIUM, LOW or INFO", "status": "OPEN, RESOLVED or ACCEPTED_RISK",
            "message": "string", "artifact_references": ["existing artifact"], "display_to_human": True}]}},
}


def guidance(role):
    shape = OUTPUT_SHAPES.get(role)
    if shape is None:
        return ""
    return (
        " Nested output contract (the shape below is for the 'output' field, not the whole envelope): "
        + json.dumps(shape, ensure_ascii=False, separators=(",", ":"))
        + ". Use exactly these field names. Replace descriptive string placeholders with source-grounded values. "
        "Arrays show the shape of an entry; supply the actual entries. Constraints, ambiguities, interfaces "
        "infrastructure and review findings may be empty when not needed. Include every required field. "
        "Do not invent requirement IDs or components absent from prior artifacts. "
        "Use 'Environment Bootstrap' for prerequisite infrastructure tasks and 'Developer' for development. "
        "Acceptance criteria must name an observable check, using returns, rejects, equals, contains, "
        "completes, passes, fails, creates or within. For example: 'After the fifth coin the counter equals 5 "
        "and the level completes.' Avoid subjective promises or wording such as 'should work'. "
        "Keep the separate 'evidence' object required by the role contract."
    )
