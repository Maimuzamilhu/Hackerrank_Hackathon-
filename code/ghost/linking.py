"""Step 6a: which resources belong to which person.

Builds a global ownership map resource_id -> user_id from every grounded link, each with
its provenance and strength:

  CONTENT (3)  the record itself carries the person's identifier (email, hash, phone,
               user reference) in its own fields.
  CATALOG (3)  an explicit LINK_RESOURCE_IDENTITY activity names the resource and the person.
  XREF    (2)  the record points at another resource the person owns (source_resource_id).
  LOOKUP  (2)  a lookup keyed by the person's identifier returned this resource.
  DOCUMENT(2)  a document about exactly one person mentions the resource.
  JOB     (1)  the deletion workflow processed the resource inside this person's request
               (proven by elimination, see `assign_jobs`).
  JOB_GUESS(0) the same, where several requests share a time slot and the data cannot
               separate them; used only for the primary profile every request must have.

A resource whose own record names a different identity (another user, or an email that
belongs to nobody, like the `.jr` look-alikes) is never assigned through a weaker link.
Ownership is a stable fact, so links are learned from all evidence; whether a resource's
*state* can be used for a case is decided later, strictly by audit_at.
"""
import re
from collections import defaultdict
from dataclasses import dataclass

from .data import Dataset
from .evidence.model import EMAIL_RE, Observation
from .identity import IdentityIndex, norm_email, norm_phone
from .systems import system_for_resource

CONTENT, CATALOG, XREF, LOOKUP, DOCUMENT, JOB, JOB_GUESS = "content", "catalog", "xref", "lookup", "document", "job", "job_guess"
STRENGTH = {CONTENT: 3, CATALOG: 3, XREF: 2, LOOKUP: 2, DOCUMENT: 2, JOB: 1, JOB_GUESS: 0}

# Fields that hold the *search key* of a lookup rather than the record's own content.
LOOKUP_FIELDS = {"lookup_key", "match_key", "scan_key", "query", "correlation_key", "lookup_value",
                 "subject_key", "matched_value", "lookup_keys", "events", "Search box"}


@dataclass(frozen=True)
class Link:
    resource_id: str
    owner: str            # user_id, or "unknown:<identifier>" for an identity outside the dataset
    how: str
    source: str           # file_id / activity_id that proves it
    time: str             # when that source became available

    @property
    def strength(self) -> int:
        return STRENGTH[self.how]


class OwnerResolver:
    """Maps any identifier token to a user_id (or None)."""

    def __init__(self, ds: Dataset, index: IdentityIndex):
        self.by_token: dict[str, set[str]] = defaultdict(set)
        for email, owners in index.by_email.items():
            self.by_token[email] |= owners
        for uid, u in ds.identities.items():
            self.by_token[u["email_hash"]].add(uid)
            self.by_token["tel:" + norm_phone(u["phone"])].add(uid)
            self.by_token[uid].add(uid)
        for a in ds.audit_log:
            for v in (a["old_value"], a["new_value"]):
                key = "tel:" + norm_phone(v) if a["field_name"] == "phone" else norm_email(v) if a["field_name"] == "email" else v
                self.by_token[key].add(a["user_id"])

    def owners(self, tokens: set[str]) -> set[str]:
        """Known users named by these tokens; unknown emails become 'unknown:<email>'."""
        out = set()
        for t in tokens:
            if t in self.by_token:
                out |= self.by_token[t]
            elif "@" in t:
                out.add("unknown:" + t)
        return out


def _content_tokens(o: Observation) -> set[str]:
    content = {k: v for k, v in o.fields.items() if k not in LOOKUP_FIELDS and not k.startswith("_")}
    probe = Observation(o.file_id, o.kind, o.system_id, o.container, o.resource_id, o.observed_at, content)
    if o.kind in ("image_row", "text_row"):
        probe.text = o.text   # a table row is entirely the record's own content
    return probe.identifiers()


def _lookup_tokens(o: Observation) -> set[str]:
    lookup = {k: v for k, v in o.fields.items() if k in LOOKUP_FIELDS}
    probe = Observation(o.file_id, o.kind, o.system_id, o.container, o.resource_id, o.observed_at, lookup)
    return probe.identifiers()


