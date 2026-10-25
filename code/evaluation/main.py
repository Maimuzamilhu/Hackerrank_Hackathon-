"""Step 12: evaluation. Scores the pipeline on the 32 public sample answers, runs our own
custom cases, and summarises the full 518-request run.

Run: python code/evaluation/main.py      (the numbers go into evaluation_report.md)

Every expected value in CUSTOM_CASES was written by hand from the raw dataset files or the
public sample answers, never copied from this program's output.
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost.audit import Auditor  # noqa: E402
from ghost.case import build_cases  # noqa: E402
from ghost.data import load_dataset  # noqa: E402
from ghost.evidence import load_evidence  # noqa: E402
from ghost.paths import OUTPUT_PATH  # noqa: E402
from ghost.steps import FIELDS, build_steps  # noqa: E402

KEY_FIELDS = [f for f in FIELDS if f != "step_no"]


def key(s: dict) -> tuple:
    return tuple(s[f] for f in KEY_FIELDS)


def lcs(a: list, b: list) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            dp[i + 1][j + 1] = dp[i][j] + 1 if x == y else max(dp[i][j + 1], dp[i + 1][j])
    return dp[-1][-1]


def score_samples(ds, audits) -> dict:
    m = Counter()
    confusion = Counter()
    per_phase = {p: Counter() for p in ("FINDING", "REMEDIATION", "VERIFICATION")}
    fields = {f: Counter() for f in FIELDS}
    lcs_sum = 0.0
    misses = []
    for row in ds.sample_output:
        cid = row["case_id"]
        gold = json.loads(row["steps_json"])
        a = audits[cid]
        ours = build_steps(a)
        m["cases"] += 1
        confusion[(row["deletion_status"], a.status)] += 1
        m["status_ok"] += a.status == row["deletion_status"]
        exact = a.status == row["deletion_status"] and [key(s) for s in ours] == [key(s) for s in gold]
        m["row_exact"] += exact
        gk, ok = Counter(map(key, gold)), Counter(map(key, ours))
        m["gold_steps"] += len(gold)
        m["pred_steps"] += len(ours)
        m["step_tp"] += sum((gk & ok).values())
        for p, c in per_phase.items():
            gp = Counter(k for k in gk.elements() if k[0] == p)
            op = Counter(k for k in ok.elements() if k[0] == p)
            c["gold"] += sum(gp.values())
            c["pred"] += sum(op.values())
            c["tp"] += sum((gp & op).values())
        # field level: align each gold step to our step at the same phase + resource
        # (+ operation when possible); an unaligned gold step scores 0 on every field.
        pool = list(ours)
        for g in gold:
            t = next((s for s in pool if (s["phase"], s["operation"], s["resource_id"]) == (g["phase"], g["operation"], g["resource_id"])), None) \
                or next((s for s in pool if (s["phase"], s["resource_id"]) == (g["phase"], g["resource_id"])), None)
            if t:
                pool.remove(t)
            for f in FIELDS:
                fields[f]["total"] += 1
                fields[f]["ok"] += bool(t) and t[f] == g[f]
        # sequence: exact order, and longest common subsequence relative to the gold length
        m["seq_exact"] += [key(s) for s in ours] == [key(s) for s in gold]
        lcs_sum += lcs([key(s) for s in ours], [key(s) for s in gold]) / max(len(gold), 1)
        if not exact:
            misses.append((cid, [key(s) for s in gold if key(s) not in ok], [key(s) for s in ours if key(s) not in gk]))
    m["lcs_mean"] = lcs_sum / m["cases"]
    return {"m": m, "confusion": confusion, "per_phase": per_phase, "fields": fields, "misses": misses}


# --- Custom cases -----------------------------------------------------------------------
# Each case rewrites one existing request (same case_id, so the rest of the world stays the
# same) and states what the correct answer must be, reasoned from the raw files.

def steps_of(audit) -> list[tuple]:
    return [key(s) for s in build_steps(audit)]


def gold_steps(ds, cid) -> list[tuple]:
    return [key(s) for s in json.loads(next(r for r in ds.sample_output if r["case_id"] == cid)["steps_json"])]


CUSTOM_CASES = [
    {
        "id": "C1 historical email",
        "base": "case_29",
        "change": {"email": "noah.f4380@outlook.com"},
        "why": "Same person (user_audit_log email change), same request: the answer must not depend on which of the person's emails is supplied.",
        "expect": lambda ds, a: (a.status == "INCOMPLETE" and steps_of(a) == gold_steps(ds, "case_29"), "status + all 14 steps equal the case_29 sample answer"),
    },
    {
        "id": "C2 German deletion request",
        "base": "case_13",
        "change": {"request_text": "Bitte löschen Sie mein Kundenkonto und alle meine personenbezogenen Daten dauerhaft."},
        "why": "Deletion requested in German instead of English; nothing else changes.",
        "expect": lambda ds, a: (a.status == "INCOMPLETE" and steps_of(a) == gold_steps(ds, "case_13"), "status + all 8 steps equal the case_13 sample answer"),
    },
    {
        "id": "C3 export request (not deletion)",
        "base": "case_13",
        "change": {"request_text": "Bitte senden Sie mir eine Kopie aller Daten, die Sie über mich gespeichert haben. Mein Konto soll bestehen bleiben."},
        "why": "A German data-access/export request that explicitly keeps the account: no deletion is authorized.",
        "expect": lambda ds, a: (a.status == "NOT_APPLICABLE" and steps_of(a) == [], "NOT_APPLICABLE with []"),
    },
    {
        "id": "C4 marketing opt-out that mentions deletion",
        "base": "case_29",
        "change": {"request_text": "Please stop sending me marketing emails and delete me from the newsletter list, but do not delete my account."},
        "why": "Contains 'delete', but only asks for a marketing opt-out and explicitly keeps the account.",
        "expect": lambda ds, a: (a.status == "NOT_APPLICABLE" and steps_of(a) == [], "NOT_APPLICABLE with []"),
    },
    {
        "id": "C5 earlier audit_at (time travel)",
        "base": "case_26",
        "change": {"audit_at": "2026-09-15T11:00:00Z"},
        "why": "case_26's sample answer cites evidence_92, captured 2026-09-15T11:44:55Z. Auditing 45 minutes before that capture must not use it.",
        "expect": lambda ds, a: (
            all(s[-1] != "evidence_92" for s in steps_of(a))
            and all(ds.manifest[s[-1]]["captured_at"] <= "2026-09-15T11:00:00Z" for s in steps_of(a) if s[-1] in ds.manifest),
            "evidence_92 never cited; every cited file captured at or before the new audit_at"),
    },
    {
        "id": "C6 cache miss is not a deletion",
        "base": "case_267",
        "change": {},
        "why": "evidence_25 line 41 (09:52:27Z, before audit_at 10:00Z) reads audit_record_6621 with rows=\"1\"; the same trace has a cache_lookup result=\"miss\". The retained audit record is present and that read proves it.",
        "expect": lambda ds, a: (
            ("VERIFICATION", "security_service", "audit_record_6621", "VERIFY_RECORD_PRESENT", "PRESENT", "evidence_25") in steps_of(a),
            "VERIFY_RECORD_PRESENT audit_record_6621 cites evidence_25"),
    },
    {
        "id": "C7 restorable backup in the recovery console",
        "base": "case_55",
        "change": {},
        "why": "evidence_32 (backup console, 2026-04-29) lists backup_9287 with areed1433@gmail.com, checksum verified, no restore block; the request was never started. The backup can restore the person's data.",
        "expect": lambda ds, a: (
            a.status == "INCOMPLETE"
            and ("FINDING", "backup_service", "backup_9287", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_32") in steps_of(a)
            and ("REMEDIATION", "backup_service", "backup_9287", "BLOCK_RESTORE", "BLOCKED", "") in steps_of(a)
            and ("VERIFICATION", "backup_service", "backup_9287", "VERIFY_RESTORE_BLOCKED", "BLOCKED", "") in steps_of(a),
            "INCOMPLETE; restore risk on backup_9287 cites evidence_32, then BLOCK_RESTORE and VERIFY_RESTORE_BLOCKED"),
    },
    {
        "id": "C8 keep cue beats delete cue",
        "base": "case_13",
        "change": {"request_text": "I read that you delete inactive accounts. Please do NOT delete my account, I still use it."},
        "why": "Mentions deletion only to refuse it.",
        "expect": lambda ds, a: (a.status == "NOT_APPLICABLE" and steps_of(a) == [], "NOT_APPLICABLE with []"),
    },
]


def run_custom(ds, ev) -> list[tuple]:
    base_requests = ds.sample_requests + ds.requests
    results = []
    for c in CUSTOM_CASES:
        requests = [{**q, **c["change"]} if q["case_id"] == c["base"] else q for q in base_requests]
        cases = build_cases(ds, ev, requests)
        auditor = Auditor(ds, ev, cases)
        case = next(x for x in cases if x.case_id == c["base"])
        audit = auditor.assess(case)
        ok, expected = c["expect"](ds, audit)
        results.append((c, ok, expected, audit.status, len(build_steps(audit))))
    return results


def main() -> int:
    ds = load_dataset()
    ev = load_evidence(ds)
    cases = build_cases(ds, ev, ds.sample_requests + ds.requests)
    auditor = Auditor(ds, ev, cases)
    audits = {c.case_id: auditor.assess(c) for c in cases}

    r = score_samples(ds, audits)
    m = r["m"]
    pct = lambda a, b: f"{a}/{b} ({100 * a / b:.1f}%)" if b else "n/a"
    print("## Sample answers (32 public cases)")
    print("status accuracy       ", pct(m["status_ok"], m["cases"]))
    print("row exact (status+all steps)", pct(m["row_exact"], m["cases"]))
    p, rc = m["step_tp"] / m["pred_steps"], m["step_tp"] / m["gold_steps"]
    print(f"steps: gold {m['gold_steps']} pred {m['pred_steps']} matched {m['step_tp']}  precision {p:.3f} recall {rc:.3f} F1 {2 * p * rc / (p + rc):.3f}")
    for ph, c in r["per_phase"].items():
        print(f"  {ph:<13} gold {c['gold']:>3} pred {c['pred']:>3} matched {c['tp']:>3}")
    tot = sum(c["total"] for c in r["fields"].values())
    okf = sum(c["ok"] for c in r["fields"].values())
    print("field accuracy (all fields)", pct(okf, tot))
    for f, c in r["fields"].items():
        print(f"  {f:<12} {pct(c['ok'], c['total'])}")
    print("sequence exact        ", pct(m["seq_exact"], m["cases"]))
    print(f"sequence LCS / gold length (mean) {m['lcs_mean']:.3f}")
    print("status confusion (gold -> ours):", dict(r["confusion"]))
    for cid, missing, extra in r["misses"]:
        print(f"  miss {cid}: missing {[k[2:4] + (k[5],) for k in missing]} extra {[k[2:4] + (k[5],) for k in extra]}")

    print("\n## Custom cases")
    results = run_custom(ds, ev)
    for c, ok, expected, status, n in results:
        print(f"{'PASS' if ok else 'FAIL'}  {c['id']:<44} base {c['base']:<9} -> {status} ({n} steps) | expected: {expected}")
    print(f"custom cases passed: {sum(x[1] for x in results)}/{len(results)}")

    print("\n## Full run (output.csv)")
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        print("rows", len(rows), dict(Counter(x["deletion_status"] for x in rows)))
        claims = Counter((q["reported_status"], audits[q["case_id"]].status) for q in ds.requests)
        print("reported -> audited:", dict(sorted(claims.items())))
    return 0 if all(x[1] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
