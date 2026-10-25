"""Stage 1 verification: prove the loader and evidence parsers lose nothing.

Every check compares what the code produced against an independent reading of the raw
files (raw line counts, raw INSERT counts, raw regex scans), so a parser bug cannot hide
behind itself. Run:  python code/main.py verify
"""
import csv
import json
import re
from collections import Counter, defaultdict

from ghost.data import load_dataset
from ghost.evidence import load_evidence
from ghost.evidence.logs import parse_line
from ghost.paths import DATASET_DIR, EVIDENCE_DIR
from ghost.systems import PREFIX_SYSTEM, resource_prefix

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RESOURCE_TOKEN = re.compile(r"\b(?:account|analytics|audit_record|backup|cache|data_flow|email_delivery|export|legal_hold|marketing|object|search|support|tombstone)_\d+\b")

EXPECTED_HEADERS = {
    "deletion_requests.csv": "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text",
    "sample_deletion_requests.csv": "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text",
    "sample_output.csv": "case_id,deletion_status,steps_json",
    "output.csv": "case_id,deletion_status,steps_json",
    "user_identities.csv": "user_id,name,email,phone,email_hash,updated_at",
    "user_audit_log.csv": "audit_id,user_id,event_time,field_name,old_value,new_value",
    "system_activity.csv": "activity_id,event_time,event_type,source_system,target_system,resource_id,reported_result,details",
    "deletion_rules.csv": "rule_id,data_category,condition,required_action",
    "evidence_files.csv": "file_id,file_path,origin,captured_at",
}
TIME_COLUMNS = {
    "deletion_requests.csv": ["requested_at", "reported_at", "audit_at"],
    "sample_deletion_requests.csv": ["requested_at", "reported_at", "audit_at"],
    "user_identities.csv": ["updated_at"],
    "user_audit_log.csv": ["event_time"],
    "system_activity.csv": ["event_time"],
    "evidence_files.csv": ["captured_at"],
}
ID_COLUMNS = {
    "deletion_requests.csv": "case_id", "sample_deletion_requests.csv": "case_id", "sample_output.csv": "case_id",
    "user_identities.csv": "user_id", "user_audit_log.csv": "audit_id", "system_activity.csv": "activity_id",
    "deletion_rules.csv": "rule_id", "evidence_files.csv": "file_id",
}


class Report:
    def __init__(self):
        self.failures, self.warnings, self.passes = [], [], 0

    def check(self, ok: bool, msg: str, warn: bool = False):
        if ok:
            self.passes += 1
        else:
            (self.warnings if warn else self.failures).append(msg)
        return ok


def verify_csvs(r: Report):
    print("\n[1] CSV files")
    for name, header in EXPECTED_HEADERS.items():
        path = DATASET_DIR / name
        raw = path.read_bytes()
        r.check(not raw.startswith(b"\xef\xbb\xbf"), f"{name}: has a UTF-8 BOM")
        text = raw.decode("utf-8")  # raises if not UTF-8
        first = text.splitlines()[0]
        r.check(first == header, f"{name}: header {first!r} != expected")
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        physical = len(text.splitlines()) - 1
        # A row count below the physical line count would mean quoted multi-line fields.
        r.check(len(rows) == physical, f"{name}: {len(rows)} rows vs {physical} physical lines (multi-line fields?)", warn=True)
        bad_width = [i for i, row in enumerate(rows) if None in row or any(v is None for v in row.values())]
        r.check(not bad_width, f"{name}: {len(bad_width)} rows with wrong column count")
        if name in ID_COLUMNS:
            ids = [row[ID_COLUMNS[name]] for row in rows]
            dupes = [k for k, v in Counter(ids).items() if v > 1]
            r.check(not dupes, f"{name}: duplicate ids {dupes[:5]}")
        for col in TIME_COLUMNS.get(name, []):
            bad = [row[col] for row in rows if not ISO_Z.match(row[col])]
            r.check(not bad, f"{name}.{col}: {len(bad)} non ISO-Z timestamps e.g. {bad[:3]}")
        blanks = Counter(k for row in rows for k, v in row.items() if v == "")
        print(f"  {name:<30} rows={len(rows):<5} blanks={dict(blanks) or '-'}")


