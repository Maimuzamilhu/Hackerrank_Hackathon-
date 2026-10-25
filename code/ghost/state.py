"""Step 6b: what each piece of evidence says about a resource's state.

Every observation is turned into a Signal: is the record there, does it still expose the
person, is a data flow running, can a backup be restored. Signals are facts as written in
the evidence; workflow claims in system_activity.csv never become state signals.
"""
import re
from dataclasses import dataclass

from .evidence.model import EMAIL_RE, PHONE_RE, Observation
from .systems import resource_prefix

# Fields that directly identify a person (name, contact details, hash, user link).
PII_FIELDS = {"email", "phone", "full_name", "name", "display_name", "email_hash", "requester_email",
              "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
              "user_id", "uploaded_by", "Email", "Owner", "Account", "Selector", "Display name"}
# A stable internal reference that policy allows on retained records and tombstones.
STABLE_REF_FIELDS = {"customer_reference", "subject_reference", "subject_ref", "subject_selector"}
TEXT_FIELDS = {"message_text", "message_excerpt", "Message"}

RESULT_MAP = {
    "FOUND": dict(present=True), "NOT_FOUND": dict(present=False),
    "HIT": dict(present=True), "MISS": dict(present=False),
    "IDENTIFIABLE": dict(present=True, exposed=True), "ANONYMIZED": dict(present=True, exposed=False),
    "REDACTED": dict(present=True, exposed=False),
    "ACTIVE": dict(present=True, active=True), "DISABLED": dict(present=True, active=False),
    "BLOCKED": dict(present=True, active=False),
}
SNAPSHOT_KINDS = {"sql_row", "json_record", "image_row"}


@dataclass
class Signal:
    resource_id: str
    file_id: str
    time: str                    # when the evidence observed it
    present: bool | None = None
    exposed: bool | None = None  # identifying fields still populated
    active: bool | None = None   # flow running / restore allowed
    category: str | None = None  # record_type stated by the evidence
    attachment: bool = False     # a support record that is a file attachment


def _null(v) -> bool:
    return v is None or v == "" or v == "null" or v == "—" or v == "[REDACTED]"


def _truthy(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str) and v.lower() in ("true", "1", "yes", "enabled", "active", "continuous"):
        return True
    if isinstance(v, str) and v.lower() in ("false", "0", "no", "disabled", "stopped", "blocked"):
        return False
    return None


def _flat(fields: dict) -> dict:
    """Merge nested record/document/profile/columns dicts into one level."""
    out = {}
    for k, v in fields.items():
        if k.startswith("_"):
            continue
        if k in ("record", "document", "profile", "columns") and isinstance(v, dict):
            out.update(_flat(v))
        elif k == "hits" and isinstance(v, dict):
            docs = v.get("documents") or []
            out["_hit_total"] = v.get("total", len(docs))
            for d in docs:
                if isinstance(d, dict):
                    out.update(_flat(d))
        else:
            out[k] = v
    return out


def _exposes(flat: dict) -> bool | None:
    seen = False
    for k, v in flat.items():
        if k in PII_FIELDS:
            seen = True
            if not _null(v):
                return True
        if k in TEXT_FIELDS and isinstance(v, str):
            seen = True
            if EMAIL_RE.search(v) or PHONE_RE.search(v):
                return True
    if "direct_identifiers" in flat:
        return str(flat["direct_identifiers"]) not in ("0", "")
    return False if seen else None


