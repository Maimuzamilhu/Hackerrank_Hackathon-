"""Steps 1-11 end to end: load, parse, audit every request, write output.csv."""
import csv
from pathlib import Path

from .audit import Auditor, CaseAudit
from .case import build_cases
from .data import load_dataset
from .evidence import load_evidence
from .paths import OUTPUT_PATH
from .steps import build_steps, to_json


def case_key(q: dict) -> tuple[int, str]:
    num = q["case_id"].rsplit("_", 1)[-1]
    return (int(num), q["case_id"]) if num.isdigit() else (10**9, q["case_id"])


def audit_all() -> tuple[list[dict], dict[str, CaseAudit]]:
    """Audit the evaluation and the public sample requests together: they share one world
    (the same activity stream and evidence), so deletion jobs are assigned across all of them."""
    ds = load_dataset()
    ev = load_evidence(ds)
    cases = build_cases(ds, ev, ds.sample_requests + ds.requests)
    auditor = Auditor(ds, ev, cases)
    return ds.requests, {c.case_id: auditor.assess(c) for c in cases}


def write_output(requests: list[dict], audits: dict[str, CaseAudit], path: Path = OUTPUT_PATH) -> int:
    """Step 11: one row per evaluation request, sorted by case_id the way the dataset itself
    lists them (numerically: case_33 ... case_550, not case_100 before case_33)."""
    rows = sorted(requests, key=case_key)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for q in rows:
            a = audits[q["case_id"]]
            w.writerow([q["case_id"], a.status, to_json(build_steps(a))])
    return len(rows)
