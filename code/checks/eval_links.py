"""Dev check: resource linking vs the sample answers (recall / precision per case)."""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost.data import load_dataset  # noqa: E402
from ghost.evidence import load_evidence  # noqa: E402
from ghost.identity import IdentityIndex  # noqa: E402
from ghost.intent import classify_request  # noqa: E402
from ghost.linking import OwnerResolver, assign_jobs, collect_links, resolve_owners  # noqa: E402

ds = load_dataset(); ev = load_evidence(ds); ix = IdentityIndex(ds)
links = collect_links(ds, ev, OwnerResolver(ds, ix))
owners = resolve_owners(links)
cases = []
for q in ds.sample_requests + ds.requests:
    if classify_request(q["request_text"]).authorizes_deletion:
        cases.append({**q, "user_id": ix.resolve(q["email"], q["audit_at"]).user_id})
jobs = assign_jobs(ds, cases, owners)
overridden = {l.resource_id for l in jobs.pop("__overridden__", [])}
by_user = defaultdict(set)
for r, (u, _) in owners.items():
    if r not in overridden:
        by_user[u].add(r)
tp = fp = fn = 0
for row in ds.sample_output:
    c = next(x for x in cases if x["case_id"] == row["case_id"])
    gold = {s["resource_id"] for s in json.loads(row["steps_json"])}
    mine = by_user[c["user_id"]] | {l.resource_id for l in jobs.get(c["case_id"], [])}
    tp += len(gold & mine); fp += len(mine - gold); fn += len(gold - mine)
    if mine - gold or gold - mine:
        print(row["case_id"], c["user_id"], "MISSING", sorted(gold - mine), "EXTRA", sorted(mine - gold))
print(f"resource recall {tp / (tp + fn):.3f} precision {tp / (tp + fp):.3f}")
total_job = sum(len(v) for v in jobs.values())
acc = sum(1 for c in cases if any(l.resource_id.startswith('account_') for l in jobs.get(c['case_id'], [])) or any(r.startswith('account_') for r in by_user[c['user_id']]))
print(f"job links {total_job}; deletion cases with a primary account linked: {acc}/{len(cases)}")
ok = bad = 0
for row in ds.sample_output:
    for l in jobs.get(row["case_id"], []):
        hit = l.resource_id in {s["resource_id"] for s in json.loads(row["steps_json"])}
        print("  job link", row["case_id"], l.resource_id, l.how, "OK" if hit else "WRONG")
