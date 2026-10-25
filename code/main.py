"""Ghost Data Still Exists: deletion-audit agent entry point.

Usage:
    python code/main.py              # same as `run`
    python code/main.py run          # full audit of all 518 requests -> output.csv at the repo root
    python code/main.py inventory    # Stage 1 summary: load all data and parse all evidence
    python code/main.py verify       # Stage 1 verification against the raw files
    python code/main.py verify-stage2  # Stage 2: request intent, identity, audit_at cutoff
    python code/main.py verify-stage3  # Stage 3: linking, state, policy, status vs sample answers
    python code/main.py validate-stage3  # Stage 3 integrity checks on all 550 cases
    python code/main.py verify-stage4  # Stage 4: steps_json vs sample answers + output.csv contract
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost.data import load_dataset  # noqa: E402
from ghost.evidence import load_evidence  # noqa: E402


def inventory() -> int:
    ds = load_dataset()
    print("Step 1 - dataset")
    for name, n in [
        ("deletion requests", len(ds.requests)),
        ("sample requests", len(ds.sample_requests)),
        ("sample answers", len(ds.sample_output)),
        ("current identities", len(ds.identities)),
        ("identity changes", len(ds.audit_log)),
        ("system activity events", len(ds.activity)),
        ("deletion rules", len(ds.rules)),
        ("evidence files", len(ds.manifest)),
    ]:
        print(f"  {name:<24}{n:>6}")

    evidence = load_evidence(ds)
    print("\nStep 2 - evidence")
    by_ext: Counter = Counter()
    kinds: Counter = Counter()
    empty, pending = [], []
    for file_id, obs in evidence.items():
        by_ext[ds.evidence_path(file_id).suffix] += 1
        kinds.update(o.kind for o in obs)
        if obs and all(o.fields.get("transcription_pending") for o in obs):
            pending.append(file_id)
        elif not any(o.resource_id or o.text or o.kind == "sql_scope" for o in obs):
            empty.append(file_id)
    print("  files by type   ", dict(sorted(by_ext.items())))
    print("  observations    ", dict(sorted(kinds.items())), "total", sum(kinds.values()))
    print("  files with nothing extracted:", empty or "none")
    print("  images waiting for transcription:", len(pending))

    # Every evidence citation in the sample answers must be traceable to something we parsed:
    # either the cited resource appears in it, or (for absence checks) the file captured that system.
    audit_ids = {r["audit_id"] for r in ds.audit_log}
    total, traced, untraced = 0, 0, []
    for row in ds.sample_output:
        for step in json.loads(row["steps_json"]):
            eid, rid = step["evidence_id"], step["resource_id"]
            if not eid:
                continue
            total += 1
            if eid in ds.activity_by_id:
                act = ds.activity_by_id[eid]
                ok = rid in (act["resource_id"], act["details"]) or rid in act["details"]
            elif eid in audit_ids:
                ok = True
            elif eid in evidence:
                obs = evidence[eid]
                ok = any(o.resource_id == rid or rid in o.text or rid in json.dumps(o.fields, default=str) for o in obs)
                if not ok and step["state"] == "ABSENT":
                    ok = any(o.kind in ("sql_scope", "json_scope") and o.system_id == step["system_id"] for o in obs)
            else:
                ok = False
            traced += ok
            if not ok:
                untraced.append((row["case_id"], eid, rid))
    print(f"\nSample citations traced to parsed evidence: {traced}/{total}")
    for item in untraced:
        print("  untraced:", item)
    return 1 if empty or untraced else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "inventory", "verify", "verify-stage2", "verify-stage3", "validate-stage3", "verify-stage4"])
    args = parser.parse_args()
    if args.command == "run":
        from ghost.pipeline import audit_all, write_output
        from ghost.paths import OUTPUT_PATH
        requests, audits = audit_all()
        n = write_output(requests, audits)
        print(f"wrote {n} rows to {OUTPUT_PATH}:", dict(Counter(audits[q["case_id"]].status for q in requests)))
        return 0
    if args.command == "inventory":
        return inventory()
    if args.command == "verify":
        from checks.verify_stage1 import run
        return run()
    if args.command == "verify-stage2":
        from checks.verify_stage2 import run
        return run()
    if args.command == "verify-stage3":
        from checks.eval_stage3 import run
        m = run()
        return 0 if m["status_ok"] == m["cases"] else 1
    if args.command == "validate-stage3":
        from checks.validate_stage3 import run
        return run()
    if args.command == "verify-stage4":
        from checks.verify_stage4 import run
        return run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
