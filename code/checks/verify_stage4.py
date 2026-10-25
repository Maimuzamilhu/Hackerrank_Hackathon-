"""Stage 4 checks: steps_json against the 32 sample answers (exact steps, fields, order),
then the format contract on the written output.csv.

Run: python code/main.py verify-stage4
"""
import csv
import json
from collections import Counter

from ghost.audit import Auditor
from ghost.case import build_cases
from ghost.data import load_dataset
from ghost.evidence import load_evidence
from ghost.paths import OUTPUT_PATH
from ghost.steps import FIELDS, OPERATIONS, PHASES, build_steps, order_steps, to_json

PHASE_OF = {op: ("FINDING" if op.startswith("REPORT_") else "VERIFICATION" if op.startswith("VERIFY_") else "REMEDIATION")
            for op in OPERATIONS}


def key(s: dict) -> tuple:
    return tuple(s[k] for k in FIELDS if k != "step_no")


def sample_comparison(ds, audits) -> dict:
    m = Counter()
    for row in ds.sample_output:
        gold = json.loads(row["steps_json"])
        a = audits[row["case_id"]]
        ours = build_steps(a)
        m["cases"] += 1
        m["status_ok"] += a.status == row["deletion_status"]
        m["exact_case"] += [key(s) for s in ours] == [key(s) for s in gold]
        gk, ok = Counter(key(s) for s in gold), Counter(key(s) for s in ours)
        m["gold_steps"] += sum(gk.values())
        m["our_steps"] += sum(ok.values())
        m["steps_matched"] += sum((gk & ok).values())
        # field-level: align by (phase, operation, resource) and compare the other fields
        by = {(s["phase"], s["operation"], s["resource_id"]): s for s in ours}
        for s in gold:
            t = by.get((s["phase"], s["operation"], s["resource_id"]))
            for f in ("system_id", "state", "evidence_id"):
                m[f"field_total"] += 1
                m[f"field_ok"] += bool(t) and t[f] == s[f]
        # sequence: our order applied to the gold set must equal the gold order
        m["gold_order_ok"] += [key(s) for s in order_steps(gold)] == [key(s) for s in gold]
        if [key(s) for s in ours] != [key(s) for s in gold]:
            missing = [f"{s['phase'][:4]} {s['operation']} {s['resource_id']} {s['evidence_id']!r}" for s in gold if key(s) not in ok]
            extra = [f"{s['phase'][:4]} {s['operation']} {s['resource_id']} {s['evidence_id']!r}" for s in ours if key(s) not in gk]
            m.setdefault("diffs", []).append((row["case_id"], missing, extra))
    return m


def contract(ds, path=OUTPUT_PATH) -> list[str]:
    errors = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    if header != ["case_id", "deletion_status", "steps_json"]:
        errors.append(f"header {header}")
    expected = [q["case_id"] for q in ds.requests]
    got = [r[0] for r in rows]
    if sorted(got) != sorted(expected) or len(got) != len(set(got)):
        errors.append(f"rows {len(got)} vs requests {len(expected)}")
    if got != sorted(got, key=lambda c: int(c.rsplit("_", 1)[1])):
        errors.append("rows not sorted by case_id")
    audit_at = {q["case_id"]: q["audit_at"] for q in ds.requests}
    known = set(ds.manifest) | set(ds.activity_by_id) | {r["audit_id"] for r in ds.audit_log}
    for cid, status, sj in rows:
        steps = json.loads(sj)
        where = f"{cid}:"
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{where} status {status}")
        if (status == "NOT_APPLICABLE") != (steps == []):
            errors.append(f"{where} NOT_APPLICABLE iff []")
        if status == "COMPLETE" and not steps:
            errors.append(f"{where} COMPLETE with no verification steps")
        if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
            errors.append(f"{where} step numbers")
        if order_steps(steps) != steps:
            errors.append(f"{where} order")
        if to_json(steps) != sj:
            errors.append(f"{where} not canonical JSON")
        phases = set()
        for s in steps:
            if list(s) != FIELDS:
                errors.append(f"{where} fields {list(s)}")
                continue
            op = s["operation"]
            if op not in OPERATIONS or s["state"] != OPERATIONS[op] or s["phase"] != PHASE_OF[op] or s["phase"] not in PHASES:
                errors.append(f"{where} bad step {s}")
            phases.add(s["phase"])
            eid = s["evidence_id"]
            cites = s["phase"] == "FINDING" or status == "COMPLETE"
            if cites and not eid:
                errors.append(f"{where} {op} {s['resource_id']} must cite evidence")
            if not cites and eid:
                errors.append(f"{where} future step {op} cites {eid}")
            if eid and eid not in known:
                errors.append(f"{where} unknown evidence {eid}")
            if eid in ds.manifest and ds.manifest[eid]["captured_at"] > audit_at[cid]:
                errors.append(f"{where} {eid} captured after audit_at")
        if status == "COMPLETE" and phases - {"VERIFICATION"}:
            errors.append(f"{where} COMPLETE with {phases}")
        if status == "INCOMPLETE" and not {"FINDING", "REMEDIATION", "VERIFICATION"} <= phases:
            errors.append(f"{where} INCOMPLETE missing a phase: {phases}")
        # every remediated resource is verified afterwards
        fixed = {s["resource_id"] for s in steps if s["phase"] == "REMEDIATION"}
        checked = {s["resource_id"] for s in steps if s["phase"] == "VERIFICATION"}
        if fixed - checked:
            errors.append(f"{where} remediated but not verified: {fixed - checked}")
    return errors


def run() -> int:
    ds = load_dataset()
    ev = load_evidence(ds)
    cases = build_cases(ds, ev, ds.sample_requests + ds.requests)
    auditor = Auditor(ds, ev, cases)
    audits = {c.case_id: auditor.assess(c) for c in cases if c.case_id in {q["case_id"] for q in ds.sample_requests}}

    m = sample_comparison(ds, audits)
    print("[A] Sample answers (32 public cases)")
    print(f"  status                  {m['status_ok']}/{m['cases']}")
    print(f"  cases exactly right     {m['exact_case']}/{m['cases']}")
    print(f"  steps matched           {m['steps_matched']}/{m['gold_steps']}  (we wrote {m['our_steps']})")
    print(f"  fields matched          {m['field_ok']}/{m['field_total']}")
    print(f"  ordering rule on gold   {m['gold_order_ok']}/{m['cases']}  (our sort reproduces the sample order)")
    for cid, missing, extra in m.get("diffs", []):
        print(f"  {cid}: missing {missing} extra {extra}")

    print(f"\n[B] Output contract on {OUTPUT_PATH.name}")
    if not OUTPUT_PATH.exists():
        print("  output.csv not written yet: run `python code/main.py run` first")
        return 1
    errors = contract(ds)
    with open(OUTPUT_PATH, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print("  rows", len(rows), dict(Counter(r["deletion_status"] for r in rows)))
    ops = Counter(s["operation"] for r in rows for s in json.loads(r["steps_json"]))
    print("  steps", sum(ops.values()), dict(sorted(ops.items(), key=lambda kv: list(OPERATIONS).index(kv[0]))))
    print(f"  contract errors: {len(errors)}")
    for e in errors[:20]:
        print("   ", e)
    return 1 if errors or m["gold_order_ok"] != m["cases"] else 0
