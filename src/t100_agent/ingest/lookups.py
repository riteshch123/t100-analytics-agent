"""Reference tables: BTS lookup tables, the Master Coordinate airport table and the aircraft
classification. They are downloaded to disk, then loaded into the `t100_ref` dataset, where
the enriched views join them to the raw tables.

BTS lookups are small CSVs with two columns, Code and Description. Each one decodes a coded
column in the raw data (CLASS "F" is "Scheduled Passenger/ Cargo Service F"). The download
links on the BTS form pages are obfuscated, so they are read off the page instead of built.
"""

import io
import re
import tempfile
import urllib.request
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

from .config import Settings
from .scrape import MARKET_URL, SEGMENT_URL

MASTER_COORDINATE_URL = (
    "https://www.transtats.bts.gov/DL_SelectFields.aspx"
    "?gnoyr_VQ=FLL&QO_fu146_anzr=N8vn6v10%20f722146%20gnoyr5"
)

# Committed to the repo: model, family, generation and maker for every BTS aircraft type code.
REPO_ROOT = Path(__file__).resolve().parents[3]
CLASSIFICATION_CSV = REPO_ROOT / "reference" / "aircraft_classification.csv"

# Master Coordinate columns the views use. Other columns are loaded too.
MASTER_COORDINATE_REQUIRED = {
    "AIRPORT_SEQ_ID",
    "DISPLAY_AIRPORT_NAME",
    "DISPLAY_AIRPORT_CITY_NAME_FULL",
    "LATITUDE",
    "LONGITUDE",
}

_LOOKUP_NAME = re.compile(r"(L_[A-Z0-9_]+?)\.csv", re.IGNORECASE)


def _lookup_links(driver, page_url: str) -> list[str]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(page_url)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "btnDownload")))
    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='Download_Lookup']")
    return [a.get_attribute("href") for a in links]


def _fetch_lookup(url: str) -> tuple[str, bytes]:
    """Download one lookup CSV. Returns (name such as 'L_CARRIER_GROUP', file bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        disposition = resp.headers.get("Content-Disposition", "")
        body = resp.read()
    match = _LOOKUP_NAME.search(disposition)
    if not match:
        raise RuntimeError(f"Could not tell which lookup {url} is (header: {disposition!r})")
    return match.group(1).upper(), body


def download_lookups(settings: Settings) -> list[Path]:
    """Save every lookup linked from the market and segment form pages, plus Master Coordinate."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    from .scrape import _click_download, _extract_csv, _setup_browser

    out = settings.lookups_dir
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="t100_") as download_dir:
        driver = _setup_browser(download_dir)
        try:
            links = []
            for page in (SEGMENT_URL, MARKET_URL):
                links += [u for u in _lookup_links(driver, page) if u not in links]
            print(f"Found {len(links)} lookup tables on the BTS form pages")
            for url in links:
                name, body = _fetch_lookup(url)
                path = out / f"{name}.csv"
                if path in saved:
                    continue  # the same lookup is linked from both pages
                path.write_bytes(body)
                saved.append(path)
                print(f"Saved {path}")

            # Master Coordinate is a full table with its own download form, not a lookup.
            driver.get(MASTER_COORDINATE_URL)
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "chkAllVars")))
            all_vars = driver.find_element(By.ID, "chkAllVars")
            if not all_vars.is_selected():
                all_vars.click()
            zip_path = _click_download(driver, download_dir)
            if not zip_path:
                raise RuntimeError("Failed to download Master Coordinate")
            path = out / "MASTER_COORDINATE.csv"
            _extract_csv(zip_path).to_csv(path, index=False)
            saved.append(path)
            print(f"Saved {path}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    return saved


def _read_csv_text(path: Path) -> pd.DataFrame:
    """Read a CSV as text columns. A few BTS lookups are Windows-1252, not UTF-8
    ("Scarlett Martínez" in the airport tables), so fall back to that."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def read_lookup(path: Path) -> pd.DataFrame:
    """A BTS lookup as CODE and DESCRIPTION text columns, one row per code."""
    df = _read_csv_text(path)
    df.columns = [c.strip().upper() for c in df.columns]
    if list(df.columns[:2]) != ["CODE", "DESCRIPTION"]:
        raise ValueError(f"{path.name}: expected Code and Description, got {list(df.columns)}")
    df = df[["CODE", "DESCRIPTION"]].apply(lambda col: col.str.strip())
    # A repeated code would duplicate raw rows in the join, so keep exactly one per code.
    dupes = df["CODE"].duplicated()
    if dupes.any():
        print(f"{path.name}: dropping {dupes.sum()} repeated codes")
    return df[~dupes]


def read_master_coordinate(path: Path) -> pd.DataFrame:
    """One row per AIRPORT_SEQ_ID. The table keeps history, so prefer the latest record."""
    df = _read_csv_text(path)
    df.columns = [str(c).strip().upper() for c in df.columns]
    df = df.drop(columns=[c for c in df.columns if c.startswith("UNNAMED")])
    missing = MASTER_COORDINATE_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Master Coordinate is missing {sorted(missing)}; has {list(df.columns)}")
    df = df.apply(lambda col: col.str.strip()).replace("", pd.NA)
    for col in ("AIRPORT_SEQ_ID", "AIRPORT_ID", "AIRPORT_WAC", "AIRPORT_IS_LATEST"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col]).astype("Int64")
    for col in ("LATITUDE", "LONGITUDE"):
        df[col] = pd.to_numeric(df[col]).astype("Float64")
    if "AIRPORT_IS_LATEST" in df.columns:
        df = df.sort_values("AIRPORT_IS_LATEST", ascending=False, kind="stable")
    return df.drop_duplicates("AIRPORT_SEQ_ID", keep="first").reset_index(drop=True)


def read_classification(path: Path = CLASSIFICATION_CSV) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"AIRCRAFT_TYPE": "Int64", "NUM_ENGINES": "Int64"})
    if not df["AIRCRAFT_TYPE"].is_unique:
        raise ValueError(f"{path.name}: AIRCRAFT_TYPE must be unique")
    text = [c for c in df.columns if c not in ("AIRCRAFT_TYPE", "NUM_ENGINES")]
    df[text] = df[text].astype("string")
    return df


def ref_table_id(settings: Settings, name: str) -> str:
    return f"{settings.gcp_project}.{settings.bq_ref_dataset}.{name.lower()}"


def load_reference(client: bigquery.Client, settings: Settings) -> list[str]:
    """Replace every reference table in BigQuery with what is on disk. Returns table names."""
    ds = bigquery.Dataset(f"{settings.gcp_project}.{settings.bq_ref_dataset}")
    ds.location = settings.bq_location
    client.create_dataset(ds, exists_ok=True)

    tables: dict[str, pd.DataFrame] = {"aircraft_classification": read_classification()}
    for path in sorted(settings.lookups_dir.glob("*.csv")):
        if path.stem == "MASTER_COORDINATE":
            tables["master_coordinate"] = read_master_coordinate(path)
        else:
            tables[path.stem.lower()] = read_lookup(path)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    for name, df in tables.items():
        tid = ref_table_id(settings, name)
        # Sent as Parquet, the same way the raw months are loaded, so types carry over exactly.
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        client.load_table_from_file(buf, tid, job_config=job_config).result()
        loaded = client.get_table(tid).num_rows
        if loaded != len(df):
            raise RuntimeError(f"{tid}: expected {len(df):,} rows, BigQuery has {loaded:,}")
        print(f"Loaded {loaded:,} rows into {tid}")
    return list(tables)
