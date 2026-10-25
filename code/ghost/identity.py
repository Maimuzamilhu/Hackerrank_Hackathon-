"""Step 4: resolve the request email to one user and every identifier that user has had.

The request email may be the user's current email or an earlier one. Identifiers are
reconstructed from the current snapshot plus user_audit_log, limited to what existed at or
before audit_at: a value introduced by a change after audit_at did not exist yet, and the
value it replaced was still current.
"""
import hashlib
import re
from dataclasses import dataclass, field

from .data import Dataset


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def email_hash(email: str) -> str:
    # Verified on all 850 identities: email_hash == sha256(lowercase email).
    return hashlib.sha256(norm_email(email).encode()).hexdigest()


@dataclass
class Identity:
    user_id: str
    name: str
    matched_via: str                       # "current_email" | "historical_email"
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)       # digits only
    email_hashes: set[str] = field(default_factory=set)
    audit_ids: list[str] = field(default_factory=list)  # identity changes used, for citation

    def tokens(self) -> set[str]:
        """Identifiers in the normalized form Observation.identifiers() produces."""
        return self.emails | self.email_hashes | {self.user_id} | {"tel:" + p for p in self.phones}


class IdentityIndex:
    def __init__(self, ds: Dataset):
        self.ds = ds
        self.by_email: dict[str, set[str]] = {}
        for uid, u in ds.identities.items():
            self.by_email.setdefault(norm_email(u["email"]), set()).add(uid)
        self.changes: dict[str, list[dict]] = {}
        for a in ds.audit_log:
            self.changes.setdefault(a["user_id"], []).append(a)
            if a["field_name"] == "email":
                for v in (a["old_value"], a["new_value"]):
                    self.by_email.setdefault(norm_email(v), set()).add(a["user_id"])

    def resolve(self, email: str, audit_at: str) -> Identity | None:
        owners = self.by_email.get(norm_email(email), set())
        if len(owners) != 1:
            return None   # unknown or ambiguous: never guess a person
        uid = next(iter(owners))
        current = self.ds.identities[uid]

        # Walk back from the current snapshot: undo every change made after audit_at.
        state = {f: current[f] for f in ("email", "phone", "email_hash")}
        for a in reversed(self.changes.get(uid, [])):
            if a["event_time"] > audit_at:
                state[a["field_name"]] = a["old_value"]
        values = {f: {v} for f, v in state.items()}
        used = []
        for a in self.changes.get(uid, []):
            if a["event_time"] <= audit_at:
                values[a["field_name"]] |= {a["old_value"], a["new_value"]}
                used.append(a["audit_id"])

        emails = {norm_email(e) for e in values["email"]}
        ident = Identity(
            user_id=uid,
            name=current["name"],
            matched_via="current_email" if norm_email(email) == norm_email(state["email"]) else "historical_email",
            emails=emails,
            phones={norm_phone(p) for p in values["phone"]},
            email_hashes=set(values["email_hash"]) | {email_hash(e) for e in emails},
            audit_ids=used,
        )
        return ident
