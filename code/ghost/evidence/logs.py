"""Log evidence -> Observations.

Log files come in several line formats (key=value, JSON payload, syslog, bracketed,
pipe-separated, and a requestId/function/message cloud format). Every line is normalized
to {time, level, service, trace_id, event, ...fields}. Lines that share a trace_id are then
merged into one Observation, because a lookup usually spans a `*_started` line (with the
lookup key) and a `*_completed` line (with the resource_id and result).
"""
import json
import re
from pathlib import Path

from .model import Observation

KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET_RE = re.compile(r"^\[(\w+)\] \[([\w-]+)\] \[([\w-]+)\] (\w+): ?(.*)$")
PIPE_RE = re.compile(r"^\| (\w+) \| ([\w-]+) \| ([\w-]+) \| (\w+) \| ?(.*)$")
SYSLOG_RE = re.compile(r"^<\d+>\d+ \S+ ([\w-]+) ([\w-]+) - - (.*)$")


def parse_kv(text: str) -> dict:
    fields = {}
    for key, raw in KV_RE.findall(text):
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        # Logs write missing values as the literal `null`; keep the key, drop the value.
        fields[key] = None if raw in ("null", "None", "") else raw
    return fields


def parse_line(line: str) -> dict | None:
    line = line.rstrip("\n")
    if not line.strip():
        return None
    time, _, rest = line.partition(" ")
    rest = rest.strip()
    rec: dict = {}

    if rest.startswith("{"):
        try:
            rec = {k: (None if v in ("null", "") else v) for k, v in json.loads(rest).items()}
        except json.JSONDecodeError:
            rec = parse_kv(rest)
        if "severity" in rec:
            rec["level"] = rec.pop("severity")
    elif m := BRACKET_RE.match(rest):
        rec = {"level": m[1], "service": m[2], "trace_id": m[3], "event": m[4], **parse_kv(m[5])}
    elif m := PIPE_RE.match("| " + rest if not rest.startswith("|") else rest):
        rec = {"level": m[1], "service": m[2], "trace_id": m[3], "event": m[4], **parse_kv(m[5])}
    elif m := SYSLOG_RE.match(rest):
        rec = {"service": m[1], "trace_id": m[2], **parse_kv(m[3])}
    else:
        parts = rest.split(" ", 1)
        if parts[0].isupper() and "=" not in parts[0]:
            rec = {"level": parts[0], **parse_kv(parts[1] if len(parts) > 1 else "")}
        else:
            rec = parse_kv(rest)
        if "message" in rec:
            # Cloud runtime format nests a second key=value list inside message="...".
            rec.update(parse_kv(rec.pop("message")))
        if "function" in rec and "service" not in rec:
            rec["service"] = rec.pop("function")
        if "requestId" in rec and "trace_id" not in rec:
            rec["trace_id"] = rec.pop("requestId")
    rec["time"] = time
    rec["_raw"] = line
    return rec


def parse_log(file_id: str, path: Path) -> list[Observation]:
    lines = [r for r in map(parse_line, path.read_text(encoding="utf-8").splitlines()) if r]
    groups: dict[str, list[dict]] = {}
    for i, rec in enumerate(lines):
        key = rec.get("trace_id") or f"line-{i}"
        groups.setdefault(key, []).append(rec)

    out = []
    for key, recs in groups.items():
        merged: dict = {}
        for r in recs:
            if r.get("event") == "cache_lookup" and "result" in r:
                # A cache miss says the lookup cache was cold, not that the record is gone.
                r = {**r, "cache_result": r["result"]}
                del r["result"]
            # Later lines win, but a later `null` never erases a value an earlier line stated.
            merged.update({k: v for k, v in r.items() if k != "_raw" and (v is not None or k not in merged)})
        merged["events"] = [r.get("event") for r in recs if r.get("event")]
        merged["lookup_keys"] = sorted({r[k] for r in recs for k in ("lookup_key", "match_key", "scan_key", "correlation_key", "subject_reference", "customer_email", "email") if r.get(k)})
        out.append(Observation(
            file_id=file_id,
            kind="log_trace",
            system_id=merged.get("system") or merged.get("service"),
            container=merged.get("service"),
            resource_id=merged.get("resource_id"),
            observed_at=recs[-1]["time"],
            fields=merged,
            status=_status(merged),
            text="\n".join(r["_raw"] for r in recs),
        ))
    return out


def _status(rec: dict) -> str | None:
    result = str(rec.get("result", "")).upper()
    if result in ("FOUND", "NOT_FOUND"):
        return result
    if result == "HIT":
        return "FOUND"
    if result == "MISS":
        return "NOT_FOUND"
    if str(rec.get("http_status")) == "200":
        return "FOUND"
    if str(rec.get("http_status")) == "404":
        return "NOT_FOUND"
    if rec.get("rows") is not None:
        return "FOUND" if str(rec["rows"]) not in ("0", "") else "NOT_FOUND"
    return None
