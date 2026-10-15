"""Enriched views: raw T-100 rows with every code decoded into readable names.

`<project>.t100.segment` and `<project>.t100.market` are what the agent queries. They are
views, not tables:
- They cost no storage, which matters inside the 10 GiB free tier.
- They are never stale. A month loaded into raw shows up at once, with no refresh step.
- A filter on YEAR still prunes the raw table's partitions, so queries scan no more than before.
  The lookup tables they join are a few MB.

Every join is a LEFT JOIN on a key that is unique in its lookup, so a view has exactly as many
rows as its raw table. The old Snowflake job used SELECT DISTINCT, which silently dropped real
rows that happened to be identical; these views keep them.
"""

from dataclasses import dataclass

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .bigquery_load import table_id
from .config import Settings
from .lookups import ref_table_id
from .schema import INTEGER_COLUMNS


@dataclass(frozen=True)
class Join:
    raw_column: str
    ref_table: str
    ref_key: str
    # (column in the reference table, name in the view)
    columns: tuple[tuple[str, str], ...]
    # BTS lookups keep codes as text, sometimes zero-padded ("007" for aircraft type 7),
    # so integer raw columns are matched against the code read as a number.
    code_lookup: bool = True


def _desc(raw_column: str, ref_table: str, name: str) -> Join:
    return Join(raw_column, ref_table, "CODE", (("DESCRIPTION", name),))


JOINS: tuple[Join, ...] = (
    _desc("CARRIER_GROUP", "l_carrier_group", "CARRIER_GROUP_DESC"),
    _desc("CARRIER_GROUP_NEW", "l_carrier_group_new", "CARRIER_GROUP_NEW_DESC"),
    _desc("REGION", "l_region", "CARRIER_REGION_DESC"),
    _desc("CLASS", "l_service_class", "CLASS_DESC"),
    _desc("DISTANCE_GROUP", "l_distance_group_500", "DISTANCE_GROUP_DESC"),
    _desc("DATA_SOURCE", "l_data_source", "DATA_SOURCE_DESC"),
    _desc("AIRCRAFT_GROUP", "l_aircraft_group", "AIRCRAFT_GROUP_DESC"),
    _desc("AIRCRAFT_TYPE", "l_aircraft_type", "AIRCRAFT_TYPE_DESC"),
    _desc("AIRCRAFT_CONFIG", "l_aircraft_config", "AIRCRAFT_CONFIG_DESC"),
    _desc("ORIGIN_CITY_MARKET_ID", "l_city_market_id", "ORIGIN_CITY_MARKET_NAME"),
    _desc("DEST_CITY_MARKET_ID", "l_city_market_id", "DEST_CITY_MARKET_NAME"),
    _desc("ORIGIN_WAC", "l_world_area_codes", "ORIGIN_WAC_NAME"),
    _desc("DEST_WAC", "l_world_area_codes", "DEST_WAC_NAME"),
    *(
        Join(
            f"{side}_AIRPORT_SEQ_ID",
            "master_coordinate",
            "AIRPORT_SEQ_ID",
            (
                ("DISPLAY_AIRPORT_NAME", f"{side}_AIRPORT_NAME"),
                ("DISPLAY_AIRPORT_CITY_NAME_FULL", f"{side}_AIRPORT_CITY_FULL"),
                ("LATITUDE", f"{side}_LATITUDE"),
                ("LONGITUDE", f"{side}_LONGITUDE"),
            ),
            code_lookup=False,
        )
        for side in ("ORIGIN", "DEST")
    ),
    Join(
        "AIRCRAFT_TYPE",
        "aircraft_classification",
        "AIRCRAFT_TYPE",
        tuple(
            (c, c)
            for c in (
                "AIRCRAFT_MODEL",
                "AIRCRAFT_FAMILY",
                "AIRCRAFT_GENERATION",
                "NUM_ENGINES",
                "AIRCRAFT_CATEGORY",
                "AIRCRAFT_WING_TYPE",
                "AIRCRAFT_PROPULSION",
                "OEM",
                "OEM_COUNTRY",
            )
        ),
        code_lookup=False,
    ),
)


def view_sql(
    raw_table: str, raw_columns: set[str], ref_tables: dict[str, str]
) -> tuple[str, list[Join]]:
    """SELECT for one enriched view, and the joins it had to skip.

    raw_table: full id of the raw table. raw_columns: its column names.
    ref_tables: reference table name -> full id, for the tables that exist in BigQuery.
    A join is skipped when the raw table lacks its column (market has no aircraft columns)
    or its reference table was not loaded.
    """
    select = ["r.*"]
    from_ = [f"FROM `{raw_table}` AS r"]
    skipped = []
    for i, j in enumerate(JOINS):
        if j.raw_column not in raw_columns:
            continue
        if j.ref_table not in ref_tables:
            skipped.append(j)
            continue
        alias = f"j{i}"
        right = f"{alias}.{j.ref_key}"
        if j.code_lookup and j.raw_column in INTEGER_COLUMNS:
            right = f"SAFE_CAST({right} AS INT64)"
        from_.append(
            f"LEFT JOIN `{ref_tables[j.ref_table]}` AS {alias} ON r.{j.raw_column} = {right}"
        )
        select += [f"{alias}.{src} AS {name}" for src, name in j.columns]
    sql = "SELECT\n  " + ",\n  ".join(select) + "\n" + "\n".join(from_)
    return sql, skipped


def create_views(client: bigquery.Client, settings: Settings) -> None:
    ds = bigquery.Dataset(f"{settings.gcp_project}.{settings.bq_views_dataset}")
    ds.location = settings.bq_location
    client.create_dataset(ds, exists_ok=True)

    ref_tables = {
        t.table_id: ref_table_id(settings, t.table_id)
        for t in client.list_tables(f"{settings.gcp_project}.{settings.bq_ref_dataset}")
    }
    for dataset in ("segment", "market"):
        raw = table_id(settings, dataset)
        try:
            raw_columns = {f.name for f in client.get_table(raw).schema}
        except NotFound:
            print(f"Skip {dataset}: {raw} does not exist")
            continue
        sql, skipped = view_sql(raw, raw_columns, ref_tables)
        view_id = f"{settings.gcp_project}.{settings.bq_views_dataset}.{dataset}"
        # CREATE OR REPLACE VIEW scans nothing, so it is free.
        client.query(f"CREATE OR REPLACE VIEW `{view_id}` AS\n{sql}").result()
        print(f"Created view {view_id}")
        for j in skipped:
            print(f"    not joined: {j.raw_column} (reference table {j.ref_table} not loaded)")
