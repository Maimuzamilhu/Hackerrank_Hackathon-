"""Stage 2 output: one CaseContext per request (intent, identity, and the evidence window)."""
from dataclasses import dataclass

from .data import Dataset
from .evidence.model import Observation
from .identity import Identity, IdentityIndex
from .intent import Intent, classify_request
from .timeline import activity_until, evidence_until


@dataclass
class CaseContext:
    request: dict
    intent: Intent
    identity: Identity | None
    activity: list[dict]                       # events at or before audit_at
    evidence: dict[str, list[Observation]]     # observations available at or before audit_at

    @property
    def case_id(self) -> str:
        return self.request["case_id"]


def build_cases(ds: Dataset, evidence: dict[str, list[Observation]], requests: list[dict]) -> list[CaseContext]:
    index = IdentityIndex(ds)
    cases = []
    for req in requests:
        intent = classify_request(req["request_text"])
        if not intent.authorizes_deletion:
            # NOT_APPLICABLE: no investigation is authorized, so nothing else is loaded.
            cases.append(CaseContext(req, intent, None, [], {}))
            continue
        audit_at = req["audit_at"]
        cases.append(CaseContext(
            request=req,
            intent=intent,
            identity=index.resolve(req["email"], audit_at),
            activity=activity_until(ds, audit_at),
            evidence=evidence_until(evidence, audit_at),
        ))
    return cases
