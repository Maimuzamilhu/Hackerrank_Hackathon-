"""Step 2: parse every evidence file listed in evidence_files.csv into Observations."""
from .documents import parse_image, parse_pdf, parse_text
from .jsonrec import parse_json
from .logs import parse_log
from .model import Observation
from .sql import parse_sql
from ..data import Dataset

PARSERS = {
    ".sql": parse_sql,
    ".json": parse_json,
    ".log": parse_log,
    ".txt": parse_text,
    ".pdf": parse_pdf,
    ".png": parse_image,
    ".jpg": parse_image,
    ".jpeg": parse_image,
}


def load_evidence(ds: Dataset) -> dict[str, list[Observation]]:
    """file_id -> observations, in manifest order."""
    out = {}
    for file_id in sorted(ds.manifest):
        path = ds.evidence_path(file_id)
        parser = PARSERS.get(path.suffix.lower())
        if parser is None:
            raise ValueError(f"No parser for {path.name} ({file_id})")
        observations = parser(file_id, path)
        captured_at = ds.manifest[file_id]["captured_at"]
        for o in observations:
            o.available_at = max(captured_at, o.observed_at) if o.observed_at else captured_at
        out[file_id] = observations
    return out
