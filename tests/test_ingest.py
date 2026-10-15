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
