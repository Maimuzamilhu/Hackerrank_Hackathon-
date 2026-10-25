"""Stage 3 validation on ALL cases (not just samples): integrity, time rules, over-fitting.

Run: python code/main.py validate-stage3
"""
import re
from collections import Counter, defaultdict

from ghost.audit import Auditor
from ghost.case import build_cases
from ghost.data import load_dataset
from ghost.evidence import load_evidence
from ghost.linking import JOB_WINDOWS, DEFAULT_WINDOW, _minutes, job_window
from ghost.systems import system_for_resource


class Report:
    def __init__(self):
        self.failures, self.warnings, self.passes = [], [], 0

    def check(self, ok, msg, warn=False):
        if ok:
            self.passes += 1
        else:
            (self.warnings if warn else self.failures).append(msg)


def mentions(obs, rid, alias=None) -> bool:
    """The file refers to the resource: as a row, a mention, or by its catalog alias."""
    names = [rid] + ([alias] if alias else [])
    return any(o.resource_id == rid or rid in o.fields.get("mentioned_resources", [])
               or any(re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", o.text) for n in names) for o in obs)


def run() -> int:
    r = Report()
    ds = load_dataset()
    ev = load_evidence(ds)
    cases = build_cases(ds, ev, ds.sample_requests + ds.requests)
    auditor = Auditor(ds, ev, cases)
    sample_ids = {q["case_id"] for q in ds.sample_requests}
    audits = {c.case_id: auditor.assess(c) for c in cases}

    print("[A] Evidence rules (every case)")
    late_cites = wrong_cites = 0
    for c in cases:
        a = audits[c.case_id]
        audit_at = c.request["audit_at"]
        for x in a.assessments:
            for eid, kind in ((x.finding_evidence, "finding"), (x.verify_evidence, "verify")):
                if not eid:
                    continue
                cap = ds.manifest[eid]["captured_at"] if eid in ds.manifest else None
                ok_time = cap is not None and cap <= audit_at
                late_cites += not ok_time
                r.check(ok_time, f"{c.case_id}: {kind} cites {eid} captured {cap} after audit_at {audit_at}")
                # the cited file must say something about this resource (row, mention, or a capture of its system)
                obs = ev.get(eid, [])
                ok_ref = mentions(obs, x.resource_id, auditor.aliases.get(x.resource_id)) or any(o.kind in ("sql_scope", "json_scope") and o.system_id == x.system_id for o in obs)
                wrong_cites += not ok_ref
                r.check(ok_ref, f"{c.case_id}: {kind} cites {eid} which never refers to {x.resource_id}")
            r.check(not (x.finding and not x.finding_evidence), f"{c.case_id}: finding on {x.resource_id} without evidence")
            sig_late = [s for s in x.signals if s.time > audit_at and s.file_id in ds.manifest and ds.manifest[s.file_id]["captured_at"] > audit_at]
            r.check(not sig_late, f"{c.case_id}: {x.resource_id} judged with evidence after audit_at")
    print(f"  citations after audit_at: {late_cites}; citations that never mention the resource: {wrong_cites}")

    print("\n[B] Case-level consistency")
    status = Counter(a.status for cid, a in audits.items() if cid not in sample_ids)
    print("  evaluation statuses:", dict(status))
    for c in cases:
        a = audits[c.case_id]
        if a.status == "NOT_APPLICABLE":
            r.check(not a.assessments, f"{c.case_id}: NOT_APPLICABLE with resources")
            continue
        r.check(bool(a.assessments), f"{c.case_id}: deletion case with no resources at all")
        r.check((a.status == "INCOMPLETE") == any(x.finding for x in a.assessments), f"{c.case_id}: status disagrees with findings")
        accounts = [x for x in a.assessments if x.resource_id.startswith("account_")]
        r.check(len(accounts) <= 1, f"{c.case_id}: {len(accounts)} primary accounts")
        r.check(len(accounts) == 1, f"{c.case_id}: no primary account linked", warn=True)
        for x in a.assessments:
            r.check(x.system_id == system_for_resource(x.resource_id), f"{c.case_id}: {x.resource_id} system {x.system_id}")
            if a.status == "COMPLETE":
                r.check(bool(x.verify_evidence), f"{c.case_id}: COMPLETE but {x.resource_id} has no evidence to cite", warn=True)

    print("\n[C] One resource, one request")
    owners = defaultdict(set)
    for cid, a in audits.items():
        for x in a.assessments:
            owners[x.resource_id].add(cid)
    user_of = {c.case_id: c.identity.user_id for c in cases if c.identity}
    shared = {rid: cs for rid, cs in owners.items() if len(cs) > 1}
    for rid, cs in shared.items():
        r.check(len({user_of[cid] for cid in cs}) == 1, f"{rid} assigned to requests of different people: {sorted(cs)}")
    print(f"  resources in more than one request: {len(shared)} (same person: {sum(1 for cs in shared.values() if len({user_of[x] for x in cs}) == 1)})")

    print("\n[D] Over-fitting check on tuned job windows (all deletion-worker events)")
    deletion_cases = [c for c in cases if c.identity]
    events = [a for a in ds.activity if a["source_system"] == "deletion_worker"]
    no_case = []
    for a in events:
        lo, hi = job_window(a["details"])
        fits = [c for c in deletion_cases if lo <= _minutes(a["event_time"], c.request["reported_at"]) <= hi
                and a["event_time"] >= c.request["requested_at"]]
        if not fits:
            no_case.append(a)
    print(f"  deletion-worker events: {len(events)}; outside every request's window: {len(no_case)}")
    for a in no_case[:8]:
        print("   ", a["activity_id"], a["event_time"], a["event_type"], a["resource_id"], "|", a["details"][:70])
    r.check(len(no_case) <= len(events) * 0.02, f"{len(no_case)} job events fit no request: windows may be over-fitted", warn=True)
    near_edge = 0
    for a in events:
        lo, hi = job_window(a["details"])
        offs = [_minutes(a["event_time"], c.request["reported_at"]) for c in deletion_cases]
        near_edge += any(lo - 3 <= o < lo or hi < o <= hi + 3 for o in offs) and not any(lo <= o <= hi for o in offs)
    print(f"  events within 3 min outside a window edge (would flip with a wider window): {near_edge}")

    print("\n[E] Link strength behind each finding (weak links are riskier)")
    fl = Counter((x.how_linked, x.finding) for a in audits.values() for x in a.assessments if x.finding)
    for k, v in sorted(fl.items(), key=str):
        print(f"  {k[0]:<10} {k[1]:<26} {v}")
    guesses = [(cid, x.resource_id) for cid, a in audits.items() for x in a.assessments if x.how_linked == "job_guess" and x.finding]
    r.check(not guesses, f"{len(guesses)} findings rest on a guessed link: {guesses[:5]}", warn=True)

    print("\n[F] Workflow claim vs evidence (evaluation set)")
    claims = Counter((c.request["reported_status"], audits[c.case_id].status) for c in cases if c.case_id not in sample_ids)
    for k, v in sorted(claims.items()):
        print(f"  reported {k[0]:<19} -> audited {k[1]:<15} {v}")

    print(f"\nPASSED {r.passes} checks, {len(r.failures)} failures, {len(r.warnings)} warnings")
    for f in r.failures[:30]:
        print("  FAIL", f)
    for w in Counter(re.sub(r"case_\d+|[a-z_]+_\d+", "*", w) for w in r.warnings).most_common():
        print(f"  WARN x{w[1]}: {w[0]}")
    return 1 if r.failures else 0