def verify_loader(r: Report, ds):
    print("\n[2] Loader vs raw CSV")
    raw = lambda n: list(csv.DictReader(open(DATASET_DIR / n, newline="", encoding="utf-8")))  # noqa: E731
    pairs = [
        ("deletion_requests.csv", ds.requests), ("sample_deletion_requests.csv", ds.sample_requests),
        ("sample_output.csv", ds.sample_output), ("user_identities.csv", list(ds.identities.values())),
        ("user_audit_log.csv", ds.audit_log), ("system_activity.csv", ds.activity),
        ("deletion_rules.csv", list(ds.rules.values())), ("evidence_files.csv", list(ds.manifest.values())),
    ]
    for name, loaded in pairs:
        rows = raw(name)
        key = lambda row: json.dumps(row, sort_keys=True)  # noqa: E731
        same = sorted(map(key, rows)) == sorted(map(key, loaded))
        r.check(same, f"{name}: loaded rows differ from raw rows")
        print(f"  {name:<30} raw={len(rows):<5} loaded={len(loaded):<5} identical={same}")
    r.check(len(ds.requests) == 518, f"expected 518 requests, got {len(ds.requests)}")
    r.check(len(ds.rules) == 15, "deletion_rules data_category is not unique (dict would drop rules)")
    r.check(all(ds.activity[i]["event_time"] <= ds.activity[i + 1]["event_time"] for i in range(len(ds.activity) - 1)), "activity not time-sorted")
    r.check(all(ds.audit_log[i]["event_time"] <= ds.audit_log[i + 1]["event_time"] for i in range(len(ds.audit_log) - 1)), "audit log not time-sorted")

    print("\n[3] Value domains and cross-references")
    domains = {
        "reported_status": Counter(x["reported_status"] for x in ds.requests + ds.sample_requests),
        "event_type": Counter(x["event_type"] for x in ds.activity),
        "reported_result": Counter(x["reported_result"] for x in ds.activity),
        "audit field_name": Counter(x["field_name"] for x in ds.audit_log),
        "required_action": Counter(x["required_action"] for x in ds.rules.values()),
        "sample deletion_status": Counter(x["deletion_status"] for x in ds.sample_output),
    }
    for k, v in domains.items():
        print(f"  {k:<24} {dict(sorted(v.items()))}")
    r.check(set(domains["reported_status"]) <= {"COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"}, "unexpected reported_status")
    r.check(set(domains["required_action"]) <= {"DELETE", "REDACT", "ANONYMIZE", "RETAIN"}, "unexpected required_action")

    unknown_users = {a["user_id"] for a in ds.audit_log} - set(ds.identities)
    r.check(not unknown_users, f"audit log references unknown users {sorted(unknown_users)[:5]}")
    for row in ds.requests + ds.sample_requests:
        r.check(row["requested_at"] <= row["audit_at"], f"{row['case_id']}: requested_at after audit_at", warn=True)

    # Can every request email be resolved to a user from current or historical emails?
    current = {u["email"].lower(): uid for uid, u in ds.identities.items()}
    historic = {a[k].lower(): a["user_id"] for a in ds.audit_log if a["field_name"] == "email" for k in ("old_value", "new_value") if a[k]}
    for label, reqs in (("evaluation", ds.requests), ("sample", ds.sample_requests)):
        cur = sum(1 for x in reqs if x["email"].lower() in current)
        old = sum(1 for x in reqs if x["email"].lower() not in current and x["email"].lower() in historic)
        none = [x["case_id"] for x in reqs if x["email"].lower() not in current and x["email"].lower() not in historic]
        print(f"  {label} request emails: current={cur} historical-only={old} unresolved={len(none)} {none[:6]}")

    steps_ok = True
    for row in ds.sample_output:
        try:
            steps = json.loads(row["steps_json"])
            steps_ok &= isinstance(steps, list)
        except json.JSONDecodeError:
            steps_ok = False
    r.check(steps_ok, "sample_output steps_json not all valid JSON arrays")


def verify_manifest(r: Report, ds):
    print("\n[4] Evidence manifest vs disk")
    on_disk = {p.relative_to(DATASET_DIR).as_posix() for p in EVIDENCE_DIR.rglob("*") if p.is_file()}
    listed = {m["file_path"] for m in ds.manifest.values()}
    r.check(listed <= on_disk, f"manifest lists missing files {sorted(listed - on_disk)}")
    r.check(on_disk <= listed, f"files on disk not in manifest {sorted(on_disk - listed)}")
    print(f"  listed={len(listed)} on_disk={len(on_disk)} missing={len(listed - on_disk)} unlisted={len(on_disk - listed)}")
    r.check(len(listed) == len(ds.manifest), "two file_ids point at the same file")
    print("  types:", dict(Counter(p.rsplit(".", 1)[-1] for p in listed)))


