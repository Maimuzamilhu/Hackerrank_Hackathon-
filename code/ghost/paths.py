"""Repository-relative paths, so the solution runs from any checkout location."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
CACHE_DIR = REPO_ROOT / "code" / "cache"
OUTPUT_PATH = REPO_ROOT / "output.csv"
