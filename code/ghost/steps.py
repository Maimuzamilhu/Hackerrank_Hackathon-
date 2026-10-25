"""Steps 9-10: turn a case audit into the ordered steps_json plan.

Order (problem_statement.md, Rules): phase FINDING -> REMEDIATION -> VERIFICATION; within a
phase the operation order of the Allowed Operations table, then system_id, then resource_id.
"""
import json

from .audit import CaseAudit

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
# Allowed Operations table, in table order: operation -> state it records.
OPERATIONS = {
    "REPORT_REMAINING_RECORD": "PRESENT",
    "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED",
    "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
OP_RANK = {op: i for i, op in enumerate(OPERATIONS)}
# Required final state -> the remediation that reaches it and the check that confirms it.
# A record that must stay PRESENT has no remediation among the allowed operations.
REMEDIATE = {"ABSENT": "DELETE_RECORD", "REDACTED": "REDACT_RECORD", "ANONYMIZED": "ANONYMIZE_RECORD",
             "DISABLED": "DISABLE_DATA_FLOW", "BLOCKED": "BLOCK_RESTORE"}
VERIFY = {"ABSENT": "VERIFY_RECORD_ABSENT", "PRESENT": "VERIFY_RECORD_PRESENT",
          "REDACTED": "VERIFY_RECORD_REDACTED", "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED",
          "DISABLED": "VERIFY_DATA_FLOW_DISABLED", "BLOCKED": "VERIFY_RESTORE_BLOCKED"}
FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def _step(phase: str, system: str, rid: str, op: str, evidence: str) -> dict:
    return {"phase": phase, "system_id": system, "resource_id": rid, "operation": op,
            "state": OPERATIONS[op], "evidence_id": evidence}


def build_steps(audit: CaseAudit) -> list[dict]:
    """Step 9. COMPLETE: one evidence-backed check per resource (a resource with nothing to
    cite gets no step: verification must prove the state). INCOMPLETE: every finding with its
    evidence, the remediation for each, then the future check for every relevant resource."""
    steps = []
    if audit.status == "COMPLETE":
        for a in audit.assessments:
            if a.verify_evidence:
                steps.append(_step("VERIFICATION", a.system_id, a.resource_id, VERIFY[a.required], a.verify_evidence))
    elif audit.status == "INCOMPLETE":
        for a in audit.assessments:
            if a.finding:
                steps.append(_step("FINDING", a.system_id, a.resource_id, a.finding, a.finding_evidence))
                steps.append(_step("REMEDIATION", a.system_id, a.resource_id, REMEDIATE[a.required], ""))
            steps.append(_step("VERIFICATION", a.system_id, a.resource_id, VERIFY[a.required], ""))
    return order_steps(steps)


def order_steps(steps: list[dict]) -> list[dict]:
    """Step 10: sort by phase, operation, system_id, resource_id; number from 1."""
    steps = sorted(steps, key=lambda s: (PHASES.index(s["phase"]), OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    return [{k: (i if k == "step_no" else s[k]) for k in FIELDS} for i, s in enumerate(steps, 1)]


def to_json(steps: list[dict]) -> str:
    """Deterministic compact serialization in the documented field order."""
    return json.dumps(steps, separators=(",", ":"), ensure_ascii=False)