def signal_of(o: Observation) -> Signal | None:
    if not o.resource_id:
        return None
    flat = _flat(o.fields)
    s = Signal(o.resource_id, o.file_id, o.observed_at or o.available_at)
    prefix = resource_prefix(o.resource_id)

    result = str(flat.get("result") or "").upper()
    if result in RESULT_MAP:
        for k, v in RESULT_MAP[result].items():
            setattr(s, k, v)
    elif o.status == "FOUND" or o.kind in SNAPSHOT_KINDS:
        s.present = True
    elif o.status == "NOT_FOUND":
        s.present = False
    elif o.kind == "log_trace":
        # A service read, scan, heartbeat or check that names the resource found it.
        s.present = True
    if flat.get("profile", "x") is None or ("_hit_total" in flat and flat["_hit_total"] == 0):
        s.present = False
    if str(flat.get("status_code", "")) == "404" or str(flat.get("http_status", "")) == "404":
        s.present = False
    if _truthy(flat.get("available")) is False or _truthy(flat.get("archive_present")) is False:
        s.present = False

    if s.present:
        if flat.get("detector") == "direct_identifier" and not _null(flat.get("matched_value")):
            s.exposed = True   # a PII scanner matched a direct identifier in the record
        if s.exposed is None:
            s.exposed = _exposes(flat)
        if prefix == "data_flow" and s.active is None:
            for k in ("enabled", "Mode", "Status", "mode"):
                if k in flat and (t := _truthy(flat[k])) is not None:
                    s.active = t
                    break
        if prefix == "backup" and s.active is None:
            if "restore_allowed" in flat:
                s.active = _truthy(flat["restore_allowed"])
            elif "eligible" in flat or "deletion_block" in flat:
                s.active = _truthy(flat.get("eligible")) is True and _truthy(flat.get("deletion_block")) is not True
            elif flat.get("decision") in ("deny", "allow"):
                s.active = flat["decision"] == "allow"
            elif o.kind == "image_row" and s.exposed:
                # Listed in the live recovery console, still carrying the person's identifiers,
                # with no restore block stated: the archive can be restored as it is.
                s.active = True
    s.category = flat.get("record_type") if isinstance(flat.get("record_type"), str) else None
    s.attachment = (o.container == "ticket_attachments" or s.category == "SUPPORT_ATTACHMENT"
                    or (prefix == "support" and ("file_name" in flat or "file" in flat
                                                 or "attachment" in str(flat.get("event", "")))))
    return s


def absence_signals(observations: list[Observation], resource_id: str, system_id: str, person: set[str]) -> list[Signal]:
    """A capture of a system that does not contain the resource is evidence it was absent.

    Primary/replica SQL topology snapshots dump whole tables, so any of them counts.
    Inventory dumps and snapshot bundles hold a selection of customers: their absence only
    counts when the same file holds some other record of this person (it covers them).
    """
    out = []
    by_file: dict[str, list[Observation]] = {}
    for o in observations:
        by_file.setdefault(o.file_id, []).append(o)
    for file_id, obs in by_file.items():
        scopes = [o for o in obs if o.kind in ("sql_scope", "json_scope") and o.system_id == system_id]
        if not scopes:
            continue
        # Single-system primary/replica dumps (customer_profiles_*, search_documents_*) hold
        # the whole table; the mixed customer_data_* replicas hold a selection of customers.
        full_table = any(o.kind == "sql_scope" and o.fields.get("topology") and not o.container.startswith("customer_data_")
                         for o in scopes)
        covers_person = any(o.resource_id and (o.identifiers() & person) for o in obs)
        if not (full_table or covers_person):
            continue
        contains = any(o.resource_id == resource_id for o in obs)
        if not contains:
            t = max(o.observed_at or o.available_at for o in scopes)
            out.append(Signal(resource_id, file_id, t, present=False))
    return out


def alias_signals(observations: list[Observation], resource_id: str, alias: str) -> list[Signal]:
    """The catalog's business name for a resource shown on a screen row, a screen header or
    a document: the record existed then, exposing PII if that row/page shows any."""
    out = []
    pattern = re.compile(rf"(?<![\w.-]){re.escape(alias)}(?![\w-])")
    for o in observations:
        if o.kind == "image_row":
            text = o.text
        elif o.kind == "image_doc":
            text = "\n".join(f"{k}: {v}" for name, kv in o.fields.get("sections", {}).items()
                             if name == "Screen" or name.startswith("Panel") for k, v in kv.items())
        elif o.kind == "pdf_doc":
            text = o.text
        else:
            continue
        if not pattern.search(text):
            continue
        s = Signal(resource_id, o.file_id, o.available_at, present=True)
        page = text if o.kind != "image_doc" else o.text
        s.exposed = bool(EMAIL_RE.search(page) or PHONE_RE.search(page))
        out.append(s)
    return out


def document_signals(observations: list[Observation], resource_id: str, system_id: str, origin: dict[str, str]) -> list[Signal]:
    """A business document about a record shows it existed when the document was captured:
    a screenshot whose header or detail panel shows the record (screens show live data), or
    a PDF exported from the record's own system. A support PDF is the ticket's attachment."""
    out = []
    for o in observations:
        if o.kind not in ("pdf_doc", "image_doc") or resource_id not in o.fields.get("mentioned_resources", []):
            continue
        if o.kind == "pdf_doc" and origin.get(o.file_id) != system_id:
            continue
        s = Signal(resource_id, o.file_id, o.available_at, present=True)
        s.exposed = bool(EMAIL_RE.search(o.text) or PHONE_RE.search(o.text))
        s.attachment = system_id == "support_platform" and o.kind == "pdf_doc"
        out.append(s)
    return out
