"""Step 5: the audit_at cutoff. Only information available at or before audit_at counts.

Timestamps are uniform `YYYY-MM-DDTHH:MM:SSZ` strings (checked by `verify`), so string
comparison is time comparison.
"""
from .data import Dataset
from .evidence.model import Observation


def activity_until(ds: Dataset, audit_at: str) -> list[dict]:
    return [a for a in ds.activity if a["event_time"] <= audit_at]


def evidence_until(evidence: dict[str, list[Observation]], audit_at: str) -> dict[str, list[Observation]]:
    """file_id -> observations knowable at audit_at.

    A file captured after audit_at is dropped entirely. Inside a file, an observation dated
    after audit_at is dropped too (available_at = max(file capture, row time)).
    """
    out = {}
    for file_id, obs in evidence.items():
        kept = [o for o in obs if o.available_at <= audit_at]
        if kept:
            out[file_id] = kept
    return out
