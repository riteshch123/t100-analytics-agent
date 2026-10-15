"""Fixed column types for the raw T-100 files.

BTS files are read with pandas, whose type inference changes from month to month
(a column that is empty in one month comes back as float, text in another). BigQuery
rejects appends whose types differ from the table, so every month is cast to this one
schema before it is written. Columns not listed here are kept as text.
"""

import pandas as pd

DATASETS = ("market", "segment")

# Columns that are text in both datasets.
TEXT_COLUMNS = {
    "UNIQUE_CARRIER",
    "UNIQUE_CARRIER_NAME",
    "UNIQUE_CARRIER_ENTITY",
    "REGION",
    "CARRIER",
    "CARRIER_NAME",
    "ORIGIN",
    "ORIGIN_CITY_NAME",
    "ORIGIN_STATE_ABR",
    "ORIGIN_STATE_NM",
    "ORIGIN_COUNTRY",
    "ORIGIN_COUNTRY_NAME",
    "DEST",
    "DEST_CITY_NAME",
    "DEST_STATE_ABR",
    "DEST_STATE_NM",
    "DEST_COUNTRY",
    "DEST_COUNTRY_NAME",
    "CLASS",
    "DATA_SOURCE",
}

# Whole-number codes and IDs. Nullable, so a missing value stays missing.
INTEGER_COLUMNS = {
    "AIRLINE_ID",
    "CARRIER_GROUP",
    "CARRIER_GROUP_NEW",
    "ORIGIN_AIRPORT_ID",
    "ORIGIN_AIRPORT_SEQ_ID",
    "ORIGIN_CITY_MARKET_ID",
    "ORIGIN_STATE_FIPS",
    "ORIGIN_WAC",
    "DEST_AIRPORT_ID",
    "DEST_AIRPORT_SEQ_ID",
    "DEST_CITY_MARKET_ID",
    "DEST_STATE_FIPS",
    "DEST_WAC",
    "AIRCRAFT_GROUP",
    "AIRCRAFT_TYPE",
    "AIRCRAFT_CONFIG",
    "YEAR",
    "QUARTER",
    "MONTH",
    "DISTANCE_GROUP",
}

# Measures that are summed by the agent.
FLOAT_COLUMNS = {
    "DEPARTURES_SCHEDULED",
    "DEPARTURES_PERFORMED",
    "PAYLOAD",
    "SEATS",
    "PASSENGERS",
    "FREIGHT",
    "MAIL",
    "DISTANCE",
    "RAMP_TO_RAMP",
    "AIR_TIME",
}

# Text values that mean "missing". The old scraper's astype(str) wrote 'nan' and 'None'.
MISSING_TEXT = {"", "nan", "NaN", "None", "NULL", "<NA>"}


def normalize(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """Return the month's rows with upper-case names, fixed types and real nulls."""
    df = df.copy()
    df.columns = [str(c).strip().upper() for c in df.columns]

    # BTS CSVs end with a trailing comma, which pandas reads as an empty "Unnamed" column.
    unnamed = [c for c in df.columns if c.startswith("UNNAMED")]
    df = df.drop(columns=[c for c in unnamed if df[c].isna().all()])

    if "YEAR" not in df.columns:
        df["YEAR"] = year
    if "MONTH" not in df.columns:
        df["MONTH"] = month

    for col in df.columns:
        if col in INTEGER_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="raise").astype("Int64")
        elif col in FLOAT_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="raise").astype("Float64")
        else:
            text = df[col].astype("string").str.strip()
            df[col] = text.mask(text.isin(MISSING_TEXT))

    wrong = df[(df["YEAR"] != year) | (df["MONTH"] != month)]
    if len(wrong):
        raise ValueError(
            f"{len(wrong)} rows are not from {year}-{month:02d}; refusing to write this month"
        )
    return df
