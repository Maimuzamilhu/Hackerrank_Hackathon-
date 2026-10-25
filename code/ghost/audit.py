"""Steps 6-8: for each deletion request, which resources are relevant, what policy requires
for each one, what state it is in at audit_at, and whether the deletion is complete.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .case import CaseContext
from .data import Dataset
from .evidence.model import Observation
from .linking import Link, OwnerResolver, assign_jobs, catalog_aliases, collect_links, resolve_owners
from .identity import IdentityIndex
from .state import Signal, absence_signals, alias_signals, document_signals, signal_of
from .systems import resource_prefix, system_for_resource

# Default data category per resource family (deletion_rules.csv names).
PREFIX_CATEGORY = {
    "account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "cache": "CACHE_ENTRY",
    "support": "SUPPORT_MESSAGE", "analytics": "ANALYTICS_PROFILE", "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE", "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG", "legal_hold": "LEGAL_HOLD_RECORD", "backup": "BACKUP_ARCHIVE",
    "tombstone": "ACCOUNT_TOMBSTONE", "data_flow": "DATA_FLOW", "object": "SUPPORT_ATTACHMENT",
}
# What the final state must be for each required action.
ACTION_STATE = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}
# The workflow's own action names, used only when the evidence does not state a category.
WORKER_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}


@dataclass
class Assessment:
    resource_id: str
    system_id: str
    category: str
    required: str                 # ABSENT | PRESENT | REDACTED | ANONYMIZED | DISABLED | BLOCKED
    finding: str | None           # REPORT_* operation when the current state violates policy
    finding_evidence: str = ""
    verify_evidence: str = ""     # evidence proving the compliant state (COMPLETE cases)
    how_linked: str = ""
    signals: list[Signal] = field(default_factory=list)


@dataclass
class CaseAudit:
    case_id: str
    status: str                   # COMPLETE | INCOMPLETE | NOT_APPLICABLE
    assessments: list[Assessment] = field(default_factory=list)


class Auditor:
    def __init__(self, ds: Dataset, evidence: dict[str, list[Observation]], cases: list[CaseContext]):
        self.ds, self.evidence = ds, evidence
        index = IdentityIndex(ds)
        self.links = collect_links(ds, evidence, OwnerResolver(ds, index))
        self.owners = resolve_owners(self.links)
        job_cases = [{**c.request, "user_id": c.identity.user_id} for c in cases if c.identity]
        self.jobs = assign_jobs(ds, job_cases, self.owners)
        self.overridden = {link.resource_id for link in self.jobs.pop("__overridden__", [])}
        self.by_user: dict[str, list[str]] = defaultdict(list)
        for rid, (owner, _) in self.owners.items():
            if rid not in self.overridden:
                self.by_user[owner].append(rid)
        # previous request of the same person, to keep each request to its own resources
        self.prev_audit: dict[str, str] = {}
        self.next_audit: dict[str, str] = {}
        per_user = defaultdict(list)
        for c in cases:
            if c.identity:
                per_user[c.identity.user_id].append(c)
        for cs in per_user.values():
            cs.sort(key=lambda c: c.request["audit_at"])
            for earlier, later in zip(cs, cs[1:]):
                self.prev_audit[later.case_id] = earlier.request["audit_at"]
                self.next_audit[earlier.case_id] = later.request["audit_at"]
        self.obs_by_res: dict[str, list[Observation]] = defaultdict(list)
        for obs in evidence.values():
            for o in obs:
                if o.resource_id:
                    self.obs_by_res[o.resource_id].append(o)
        self.origin = {fid: m["origin"] for fid, m in ds.manifest.items()}
        self.aliases = catalog_aliases(ds)
        self.worker_action: dict[str, str] = {}
        for a in ds.activity:
            if a["source_system"] == "deletion_worker" and a["event_type"] in WORKER_ACTION:
                self.worker_action[a["resource_id"]] = WORKER_ACTION[a["event_type"]]

    def relevant(self, case: CaseContext) -> dict[str, str]:
        """resource_id -> how it is linked to this request."""
        uid, audit_at = case.identity.user_id, case.request["audit_at"]
        since = self.prev_audit.get(case.case_id, "")
        has_later = case.case_id in self.next_audit
        out = {}
        for link in self.jobs.get(case.case_id, []):
            out[link.resource_id] = link.how
        for rid in self.by_user.get(uid, []):
            if rid in out:
                continue
            times = [l.time for l in self.owners[rid][1]] + [o.available_at for o in self.obs_by_res.get(rid, [])]
            # A person with an earlier request: resources last seen before that request
            # was audited belong to the earlier request.
            if since and max(times) <= since:
                continue
            # ...and resources first seen after this request was audited belong to the later one.
            if has_later and min(times) > audit_at:
                continue
            out[rid] = self.owners[rid][1][0].how
        return out

    def assess(self, case: CaseContext) -> CaseAudit:
        if not case.intent.authorizes_deletion:
            return CaseAudit(case.case_id, "NOT_APPLICABLE")
        audit_at = case.request["audit_at"]
        visible = [o for obs in case.evidence.values() for o in obs]
        result = CaseAudit(case.case_id, "COMPLETE")
        for rid, how in sorted(self.relevant(case).items()):
            system = system_for_resource(rid)
            sigs = [s for o in self.obs_by_res.get(rid, []) if o.available_at <= audit_at and (s := signal_of(o))]
            sigs += absence_signals(visible, rid, system, case.identity.tokens())
            sigs += document_signals(visible, rid, system, self.origin)
            if rid in self.aliases:
                sigs += alias_signals(visible, rid, self.aliases[rid])
            sigs.sort(key=lambda s: (s.time, s.present is True))
            a = self._judge(rid, system, sigs)
            a.how_linked = how
            result.assessments.append(a)
            if a.finding:
                result.status = "INCOMPLETE"
        return result

    def _category(self, rid: str, sigs: list[Signal]) -> str:
        stated = [s.category for s in sigs if s.category]
        if any(s.attachment for s in sigs):
            return "SUPPORT_ATTACHMENT"
        if stated:
            return stated[-1]
        prefix = resource_prefix(rid)
        if self.worker_action.get(rid) == "DELETE" and prefix in ("analytics", "support"):
            # The record is not the kind policy keeps (e.g. a contact record keyed by an old
            # identifier); the workflow deletes it, and nothing in the evidence says otherwise.
            return "SECONDARY_IDENTIFIER"
        return PREFIX_CATEGORY.get(prefix, "USER_PROFILE")

    @staticmethod
    def _proof(required: str, sigs: list[Signal]) -> str:
        """Latest evidence that positively shows the required state (never a mere absence
        for a record that must exist)."""
        def shows(s: Signal) -> bool:
            if required == "ABSENT":
                return s.present is False
            if required == "PRESENT":
                return s.present is True
            if required in ("REDACTED", "ANONYMIZED"):
                return s.present is True and s.exposed is False
            return s.active is False   # DISABLED / BLOCKED
        hit = next((s for s in reversed(sigs) if shows(s)), None)
        return hit.file_id if hit else ""

    def _judge(self, rid: str, system: str, sigs: list[Signal]) -> Assessment:
        category = self._category(rid, sigs)
        rule = self.ds.rules.get(category)
        latest = sigs[-1] if sigs else None
        last_present = next((s for s in reversed(sigs) if s.present), None)
        if category == "DATA_FLOW":
            required = "DISABLED"
            state_sig = next((s for s in reversed(sigs) if s.active is not None), None)
            bad = state_sig is not None and state_sig.active
            op = "REPORT_ACTIVE_DATA_FLOW"
        elif category == "BACKUP_ARCHIVE":
            required = "BLOCKED"
            state_sig = next((s for s in reversed(sigs) if s.active is not None), None)
            bad = state_sig is not None and state_sig.active
            op = "REPORT_RESTORE_RISK"
        else:
            action = rule["required_action"] if rule else "DELETE"
            required = ACTION_STATE[action]
            state_sig = latest
            if required == "ABSENT":
                bad, op = bool(latest and latest.present), "REPORT_REMAINING_RECORD"
            elif required in ("REDACTED", "ANONYMIZED"):
                state_sig = last_present if latest and latest.present else latest
                bad = bool(latest and latest.present and last_present and last_present.exposed)
                op = "REPORT_EXPOSED_PII"
            else:  # PRESENT: retained records; nothing to remediate with the allowed operations
                bad, op = False, None
        a = Assessment(rid, system, category, required, op if bad else None)
        if bad:
            a.finding_evidence = state_sig.file_id
        else:
            a.verify_evidence = self._proof(required, sigs)
        a.signals = sigs
        return a