def collect_links(ds: Dataset, evidence: dict[str, list[Observation]], resolver: OwnerResolver) -> list[Link]:
    links: list[Link] = []
    known = set()
    for file_id, obs in evidence.items():
        for o in obs:
            if o.resource_id and system_for_resource(o.resource_id):
                known.add(o.resource_id)
                owners = resolver.owners(_content_tokens(o))
                for u in owners:
                    links.append(Link(o.resource_id, u, CONTENT, file_id, o.available_at))
                if not owners:
                    for u in resolver.owners(_lookup_tokens(o)):
                        links.append(Link(o.resource_id, u, LOOKUP, file_id, o.available_at))
                # cross reference to another resource (search document -> source account)
                ref = o.fields.get("source_resource_id")
                if isinstance(ref, str) and system_for_resource(ref):
                    links.append(Link(o.resource_id, "xref:" + ref, XREF, file_id, o.available_at))
            elif o.kind in ("pdf_doc", "text_doc", "image_doc"):
                owners = {u for u in resolver.owners(o.identifiers()) if not u.startswith("unknown:")}
                if len(owners) == 1:
                    (u,) = owners
                    for rid in o.fields.get("mentioned_resources", []):
                        if system_for_resource(rid):
                            links.append(Link(rid, u, DOCUMENT, file_id, o.available_at))

    for a in ds.activity:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            toks = {e.lower() for e in EMAIL_RE.findall(a["details"])} | set(re.findall(r"\buser_\d+\b", a["details"]))
            for u in resolver.owners(toks):
                links.append(Link(a["resource_id"], u, CATALOG, a["activity_id"], a["event_time"]))
    return links


def _minutes(a: str, b: str) -> float:
    from datetime import datetime
    p = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (p(a) - p(b)).total_seconds() / 60


# When each kind of deletion-worker message appears relative to the request's reported
# status, in minutes (measured on the sample answers, widened by a few minutes).
JOB_WINDOWS = [
    ("submitted primary profile", (-78.0, -42.0)),
    ("Policy action completed", (-32.0, -4.0)),
    ("received the final result", (34.0, 122.0)),
    ("Connector status check", (34.0, 122.0)),
    ("Restore-access check", (34.0, 122.0)),
]
DEFAULT_WINDOW = (-10.0, 12.0)   # immediate single-step reports ("reported the requested operation", ...)


def job_window(details: str) -> tuple[float, float]:
    return next((w for key, w in JOB_WINDOWS if key in details), DEFAULT_WINDOW)


