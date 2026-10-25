"""Stage 3 check: compare per-resource decisions and case status with the 32 sample answers.

For every resource in a sample answer, the answer encodes:
  * the required final state (its VERIFY_* operation),
  * whether it was a violation (a FINDING exists) and which operation/evidence,
  * for COMPLETE cases, the evidence proving the compliant state.
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost.audit import Auditor  # noqa: E402
from ghost.case import build_cases  # noqa: E402
from ghost.data import load_dataset  # noqa: E402
from ghost.evidence import load_evidence  # noqa: E402

VERIFY_STATE = {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
                "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"}


def run(verbose: bool = True) -> dict:
    ds = load_dataset()
    ev = load_evidence(ds)
    cases = build_cases(ds, ev, ds.sample_requests + ds.requests)
    auditor = Auditor(ds, ev, cases)
    by_id = {c.case_id: c for c in cases}
    m = Counter()
    for row in ds.sample_output:
        audit = auditor.assess(by_id[row["case_id"]])
        steps = json.loads(row["steps_json"])
        gold_req = {s["resource_id"]: VERIFY_STATE[s["operation"]] for s in steps if s["phase"] == "VERIFICATION"}
        gold_find = {s["resource_id"]: (s["operation"], s["evidence_id"]) for s in steps if s["phase"] == "FINDING"}
        gold_ver_ev = {s["resource_id"]: s["evidence_id"] for s in steps if s["phase"] == "VERIFICATION" and s["evidence_id"]}
        mine = {a.resource_id: a for a in audit.assessments}
        m["cases"] += 1
        m["status_ok"] += audit.status == row["deletion_status"]
        problems = []
        if audit.status != row["deletion_status"]:
            problems.append(f"STATUS {audit.status} vs {row['deletion_status']}")
        for rid, req in gold_req.items():
            m["gold_resources"] += 1
            a = mine.get(rid)
            if not a:
                problems.append(f"missing {rid}")
                continue
            m["found"] += 1
            m["required_ok"] += a.required == req
            if a.required != req:
                problems.append(f"{rid} required {a.required} vs {req} ({a.category})")
            gf = gold_find.get(rid)
            if (a.finding is not None) != (gf is not None) or (gf and a.finding != gf[0]):
                problems.append(f"{rid} finding {a.finding} vs {gf[0] if gf else None}")
            else:
                m["finding_ok"] += 1
            if gf:
                m["finding_ev_total"] += 1
                m["finding_ev_ok"] += a.finding_evidence == gf[1]
                if a.finding_evidence != gf[1]:
                    problems.append(f"{rid} finding evidence {a.finding_evidence} vs {gf[1]}")
            if rid in gold_ver_ev:
                m["verify_ev_total"] += 1
                m["verify_ev_ok"] += a.verify_evidence == gold_ver_ev[rid]
                if a.verify_evidence != gold_ver_ev[rid]:
                    problems.append(f"{rid} verify evidence {a.verify_evidence} vs {gold_ver_ev[rid]}")
        for rid in mine.keys() - gold_req.keys():
            m["extra"] += 1
            problems.append(f"extra {rid} ({mine[rid].how_linked}, finding={mine[rid].finding})")
        if problems and verbose:
            print(row["case_id"], row["deletion_status"], "|", "; ".join(problems))
    print(f"\nstatus {m['status_ok']}/{m['cases']} | resources found {m['found']}/{m['gold_resources']} (extra {m['extra']}) | "
          f"required state {m['required_ok']}/{m['found']} | finding decision {m['finding_ok']}/{m['found']} | "
          f"finding evidence {m['finding_ev_ok']}/{m['finding_ev_total']} | verify evidence {m['verify_ev_ok']}/{m['verify_ev_total']}")
    return m


if __name__ == "__main__":
    run()
