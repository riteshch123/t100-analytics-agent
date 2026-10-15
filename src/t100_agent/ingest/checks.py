"""Data checks on the BigQuery raw tables and enriched views. Run after a load:

python -m t100_agent.ingest.checks
"""

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .bigquery_load import get_client, table_id
from .config import Settings

# Each check returns rows only when something is wrong.
CHECKS = {
    "months with no rows between the first and last month loaded": """
        WITH months AS (SELECT DISTINCT YEAR * 12 + MONTH - 1 AS k FROM `{t}`),
        bounds AS (SELECT MIN(k) AS lo, MAX(k) AS hi FROM months)
        SELECT DIV(k, 12) AS year, MOD(k, 12) + 1 AS month
        FROM bounds, UNNEST(GENERATE_ARRAY(lo, hi)) AS k
        WHERE k NOT IN (SELECT k FROM months)
    """,
    "text columns still holding 'nan' or 'None'": """
        SELECT * FROM (
            SELECT 'ORIGIN_STATE_ABR' AS col, COUNTIF(ORIGIN_STATE_ABR IN ('nan', 'None')) AS n
            FROM `{t}`
            UNION ALL
            SELECT 'DEST_STATE_ABR', COUNTIF(DEST_STATE_ABR IN ('nan', 'None')) FROM `{t}`
        )
        WHERE n > 0
    """,
}

# Informational: printed, but does not fail the run.
SEGMENT_INFO = {
    # The old Snowflake MERGE key left out AIRCRAFT_CONFIG. Rows that share that key are
    # legitimate and must not be merged; this shows how often the old pipeline would have collided.
    "rows sharing the old MERGE key (would have collided in Snowflake)": """
        SELECT YEAR, MONTH, ORIGIN, DEST, UNIQUE_CARRIER, CLASS, AIRCRAFT_TYPE, DATA_SOURCE,
               COUNT(*) AS n
        FROM `{t}`
        GROUP BY ALL
        HAVING n > 1
        LIMIT 20
    """,
}


# Enriched views, checked on the latest year only to keep the scan small.
VIEW_CHECKS = {
    # A lookup with a repeated key would multiply rows in the join.
    "rows in the view that differ from the raw table": """
        SELECT v.n AS view_rows, r.n AS raw_rows
        FROM (SELECT COUNT(*) AS n FROM `{v}` WHERE YEAR = @y) AS v,
             (SELECT COUNT(*) AS n FROM `{t}` WHERE YEAR = @y) AS r
        WHERE v.n != r.n
    """,
}

VIEW_INFO = {
    "airports with no name in Master Coordinate (top 5 by rows)": """
        SELECT ORIGIN, ORIGIN_AIRPORT_SEQ_ID, COUNT(*) AS n
        FROM `{v}` WHERE YEAR = @y AND ORIGIN_AIRPORT_NAME IS NULL
        GROUP BY ALL ORDER BY n DESC LIMIT 5
    """,
}

SEGMENT_VIEW_INFO = {
    "aircraft types missing from the classification (top 5 by passengers)": """
        SELECT AIRCRAFT_TYPE, ANY_VALUE(AIRCRAFT_TYPE_DESC) AS bts_name,
               ROUND(SUM(PASSENGERS)) AS passengers, COUNT(*) AS n
        FROM `{v}` WHERE YEAR = @y AND AIRCRAFT_FAMILY IS NULL
        GROUP BY AIRCRAFT_TYPE ORDER BY passengers DESC LIMIT 5
    """,
}


def _view_checks(client: bigquery.Client, settings: Settings, dataset: str) -> bool:
    view = f"{settings.gcp_project}.{settings.bq_views_dataset}.{dataset}"
    try:
        client.get_table(view)
    except NotFound:
        print(f"[{dataset} view] not created yet, skipping")
        return True
    raw = table_id(settings, dataset)
    cap = settings.max_bytes_billed
    year_job = bigquery.QueryJobConfig(maximum_bytes_billed=cap)
    year = list(client.query(f"SELECT MAX(YEAR) AS y FROM `{raw}`", job_config=year_job))[0]["y"]
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=cap,
        query_parameters=[bigquery.ScalarQueryParameter("y", "INT64", year)],
    )
    info = {**VIEW_INFO, **(SEGMENT_VIEW_INFO if dataset == "segment" else {})}
    ok = True
    for name, sql in {**VIEW_CHECKS, **info}.items():
        rows = list(client.query(sql.format(v=view, t=raw), job_config=job_config).result())
        status = "none" if not rows else f"{len(rows)} found"
        print(f"[{dataset} view, {year}] {name}: {status}")
        for r in rows[:5]:
            print("    ", dict(r))
        if name in VIEW_CHECKS:
            ok = ok and not rows
    return ok


def run_checks(settings: Settings) -> bool:
    client = get_client(settings)
    ok = True
    for dataset in ("market", "segment"):
        info = SEGMENT_INFO if dataset == "segment" else {}
        for name, sql in {**CHECKS, **info}.items():
            job_config = bigquery.QueryJobConfig(maximum_bytes_billed=settings.max_bytes_billed)
            rows = list(
                client.query(
                    sql.format(t=table_id(settings, dataset)), job_config=job_config
                ).result()
            )
            status = "none" if not rows else f"{len(rows)} found"
            print(f"[{dataset}] {name}: {status}")
            for r in rows[:5]:
                print("    ", dict(r))
            if name in CHECKS:
                ok = ok and not rows
        ok = _view_checks(client, settings, dataset) and ok
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if run_checks(Settings.from_env()) else 1)
