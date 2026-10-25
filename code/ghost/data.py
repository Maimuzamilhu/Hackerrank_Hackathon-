"""Step 1: load every participant-facing CSV from dataset/.

Only files under dataset/ are read. Timestamps stay as ISO-8601 strings; they all use the
same `YYYY-MM-DDTHH:MM:SSZ` shape, so string comparison orders them correctly.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DATASET_DIR


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Dataset:
    requests: list[dict]
    sample_requests: list[dict]
    sample_output: list[dict]
    identities: dict[str, dict]           # user_id -> current identity row
    audit_log: list[dict]                 # identity change history, time-ordered
    activity: list[dict]                  # global system activity, time-ordered
    rules: dict[str, dict]                # data_category -> rule row
    manifest: dict[str, dict]             # file_id -> manifest row
    activity_by_id: dict[str, dict] = field(default_factory=dict)

    def evidence_path(self, file_id: str) -> Path:
        return DATASET_DIR / self.manifest[file_id]["file_path"]


def load_dataset(root: Path = DATASET_DIR) -> Dataset:
    identities = {row["user_id"]: row for row in read_csv(root / "user_identities.csv")}
    audit_log = sorted(read_csv(root / "user_audit_log.csv"), key=lambda r: (r["event_time"], r["audit_id"]))
    activity = sorted(read_csv(root / "system_activity.csv"), key=lambda r: (r["event_time"], r["activity_id"]))
    rules = {row["data_category"]: row for row in read_csv(root / "deletion_rules.csv")}
    manifest = {row["file_id"]: row for row in read_csv(root / "evidence_files.csv")}
    return Dataset(
        requests=sorted(read_csv(root / "deletion_requests.csv"), key=lambda r: r["case_id"]),
        sample_requests=sorted(read_csv(root / "sample_deletion_requests.csv"), key=lambda r: r["case_id"]),
        sample_output=read_csv(root / "sample_output.csv"),
        identities=identities,
        audit_log=audit_log,
        activity=activity,
        rules=rules,
        manifest=manifest,
        activity_by_id={row["activity_id"]: row for row in activity},
    )
