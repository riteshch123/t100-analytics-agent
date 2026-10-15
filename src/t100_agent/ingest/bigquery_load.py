"""Load monthly Parquet files into BigQuery raw tables.

Tables are `<project>.<dataset>.raw_market` and `raw_segment`, partitioned by YEAR and
clustered by MONTH and carrier, so a query for a few years scans only those partitions.
Load jobs are free in BigQuery; only queries and storage count against the free tier.
Every query here sets maximum_bytes_billed, so a mistake fails instead of costing money.
"""

from pathlib import Path

import pyarrow.parquet as pq
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .config import Settings

PARTITION_RANGE = bigquery.PartitionRange(start=1990, end=2051, interval=1)


def table_id(settings: Settings, dataset: str) -> str:
    return f"{settings.gcp_project}.{settings.bq_dataset}.raw_{dataset}"


def get_client(settings: Settings) -> bigquery.Client:
    if not settings.gcp_project:
        raise ValueError("Set GCP_PROJECT to your Google Cloud project id")
    return bigquery.Client(project=settings.gcp_project, location=settings.bq_location)


def ensure_dataset(client: bigquery.Client, settings: Settings) -> None:
    ds = bigquery.Dataset(f"{settings.gcp_project}.{settings.bq_dataset}")
    ds.location = settings.bq_location
    client.create_dataset(ds, exists_ok=True)


def _query(client: bigquery.Client, settings: Settings, sql: str, params: list) -> list:
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=settings.max_bytes_billed,
    )
    return list(client.query(sql, job_config=job_config).result())


def month_row_count(
    client: bigquery.Client, settings: Settings, dataset: str, year: int, month: int
) -> int:
    """Rows already loaded for one month, or 0 when the table does not exist yet."""
    try:
        client.get_table(table_id(settings, dataset))
    except NotFound:
        return 0
    rows = _query(
        client,
        settings,
        f"SELECT COUNT(*) AS n FROM `{table_id(settings, dataset)}` WHERE YEAR = @y AND MONTH = @m",
        [
            bigquery.ScalarQueryParameter("y", "INT64", year),
            bigquery.ScalarQueryParameter("m", "INT64", month),
        ],
    )
    return rows[0]["n"]


def loaded_months(
    client: bigquery.Client, settings: Settings, dataset: str
) -> set[tuple[int, int]]:
    """Every (year, month) present in a raw table."""
    try:
        client.get_table(table_id(settings, dataset))
    except NotFound:
        return set()
    rows = _query(
        client,
        settings,
        f"SELECT DISTINCT YEAR AS y, MONTH AS m FROM `{table_id(settings, dataset)}`",
        [],
    )
    return {(r["y"], r["m"]) for r in rows}


def load_month(
    client: bigquery.Client,
    settings: Settings,
    dataset: str,
    year: int,
    month: int,
    path: Path,
    replace: bool = False,
) -> int:
    """Append one month's Parquet file and verify the row count. Returns rows loaded.

    A month that is already loaded is skipped, unless replace=True, which deletes it first.
    """
    expected = pq.ParquetFile(path).metadata.num_rows
    existing = month_row_count(client, settings, dataset, year, month)
    if existing and not replace:
        print(f"Skip, {dataset} {year}-{month:02d} already in BigQuery ({existing:,} rows)")
        return 0
    if existing and replace:
        _query(
            client,
            settings,
            f"DELETE FROM `{table_id(settings, dataset)}` WHERE YEAR = @y AND MONTH = @m",
            [
                bigquery.ScalarQueryParameter("y", "INT64", year),
                bigquery.ScalarQueryParameter("m", "INT64", month),
            ],
        )

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    try:
        client.get_table(table_id(settings, dataset))
        # A column BTS adds later becomes a new nullable column instead of a failed load.
        job_config.schema_update_options = [bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION]
    except NotFound:
        # Partitioning and clustering can only be set when the table is created.
        job_config.range_partitioning = bigquery.RangePartitioning(
            field="YEAR", range_=PARTITION_RANGE
        )
        job_config.clustering_fields = ["MONTH", "UNIQUE_CARRIER"]
    with open(path, "rb") as f:
        client.load_table_from_file(f, table_id(settings, dataset), job_config=job_config).result()

    loaded = month_row_count(client, settings, dataset, year, month)
    if loaded != expected:
        raise RuntimeError(
            f"{dataset} {year}-{month:02d}: file has {expected:,} rows but BigQuery has {loaded:,}"
        )
    print(f"Loaded {loaded:,} rows into {table_id(settings, dataset)} for {year}-{month:02d}")
    return loaded
