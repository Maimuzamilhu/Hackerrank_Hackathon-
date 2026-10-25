"""The common shape every evidence parser produces."""
import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_REF_RE = re.compile(r"\buser_\d+\b")
RESOURCE_RE = re.compile(r"\b[a-z]+(?:_[a-z]+)*_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


@dataclass
class Observation:
    """One thing a piece of evidence says about one resource (or one document).

    `fields` holds every attribute exactly as the evidence states it; nothing is interpreted
    here. `status` is set only when the evidence states presence explicitly
    (FOUND / NOT_FOUND, HTTP 200 / 404, rows=0, ...).
    """
    file_id: str
    kind: str                       # sql_row | json_record | log_trace | text_row | pdf_doc | image_doc
    system_id: str | None
    container: str | None           # table, collection, or log service
    resource_id: str | None
    observed_at: str | None         # row-level time if the evidence gives one
    fields: dict = field(default_factory=dict)
    status: str | None = None
    text: str = ""
    # Earliest time this observation could be known: the later of the file's capture time
    # (manifest) and the observation's own time. Used for the audit_at cutoff.
    available_at: str | None = None

    def identifiers(self) -> set[str]:
        """Normalized identifiers mentioned anywhere in this observation."""
        blob = self.text + " " + " ".join(str(v) for v in _flatten(self.fields))
        ids = {m.lower() for m in EMAIL_RE.findall(blob)}
        ids |= set(HASH_RE.findall(blob.lower()))
        ids |= set(USER_REF_RE.findall(blob))
        ids |= {"tel:" + phone_digits(m) for m in PHONE_RE.findall(blob)}
        return ids


def _flatten(value):
    if isinstance(value, dict):
        # Keys starting with "_" are parser metadata (paths, provenance), not record content.
        for k, v in value.items():
            if not str(k).startswith("_"):
                yield from _flatten(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    elif value is not None:
        yield value
