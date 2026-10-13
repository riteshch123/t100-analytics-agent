"""Data checks on the BigQuery raw tables. Run after a load:

python -m t100_agent.ingest.checks
"""

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
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if run_checks(Settings.from_env()) else 1)
