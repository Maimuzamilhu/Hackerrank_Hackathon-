"""Plain-text records, PDFs and images -> Observations.

Text and PDF evidence becomes one document Observation (full text kept for matching), plus
one row Observation per pipe-separated table line such as
`- <time> | <system> | <resource_id> | <category> | <result> | <subject>`.

Images need a transcription. It is read from code/cache/images/<file stem>.txt, so the
pipeline stays deterministic, costs nothing at run time, and never re-reads an image once it
has been transcribed. Each transcription records the SHA-256 of its source image; a mismatch
fails loudly instead of silently using text from a different screenshot.
"""
import hashlib
import re
from pathlib import Path

from .model import RESOURCE_RE, Observation
from ..paths import CACHE_DIR
from ..systems import system_for_resource

IMAGE_CACHE = CACHE_DIR / "images"


def _doc(file_id: str, kind: str, text: str, **extra) -> Observation:
    return Observation(
        file_id=file_id, kind=kind, system_id=None, container=None, resource_id=None, observed_at=None,
        fields={"mentioned_resources": sorted(set(RESOURCE_RE.findall(text))), **extra}, text=text,
    )


def _pipe_rows(file_id: str, text: str) -> list[Observation]:
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().lstrip("-").split("|")]
        if len(cells) < 5 or not RESOURCE_RE.fullmatch(cells[2] or ""):
            continue
        time, system, resource, category, result, *rest = cells
        out.append(Observation(
            file_id=file_id, kind="text_row", system_id=system, container=category, resource_id=resource,
            observed_at=time, status=result.upper() if result.upper() in ("FOUND", "NOT_FOUND") else None,
            fields={"record_type": category, "result": result, "subject_reference": rest[0] if rest else None, "extra": rest[1:]},
            text=line,
        ))
    return out


def parse_text(file_id: str, path: Path) -> list[Observation]:
    text = path.read_text(encoding="utf-8")
    return [_doc(file_id, "text_doc", text), *_pipe_rows(file_id, text)]


def parse_pdf(file_id: str, path: Path) -> list[Observation]:
    import pymupdf
    with pymupdf.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    return [_doc(file_id, "pdf_doc", text), *_pipe_rows(file_id, text)]


def parse_image(file_id: str, path: Path) -> list[Observation]:
    cached = IMAGE_CACHE / (path.stem + ".txt")
    if not cached.exists():
        return [_doc(file_id, "image_doc", "", transcription_pending=True)]
    text = cached.read_text(encoding="utf-8")
    stamp = re.search(r"^# Source-SHA256: ([0-9a-f]{64})$", text, re.M)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not stamp or stamp[1] != actual:
        raise ValueError(f"{cached.name} was transcribed from a different image; re-transcribe {path.name}")
    return parse_transcription(file_id, text)


def parse_transcription(file_id: str, text: str) -> list[Observation]:
    """Screen transcription -> one image_doc (all `Key: value` pairs by section) plus one
    image_row per table row, keyed to the first resource id in the row."""
    # The `# ...` header (provenance + image checksum) is not screen content; keeping it out
    # of the text stops the checksum from being mistaken for a customer email hash.
    stamp = re.search(r"^# Source-SHA256: ([0-9a-f]{64})$", text, re.M)
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("# ")).strip() + "\n"
    sections: dict[str, dict] = {}
    rows: list[Observation] = []
    section, header = None, None
    for line in text.splitlines():
        if line.startswith("## "):
            section, header = line[3:].strip(), None
            sections[section] = {}
        elif section and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells
                continue
            row = dict(zip(header, cells))
            # Key the row by the first id of a known resource family; ids like user_03 or
            # file names (return_label_4821) are attributes, not the row's resource.
            resources = RESOURCE_RE.findall(line)
            owned = [x for x in resources if system_for_resource(x)]
            rows.append(Observation(
                file_id=file_id, kind="image_row", system_id=system_for_resource(owned[0]) if owned else None,
                container=section, resource_id=owned[0] if owned else None, observed_at=None,
                fields={"columns": row, "mentioned_resources": resources}, text=line,
            ))
        elif section and ": " in line:
            key, _, value = line.partition(": ")
            sections[section][key.strip()] = value.strip()
    doc = _doc(file_id, "image_doc", text, sections=sections, _source_sha256=stamp[1] if stamp else None)
    # Resources the screen is *about*: its header and detail panels. Activity feeds and
    # tables mention other records in passing (tables become their own rows above).
    about = "\n".join(f"{k}: {v}" for name, kv in sections.items()
                       if name == "Screen" or name.startswith("Panel") or name == "Summary cards" for k, v in kv.items())
    doc.fields["mentioned_resources"] = sorted(set(RESOURCE_RE.findall(about + "\n" + "\n".join(
        name for name in sections if name.startswith("Panel")))))
    return [doc, *rows]
