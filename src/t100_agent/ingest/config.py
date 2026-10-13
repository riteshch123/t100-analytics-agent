"""Settings for the ingestion pipeline, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    gcp_project: str
    bq_dataset: str
    bq_location: str
    data_dir: Path
    # Hard cap on bytes a single query may scan. BigQuery fails the query instead of billing more.
    max_bytes_billed: int

    @classmethod
    def from_env(cls) -> "Settings":
        project = os.environ.get("GCP_PROJECT", "")
        return cls(
            gcp_project=project,
            bq_dataset=os.environ.get("BQ_RAW_DATASET", "t100_raw"),
            bq_location=os.environ.get("BQ_LOCATION", "US"),
            data_dir=Path(os.environ.get("T100_DATA_DIR", "data/raw")),
            max_bytes_billed=int(os.environ.get("BQ_MAX_BYTES_BILLED", str(2 * 1024**3))),
        )


def month_path(data_dir: Path, dataset: str, year: int, month: int) -> Path:
    """Where one month's Parquet file lives on disk."""
    return data_dir / f"t100_{dataset}" / f"year={year}" / f"month={month:02d}" / "data.parquet"
