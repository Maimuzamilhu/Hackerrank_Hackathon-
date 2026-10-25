"""SQLite-compatible SQL snapshot exports -> Observations.

Two snapshot styles exist:
  * inventory captures with a `snapshot_scope` table naming the systems captured, and
  * topology captures (primary / replica / failover tables) whose capture time and topology
    are stated in `-- Key: value` header comments.
Each file yields one scope Observation per captured system (an empty table in a captured
system is evidence of absence) plus one Observation per table row.
"""
import json
import re
import sqlite3
from pathlib import Path

from .model import Observation
from ..systems import system_for_resource, system_for_table

TIME_COLUMNS = ("updated_at", "indexed_at", "captured_at", "recorded_at")
HEADER_RE = re.compile(r"^--\s*([A-Za-z][\w ]*?):\s*(.+)$")


def parse_header(sql: str) -> dict:
    meta = {}
    for line in sql.splitlines():
        if line.startswith("--") and (m := HEADER_RE.match(line)):
            meta[m[1].strip().lower().replace(" ", "_")] = m[2].strip()
        elif m := re.match(r"^--\s*(\w+)=(\S+)", line):
            meta[m[1]] = m[2]
    return meta


def parse_sql(file_id: str, path: Path) -> list[Observation]:
    sql = path.read_text(encoding="utf-8")
    header = parse_header(sql)
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        data_tables = [t for t in tables if t != "snapshot_scope"]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in data_tables}

        # Scope = which (system, table) pairs this snapshot captured, and when. An empty
        # captured table is evidence of absence, which a row-only view would lose.
        pairs: dict[tuple[str, str], str | None] = {}
        if "snapshot_scope" in tables:
            captured = {r["origin_system"]: r["captured_at"] for r in conn.execute("SELECT * FROM snapshot_scope")}
            for t in data_tables:
                if (s := system_for_table(t)) in captured:
                    pairs[(s, t)] = captured[s]
        else:
            # Topology captures: primary/replica tables, possibly holding several systems
            # (origin_system column) in one table.
            for t in data_tables:
                cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
                systems = {system_for_table(t)} - {None}
                if "origin_system" in cols:
                    systems |= {r[0] for r in conn.execute(f'SELECT DISTINCT origin_system FROM "{t}"')}
                for s in systems:
                    pairs[(s, t)] = header.get("captured_at")

        out = []
        for (system, table), captured_at in sorted(pairs.items()):
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')]
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE origin_system = ?', (system,)).fetchone()[0] if "origin_system" in cols else counts[table]
            out.append(Observation(
                file_id=file_id, kind="sql_scope", system_id=system, container=table, resource_id=None,
                observed_at=captured_at, fields={"row_count": n, "topology": header.get("database_topology")},
            ))
        for table in data_tables:
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                fields = dict(row)
                if "record_json" in fields:
                    # Replica tables store the source record as a JSON column.
                    fields["record"] = json.loads(fields.pop("record_json"))
                rid = fields.get("resource_id")
                # A snapshot row is observed when the snapshot was captured; its own
                # updated_at stays in `fields`.
                system = fields.get("origin_system") or system_for_table(table) or (rid and system_for_resource(rid))
                observed = (pairs.get((system, table)) or header.get("captured_at")
                            or next((fields[c] for c in TIME_COLUMNS if fields.get(c)), None))
                out.append(Observation(
                    file_id=file_id,
                    kind="sql_row",
                    system_id=fields.get("origin_system") or system_for_table(table) or (rid and system_for_resource(rid)),
                    container=table,
                    resource_id=rid,
                    observed_at=observed,
                    fields=fields,
                    status="FOUND",   # a row in a snapshot means the record existed at capture time
                ))
        return out
    finally:
        conn.close()
