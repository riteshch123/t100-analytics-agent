from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from t100_agent.ingest.config import month_path
from t100_agent.ingest.run import _months
from t100_agent.ingest.schema import normalize
from t100_agent.ingest.scrape import write_month


def _raw_month(**overrides) -> pd.DataFrame:
    """A small month as pandas reads it from the BTS CSV with dtype=str."""
    data = {
        "YEAR": ["2024", "2024"],
        "MONTH": ["1", "1"],
        "ORIGIN": ["SPI", "YYZ"],
        "ORIGIN_STATE_ABR": ["IL", None],
        "UNIQUE_CARRIER": ["UA", "AC"],
        "AIRCRAFT_CONFIG": ["1", "2"],
        "DEPARTURES_PERFORMED": ["31.00", "4.00"],
        "Unnamed: 7": [None, None],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_missing_text_stays_null_not_nan_string():
    df = normalize(_raw_month(), 2024, 1)
    assert df["ORIGIN_STATE_ABR"].isna().tolist() == [False, True]
    assert "nan" not in df["ORIGIN_STATE_ABR"].dropna().tolist()


def test_nan_text_from_old_scraper_becomes_null():
    df = normalize(_raw_month(ORIGIN_STATE_ABR=["IL", "nan"]), 2024, 1)
    assert df["ORIGIN_STATE_ABR"].isna().tolist() == [False, True]


def test_types_are_fixed_regardless_of_input():
    df = normalize(_raw_month(), 2024, 1)
    assert str(df["YEAR"].dtype) == "Int64"
    assert str(df["AIRCRAFT_CONFIG"].dtype) == "Int64"
    assert str(df["DEPARTURES_PERFORMED"].dtype) == "Float64"
    assert str(df["ORIGIN"].dtype).startswith("string")


def test_empty_trailing_column_is_dropped():
    df = normalize(_raw_month(), 2024, 1)
    assert not any(c.startswith("UNNAMED") for c in df.columns)


def test_rows_from_another_month_are_rejected():
    with pytest.raises(ValueError, match="not from 2024-02"):
        normalize(_raw_month(), 2024, 2)


def test_month_written_as_parquet_keeps_types(tmp_path: Path):
    path = month_path(tmp_path, "segment", 2024, 1)
    write_month(normalize(_raw_month(), 2024, 1), path)
    schema = pq.read_schema(path)
    assert str(schema.field("YEAR").type) == "int64"
    assert str(schema.field("ORIGIN").type) in ("string", "large_string")
    assert path.as_posix().endswith("t100_segment/year=2024/month=01/data.parquet")


def test_month_range_crosses_year_boundary():
    assert list(_months((2023, 11), (2024, 2))) == [(2023, 11), (2023, 12), (2024, 1), (2024, 2)]


def test_lookup_keeps_one_row_per_code(tmp_path):
    from t100_agent.ingest.lookups import read_lookup

    path = tmp_path / "L_SERVICE_CLASS.csv"
    path.write_text('Code,Description\nF," Scheduled "\nF,Duplicate\nL,Charter\n')
    df = read_lookup(path)
    assert df.to_dict("records") == [
        {"CODE": "F", "DESCRIPTION": "Scheduled"},
        {"CODE": "L", "DESCRIPTION": "Charter"},
    ]


def test_lookup_reads_windows_1252(tmp_path):
    from t100_agent.ingest.lookups import read_lookup

    path = tmp_path / "L_AIRPORT.csv"
    path.write_bytes(
        'Code,Description\r\n"RIH","Rio Hato, Panama: Scarlett Martínez"\r\n'.encode("cp1252")
    )
    assert read_lookup(path).loc[0, "DESCRIPTION"] == "Rio Hato, Panama: Scarlett Martínez"


def test_padded_codes_join_integer_columns():
    from t100_agent.ingest.views import view_sql

    sql, _ = view_sql("p.raw.t", {"AIRCRAFT_TYPE"}, {"l_aircraft_type": "p.ref.l_aircraft_type"})
    assert "ON r.AIRCRAFT_TYPE = SAFE_CAST(j4.CODE AS INT64)" in sql


def test_master_coordinate_prefers_latest_record(tmp_path):
    from t100_agent.ingest.lookups import read_master_coordinate

    path = tmp_path / "MASTER_COORDINATE.csv"
    path.write_text(
        "AIRPORT_SEQ_ID,DISPLAY_AIRPORT_NAME,DISPLAY_AIRPORT_CITY_NAME_FULL,LATITUDE,LONGITUDE,"
        "AIRPORT_IS_LATEST,Unnamed: 6\n"
        '1495101,Old Name,"Springfield, IL",39.8,-89.6,0,\n'
        '1495101,Abraham Lincoln Capital,"Springfield, IL",39.84,-89.68,1,\n'
    )
    df = read_master_coordinate(path)
    assert len(df) == 1
    assert df.loc[0, "DISPLAY_AIRPORT_NAME"] == "Abraham Lincoln Capital"
    assert str(df["AIRPORT_SEQ_ID"].dtype) == "Int64"
    assert str(df["LATITUDE"].dtype) == "Float64"


def test_classification_file_is_valid():
    from t100_agent.ingest.lookups import read_classification

    df = read_classification()
    assert df["AIRCRAFT_TYPE"].is_unique
    assert len(df) > 400


def test_view_joins_only_what_exists():
    from t100_agent.ingest.views import view_sql

    raw_columns = {"CLASS", "AIRCRAFT_TYPE", "ORIGIN_AIRPORT_SEQ_ID", "PASSENGERS"}
    ref = {
        "l_service_class": "p.ref.l_service_class",
        "aircraft_classification": "p.ref.aircraft_classification",
        "master_coordinate": "p.ref.master_coordinate",
    }
    sql, skipped = view_sql("p.raw.raw_segment", raw_columns, ref)
    assert "ON r.CLASS = j2.CODE" in sql
    assert "r.AIRCRAFT_TYPE = j10.AIRCRAFT_TYPE" in sql and "AIRCRAFT_FAMILY" in sql
    assert "ORIGIN_AIRPORT_NAME" in sql and "DEST_AIRPORT_NAME" not in sql
    # AIRCRAFT_TYPE exists in raw but its BTS lookup was not loaded: reported, not joined.
    assert [j.ref_table for j in skipped] == ["l_aircraft_type"]
    assert "SELECT DISTINCT" not in sql
