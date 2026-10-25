"""Dev helper: print everything the evidence and activity say about one case.

Usage: python code/checks/trace_case.py case_29 [--all-time]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost.data import load_dataset  # noqa: E402
from ghost.evidence import load_evidence  # noqa: E402
from ghost.identity import IdentityIndex  # noqa: E402

ds = load_dataset()
ev = load_evidence(ds)
case_id = sys.argv[1]
all_time = "--all-time" in sys.argv
req = next(r for r in ds.requests + ds.sample_requests if r["case_id"] == case_id)
ident = IdentityIndex(ds).resolve(req["email"], req["audit_at"])
answer = next((r for r in ds.sample_output if r["case_id"] == case_id), None)
answer_res = {s["resource_id"] for s in json.loads(answer["steps_json"])} if answer else set()
print(case_id, req["reported_status"], "audit_at", req["audit_at"], "user", ident.user_id, sorted(ident.emails))
toks = ident.tokens()
seen_res = set(answer_res)
for fid, obs in ev.items():
    for o in obs:
        hit = o.identifiers() & toks
        if hit and o.resource_id:
            seen_res.add(o.resource_id)
for fid in sorted(ev):
    m = ds.manifest[fid]
    for o in ev[fid]:
        if o.kind in ("sql_scope", "json_scope"):
            continue
        hit = o.identifiers() & toks
        if not hit and o.resource_id not in seen_res:
            continue
        late = "LATE " if o.available_at > req["audit_at"] else ""
        if late and not all_time:
            continue
        body = o.text.replace("\n", " || ")[:600] if o.text else json.dumps({k: v for k, v in o.fields.items() if not k.startswith("_")}, default=str)[:600]
        print(f"  {late}{fid} {m['file_path'].split('/')[-1][:40]:<40} {o.kind:<11} {str(o.system_id):<20} {str(o.resource_id):<18} via={sorted(hit) or '-'} @ {o.available_at}\n      {body}")
for a in ds.activity:
    if a["resource_id"] in seen_res or any(t in a["details"].lower() for t in toks if not t.startswith("tel:")):
        late = "LATE " if a["event_time"] > req["audit_at"] else ""
        if late and not all_time:
            continue
        print(f"  {late}ACT {a['activity_id']} {a['event_time']} {a['event_type']:<22} {a['source_system']}->{a['target_system']} {a['resource_id']} {a['reported_result']} | {a['details'][:200]}")