def assign_jobs(ds: Dataset, cases: list[dict], owners: dict[str, tuple[str, list[Link]]]) -> dict[str, list[Link]]:
    """Attribute deletion_worker events to requests. case_id -> JOB / JOB_GUESS links.

    `cases` = [{case_id, user_id, requested_at, reported_at}] for every deletion request.
    1. Anchor: an event on a resource already owned by the case's person, in its window.
    2. Propagate: an unowned event that fits exactly one case still lacking a record of that
       kind is that case's; a case needing a primary profile with exactly one candidate
       event takes it. Repeat until nothing changes. These links are proven by elimination.
    3. Fill: each request has exactly one primary profile, so a request still without one
       takes the earliest remaining candidate event. These are marked JOB_GUESS: the data
       cannot tell them apart (several requests share the same reported time).
    """
    events = [a for a in ds.activity if a["source_system"] == "deletion_worker"]
    kind_of = {a["activity_id"]: a["resource_id"].rsplit("_", 1)[0] for a in events}

    def fits(c, a):
        lo, hi = job_window(a["details"])
        return lo <= _minutes(a["event_time"], c["reported_at"]) <= hi and a["event_time"] >= c["requested_at"]

    cands = {a["activity_id"]: [c["case_id"] for c in cases if fits(c, a)] for a in events}
    case_user = {c["case_id"]: c["user_id"] for c in cases}
    assigned: dict[str, str] = {}
    guessed: set[str] = set()
    kinds: dict[str, set[str]] = defaultdict(set)

    def take(aid, cid):
        assigned[aid] = cid
        kinds[cid].add(kind_of[aid])

    overridden: set[str] = set()
    for a in events:
        owner, support = owners.get(a["resource_id"], (None, []))
        mine = [c for c in cands[a["activity_id"]] if case_user[c] == owner]
        if len(mine) == 1:
            take(a["activity_id"], mine[0])
        elif owner and kind_of[a["activity_id"]] == "account" and max(x.strength for x in support) < STRENGTH[CONTENT]:
            # A lookup tied this profile to someone whose request is nowhere near the job
            # that processed it; the job is the stronger fact, so reopen the event.
            overridden.add(a["resource_id"])

    open_events = [a for a in events if a["activity_id"] not in assigned
                   and (a["resource_id"] not in owners or a["resource_id"] in overridden)]
    changed = True
    while changed:
        changed = False
        for a in open_events:
            aid = a["activity_id"]
            if aid in assigned:
                continue
            free = [c for c in cands[aid] if kind_of[aid] not in kinds[c]]
            if len(free) == 1:
                take(aid, free[0])
                changed = True
        for c in case_user:
            if "account" in kinds[c]:
                continue
            options = [a["activity_id"] for a in open_events
                       if kind_of[a["activity_id"]] == "account" and a["activity_id"] not in assigned and c in cands[a["activity_id"]]]
            if len(options) == 1:
                take(options[0], c)
                changed = True

    for c in sorted(case_user, key=lambda cid: (next(x["reported_at"] for x in cases if x["case_id"] == cid), cid)):
        if "account" in kinds[c]:
            continue
        options = sorted((a for a in open_events if kind_of[a["activity_id"]] == "account"
                          and a["activity_id"] not in assigned and c in cands[a["activity_id"]]), key=lambda a: a["event_time"])
        if options:
            take(options[0]["activity_id"], c)
            guessed.add(options[0]["activity_id"])

    # Every job event belongs to some request: give each remaining record event to the
    # earliest-reported candidate request that has no record of that kind yet.
    # A job reports in one style: "Policy action completed" (actions that succeeded before the
    # status was reported) or "received the final result" / status checks (after it). Only
    # requests whose proven events share the event's style may receive it.
    order = {cid: (next(x["reported_at"] for x in cases if x["case_id"] == cid), cid) for cid in case_user}
    late_style = ("received the final result", "Connector status check", "Restore-access check")
    style = lambda details: "policy" if "Policy action completed" in details else "late" if any(s in details for s in late_style) else None  # noqa: E731
    by_id = {a["activity_id"]: a for a in events}
    case_styles: dict[str, set] = defaultdict(set)
    for aid, cid in assigned.items():
        if aid not in guessed and kind_of[aid] != "account":
            case_styles[cid].add(style(by_id[aid]["details"]))
    for a in sorted(open_events, key=lambda a: a["event_time"]):
        aid = a["activity_id"]
        if aid in assigned or kind_of[aid] == "account":
            continue
        s = style(a["details"])
        free = sorted((c for c in cands[aid] if kind_of[aid] not in kinds[c]
                       and case_styles[c] <= {s}), key=order.get)
        if free:
            take(aid, free[0])
            guessed.add(aid)
            case_styles[free[0]].add(s)

    by_case: dict[str, list[Link]] = defaultdict(list)
    by_case["__overridden__"] = [Link(r, "", JOB, "", "") for r in sorted(overridden)]
    for a in open_events:
        cid = assigned.get(a["activity_id"])
        if cid:
            how = JOB_GUESS if a["activity_id"] in guessed else JOB
            by_case[cid].append(Link(a["resource_id"], case_user[cid], how, a["activity_id"], a["event_time"]))
    return by_case


def catalog_aliases(ds: Dataset) -> dict[str, str]:
    """resource_id -> the business name the data catalog gives it, e.g.
    object_19 -> delivery_confirmation_19.pdf, export_1842 -> NST-2026-0718-1842.
    Screens and documents show that name rather than the internal id."""
    out = {}
    for a in ds.activity:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = re.search(rf"\b{re.escape(a['resource_id'])} \(([^)]+)\)", a["details"])
            if m and m[1] != a["resource_id"]:
                out[a["resource_id"]] = m[1]
    return out


def resolve_owners(links: list[Link]) -> dict[str, tuple[str, list[Link]]]:
    """resource_id -> (owner, supporting links). Strongest evidence wins; ties are dropped."""
    by_res: dict[str, list[Link]] = defaultdict(list)
    for link in links:
        by_res[link.resource_id].append(link)

    owners: dict[str, tuple[str, list[Link]]] = {}
    for rid, ls in by_res.items():
        direct = [x for x in ls if not x.owner.startswith("xref:")]
        if not direct:
            continue
        best = max(x.strength for x in direct)
        top = {x.owner for x in direct if x.strength == best}
        if len(top) == 1:
            (owner,) = top
            owners[rid] = (owner, [x for x in direct if x.owner == owner])
    # cross references inherit the referenced resource's owner when nothing stronger exists
    for rid, ls in by_res.items():
        if rid in owners:
            continue
        for x in ls:
            target = x.owner[len("xref:"):] if x.owner.startswith("xref:") else None
            if target and target in owners:
                owners[rid] = (owners[target][0], [x])
                break
    return owners