def verify_evidence(r: Report, ds, evidence):
    print("\n[5] Evidence parsing vs raw content (per file)")
    print(f"  {'file':<12}{'type':<6}{'raw':>6}{'parsed':>8}  resources raw/covered  notes")
    for fid in sorted(ds.manifest):
        path = ds.evidence_path(fid)
        ext = path.suffix.lower().lstrip(".")
        obs = evidence[fid]
        notes = []

        if ext == "png":
            pending = obs[0].fields.get("transcription_pending")
            r.check(not pending, f"{fid}: image has no transcription in code/cache/images/")
            raw_text = "" if pending else obs[0].text
            table_lines = [ln for ln in raw_text.splitlines() if ln.startswith("|")]
            headers = sum(1 for ln in raw_text.splitlines() if ln.startswith("## Table"))
            raw_n = len(table_lines) - headers
            parsed_n = sum(1 for o in obs if o.kind == "image_row")
            r.check(raw_n == parsed_n, f"{fid}: {raw_n} transcribed table rows but {parsed_n} parsed")
            stated = re.findall(r"(\d+) records\)", raw_text)
            r.check(sum(map(int, stated)) == raw_n, f"{fid}: screen says {stated} records, transcribed {raw_n}")
            notes.append("transcription pending" if pending else f"transcribed, {len(obs[0].fields['sections'])} sections")
            if not pending:
                r.check(obs[0].fields["_source_sha256"] not in " ".join(obs[0].identifiers()), f"{fid}: image checksum leaked into identifiers")
        elif ext == "pdf":
            import pymupdf
            with pymupdf.open(path) as doc:
                raw_text = "\n".join(p.get_text() for p in doc)
                raw_n = doc.page_count
            parsed_n = len(obs)
            r.check(len(raw_text.strip()) > 0, f"{fid}: PDF has no text layer")
            r.check(obs[0].text == raw_text, f"{fid}: PDF text differs from raw extraction")
            notes.append(f"{raw_n} pages, {len(raw_text)} chars")
        else:
            raw_text = path.read_text(encoding="utf-8")
            if ext == "sql":
                raw_n = len(re.findall(r"^INSERT INTO", raw_text, re.M))
                scope_inserts = len(re.findall(r"^INSERT INTO\s+\"?snapshot_scope", raw_text, re.M))
                raw_n -= scope_inserts
                parsed_n = sum(1 for o in obs if o.kind == "sql_row")
                r.check(raw_n == parsed_n, f"{fid}: {raw_n} INSERT rows but {parsed_n} parsed")
                scopes = [o for o in obs if o.kind == "sql_scope"]
                r.check(bool(scopes), f"{fid}: no capture scope recognized")
                r.check(all(o.observed_at for o in scopes), f"{fid}: scope without capture time")
                notes.append(f"scope={len(scopes)} (system,table) pairs, {sum(1 for o in scopes if o.fields['row_count'] == 0)} empty")
            elif ext == "json":
                data = json.loads(raw_text)
                raw_n = _count_resource_objects(data)
                parsed_n = sum(1 for o in obs if o.kind == "json_record")
                r.check(raw_n == parsed_n, f"{fid}: {raw_n} objects with resource_id but {parsed_n} parsed")
                nested = _nested_resource_objects(data)
                if nested:
                    notes.append(f"{nested} nested resource objects")
                r.check(all(o.observed_at for o in obs if o.kind == "json_record"), f"{fid}: json record without any time context")
                no_sys = sum(1 for o in obs if o.kind == "json_record" and not o.system_id)
                r.check(not no_sys, f"{fid}: {no_sys} records without a system")
            elif ext == "log":
                lines = [ln for ln in raw_text.splitlines() if ln.strip()]
                raw_n = len(lines)
                parsed = [parse_line(ln) for ln in lines]
                bad_time = [ln for ln, p in zip(lines, parsed) if not ISO_Z.match(p["time"])]
                r.check(not bad_time, f"{fid}: {len(bad_time)} lines without leading ISO timestamp")
                no_fields = [ln for ln, p in zip(lines, parsed) if len([k for k in p if not k.startswith("_") and k != "time"]) < 2]
                r.check(not no_fields, f"{fid}: {len(no_fields)} lines parsed to almost nothing, e.g. {no_fields[:1]}")
                no_event = sum(1 for p in parsed if not p.get("event"))
                no_service = sum(1 for p in parsed if not p.get("service"))
                parsed_n = sum(len(o.text.splitlines()) for o in obs)
                r.check(parsed_n == raw_n, f"{fid}: {raw_n} lines but {parsed_n} kept in traces")
                # Merging by trace_id must never fold two different resources into one trace.
                multi = [o for o in obs if len({p.get('resource_id') for p in map(parse_line, o.text.splitlines()) if p.get('resource_id')}) > 1]
                r.check(not multi, f"{fid}: {len(multi)} traces merge multiple resource_ids, e.g. {multi[0].fields.get('trace_id') if multi else ''}")
                multi_key = [o for o in obs if o.resource_id and len(o.fields["lookup_keys"]) > 1]
                if multi_key:
                    notes.append(f"{len(multi_key)} traces with 2+ keys (e.g. email + user ref)")
                if no_event or no_service:
                    notes.append(f"lines w/o event={no_event} w/o service={no_service}")
            else:  # txt
                raw_n = len(re.findall(r"^\s*-\s.*\|", raw_text, re.M))
                parsed_n = sum(1 for o in obs if o.kind == "text_row")
                r.check(raw_n == parsed_n, f"{fid}: {raw_n} table lines but {parsed_n} parsed")

        # Strongest check: every resource id written anywhere in the raw file must be reachable
        # from the parsed observations (as a resource_id, inside fields, or inside kept text).
        raw_res = set(RESOURCE_TOKEN.findall(raw_text))
        parsed_blob = " ".join((o.resource_id or "") + " " + o.text + " " + json.dumps(o.fields, default=str) for o in obs)
        covered = {x for x in raw_res if re.search(rf"\b{x}\b", parsed_blob)}
        r.check(covered == raw_res, f"{fid}: resource ids lost {sorted(raw_res - covered)[:5]}")
        # And every email in the raw file must survive too.
        raw_emails = {e.lower() for e in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", raw_text)}
        parsed_emails = {e for o in obs for e in o.identifiers() if "@" in e}
        r.check(raw_emails <= parsed_emails, f"{fid}: emails lost {sorted(raw_emails - parsed_emails)[:3]}")
        # Evidence after the manifest capture time would break the audit_at cutoff.
        cap = ds.manifest[fid]["captured_at"]
        late = [o for o in obs if o.observed_at and ISO_Z.match(o.observed_at) and o.observed_at > cap]
        if late:
            notes.append(f"{len(late)} rows dated after file capture (available_at moved later)")
        r.check(all(o.available_at and o.available_at >= cap and (not o.observed_at or o.available_at >= o.observed_at) for o in obs),
                f"{fid}: available_at not = max(captured_at, observed_at)")
        r.check(not any(v == "null" for o in obs for v in o.fields.values()), f"{fid}: literal 'null' strings left in fields")
        bad_ts = [o.observed_at for o in obs if o.observed_at and not ISO_Z.match(o.observed_at)]
        r.check(not bad_ts, f"{fid}: non ISO-Z observation times {bad_ts[:2]}")

        print(f"  {fid:<12}{ext:<6}{raw_n:>6}{parsed_n:>8}  {len(raw_res):>4}/{len(covered):<4}            {'; '.join(notes)}")


def _count_resource_objects(node, parent=None) -> int:
    """Objects with a resource_id, not counting a nested copy of the same resource."""
    if isinstance(node, list):
        return sum(_count_resource_objects(x, parent) for x in node)
    if isinstance(node, dict):
        rid = node.get("resource_id")
        here = 1 if rid is not None and rid != parent else 0
        return here + sum(_count_resource_objects(v, rid if rid is not None else parent) for v in node.values())
    return 0


def _nested_resource_objects(node, inside=False) -> int:
    if isinstance(node, list):
        return sum(_nested_resource_objects(x, inside) for x in node)
    if isinstance(node, dict):
        here = "resource_id" in node
        return (1 if here and inside else 0) + sum(_nested_resource_objects(v, inside or here) for v in node.values())
    return 0


def verify_systems(r: Report, evidence):
    print("\n[6] System named by each resource-bearing observation vs the resource's canonical owner")
    pairs = Counter()
    for obs in evidence.values():
        for o in obs:
            if not o.resource_id:
                continue
            r.check(bool(o.system_id), f"{o.file_id}: {o.resource_id} has no system")
            canonical = PREFIX_SYSTEM.get(resource_prefix(o.resource_id))
            r.check(canonical is not None, f"{o.file_id}: unknown resource family {o.resource_id}")
            pairs[(str(canonical), str(o.system_id))] += 1
    for (canonical, named), n in sorted(pairs.items()):
        flag = "" if canonical == named else "   <- evidence names a different system"
        print(f"  {canonical:<24} {named:<26} {n:>5}{flag}")


def run() -> int:
    r = Report()
    verify_csvs(r)
    ds = load_dataset()
    verify_loader(r, ds)
    verify_manifest(r, ds)
    evidence = load_evidence(ds)
    verify_evidence(r, ds, evidence)
    verify_systems(r, evidence)

    print(f"\nPASSED {r.passes} checks, {len(r.failures)} failures, {len(r.warnings)} warnings")
    for f in r.failures:
        print("  FAIL", f)
    for w in r.warnings:
        print("  WARN", w)
    return 1 if r.failures else 0
