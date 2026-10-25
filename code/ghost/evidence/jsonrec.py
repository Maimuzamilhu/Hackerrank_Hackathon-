"""JSON evidence -> one Observation per object that carries a resource_id.

The JSON files use several layouts (snapshot bundles, service exports, manifests, ledgers).
Rather than one parser per layout, walk the tree and inherit context (time, store,
collection, system) from the enclosing objects and keys.
"""
import json
from pathlib import Path

from .model import Observation
from ..systems import system_for_resource

TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "exported_at")
SYSTEM_KEYS = ("system", "data_store", "origin_system", "system_id")
CONTAINER_KEYS = ("collection", "collection_name")
SOURCE_KEYS = ("source", "origin")


def parse_json(file_id: str, path: Path) -> list[Observation]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Observation] = []
    _walk(data, {"path": []}, file_id, out)
    return out


def _walk(node, ctx: dict, file_id: str, out: list[Observation]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, ctx, file_id, out)
        return
    if not isinstance(node, dict):
        return

    ctx = dict(ctx)
    for keys, name in ((TIME_KEYS, "time"), (SYSTEM_KEYS, "system"), (CONTAINER_KEYS, "container"), (SOURCE_KEYS, "source")):
        for k in keys:
            if isinstance(node.get(k), str):
                ctx[name] = node[k]
                break

    records = node.get("records")
    if isinstance(records, list):
        # A capture of one store: an empty or partial record list is evidence of absence.
        # Without a stated store, the records' own resource family names it.
        inferred = {system_for_resource(r["resource_id"]) for r in records if isinstance(r, dict) and r.get("resource_id")}
        scope_system = ctx.get("system") or (inferred.pop() if len(inferred) == 1 else None)
    if isinstance(records, list) and scope_system:
        out.append(Observation(
            file_id=file_id, kind="json_scope", system_id=scope_system, container=ctx.get("container"),
            resource_id=None, observed_at=ctx.get("time"),
            fields={"record_count": len(node["records"]), "_path": "/".join(ctx["path"])},
        ))

    if "resource_id" in node:
        out.append(Observation(
            file_id=file_id,
            kind="json_record",
            system_id=ctx.get("system") or _system_from_path(ctx["path"]) or system_for_resource(node["resource_id"]),
            container=ctx.get("container") or (ctx["path"][-1] if ctx["path"] else None),
            resource_id=node["resource_id"],
            observed_at=ctx.get("time"),
            fields={**node, "_path": "/".join(ctx["path"]), "_source": ctx.get("source")},
            status=_status(node),
        ))
        # A nested object for the same resource is already inside `fields`; one for a
        # different resource is its own record and must not be dropped.
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _walk_nested(value, node["resource_id"], {**ctx, "path": ctx["path"] + [key]}, file_id, out)
        return

    for key, value in node.items():
        if isinstance(value, (dict, list)):
            _walk(value, {**ctx, "path": ctx["path"] + [key]}, file_id, out)


def _walk_nested(node, parent_id: str, ctx: dict, file_id: str, out: list[Observation]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_nested(item, parent_id, ctx, file_id, out)
    elif isinstance(node, dict):
        if node.get("resource_id") not in (None, parent_id):
            _walk(node, ctx, file_id, out)
        else:
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    _walk_nested(value, parent_id, {**ctx, "path": ctx["path"] + [key]}, file_id, out)


def _system_from_path(path: list[str]) -> str | None:
    # service_exports -> {account_service: {profile_lookups: [...]}}
    if "service_exports" in path:
        i = path.index("service_exports")
        if i + 1 < len(path):
            return path[i + 1]
    return None


def _status(node: dict) -> str | None:
    result = node.get("result")
    if isinstance(result, str) and result.upper() in ("FOUND", "NOT_FOUND"):
        return result.upper()
    code = node.get("status_code")
    if code == 200:
        return "FOUND"
    if code == 404:
        return "NOT_FOUND"
    return None
