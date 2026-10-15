"""Download T-100 data and its support tables from BTS TranStats."""

import os
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd

from .config import Settings, month_path
from .schema import normalize

MARKET_URL = (
    "https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMF&QO_fu146_anzr=Nv4%20Pn44vr45"
)
SEGMENT_URL = (
    "https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMG&QO_fu146_anzr=Nv4%20Pn44vr45"
)


def _setup_browser(download_dir: str):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        },
    )
    driver = webdriver.Chrome(options=opts)
    # BTS can take well over Selenium's default 120s to build a full-year file on click.
    driver.command_executor.set_timeout(600)
    return driver


def _wait_for_download(path: str, timeout: int = 180) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(path):
            s1 = os.path.getsize(path)
            time.sleep(2)
            if s1 == os.path.getsize(path):
                return True
        time.sleep(1)
    return False


def _extract_csv(zip_path: str) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path, "r") as z:
        csv_name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        with z.open(csv_name) as f:
            # Read everything as text; schema.normalize applies the real types.
            return pd.read_csv(f, dtype=str, keep_default_na=True)


def _retry_select_by_value(driver, select_id: str, value: str, retries: int = 5, wait: int = 10):
    """Select <option value=value> on <select id=select_id>, retrying while options repopulate."""
    from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import Select, WebDriverWait

    for _ in range(retries):
        try:
            sel_el = WebDriverWait(driver, wait).until(
                EC.presence_of_element_located((By.ID, select_id))
            )
            WebDriverWait(driver, wait).until(EC.element_to_be_clickable((By.ID, select_id)))
            Select(sel_el).select_by_value(str(value))
            if Select(sel_el).first_selected_option.get_attribute("value") == str(value):
                return
        except (StaleElementReferenceException, TimeoutException):
            time.sleep(0.5)
            continue
        except Exception:
            # Fall back to setting the value with JavaScript.
            driver.execute_script(
                "var s=document.getElementById(arguments[0]);"
                "if(s){s.value=arguments[1]; s.dispatchEvent(new Event('change'));}",
                select_id,
                str(value),
            )
            try:
                sel_el = WebDriverWait(driver, wait).until(
                    EC.presence_of_element_located((By.ID, select_id))
                )
                if Select(sel_el).first_selected_option.get_attribute("value") == str(value):
                    return
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError(f"Failed to set {select_id}={value} after {retries} attempts")


def _wait_option_present(driver, select_id: str, value: str, timeout: int = 10):
    """Wait until a select has an <option> with the given value."""
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def has_option(drv):
        try:
            el = drv.find_element(By.ID, select_id)
            return any(
                o.get_attribute("value") == str(value)
                for o in el.find_elements(By.TAG_NAME, "option")
            )
        except StaleElementReferenceException:
            return False

    WebDriverWait(driver, timeout).until(has_option)


def _click_download(driver, download_dir: str) -> str | None:
    """Click the form's Download button and return the path of the zip it produces."""
    from selenium.webdriver.common.by import By

    before = set(os.listdir(download_dir))
    driver.find_element(By.ID, "btnDownload").click()
    for _ in range(180):
        zips = [f for f in set(os.listdir(download_dir)) - before if f.lower().endswith(".zip")]
        if zips:
            candidate = os.path.join(download_dir, zips[0])
            if _wait_for_download(candidate):
                return candidate
        time.sleep(1)
    return None


def download_period(dataset: str, year: int, period: str) -> pd.DataFrame:
    """Drive the BTS download form for one year and return the raw rows.

    period is a month number ("1".."12") or "All" for every month of the year in one file.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    base_url = MARKET_URL if dataset == "market" else SEGMENT_URL
    with tempfile.TemporaryDirectory(prefix="t100_") as download_dir:
        driver = _setup_browser(download_dir)
        try:
            driver.get(base_url)
            wait = WebDriverWait(driver, 30)
            wait.until(EC.presence_of_element_located((By.ID, "cboYear")))
            wait.until(EC.presence_of_element_located((By.ID, "cboPeriod")))
            all_vars = driver.find_element(By.ID, "chkAllVars")
            if not all_vars.is_selected():
                all_vars.click()

            _retry_select_by_value(driver, "cboYear", str(year))
            _wait_option_present(driver, "cboPeriod", period)
            _retry_select_by_value(driver, "cboPeriod", period)

            zip_path = _click_download(driver, download_dir)
            if not zip_path:
                raise RuntimeError(f"Failed to download {dataset} {year} (period {period})")
            return _extract_csv(zip_path)
        finally:
            try:
                driver.quit()
            except Exception:
                pass


def write_month(df: pd.DataFrame, path: Path) -> None:
    """Write atomically, so an interrupted run never leaves a half-written month behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)


def scrape_month(
    dataset: str, year: int, month: int, settings: Settings, force: bool = False
) -> Path:
    """Download, normalize and save one month. Skips months already on disk unless forced."""
    assert dataset in ("market", "segment")
    path = month_path(settings.data_dir, dataset, year, month)
    if path.exists() and not force:
        print(f"Skip, already on disk: {path}")
        return path
    raw = download_period(dataset, year, str(month))
    if raw.empty:
        raise RuntimeError(f"BTS returned no rows for {dataset} {year}-{month:02d}")
    df = normalize(raw, year, month)
    write_month(df, path)
    print(f"Saved {len(df):,} rows to {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path


def scrape_year(dataset: str, year: int, months: list[int], settings: Settings) -> list[Path]:
    """Download a whole year in one BTS request and save the requested months.

    Months already on disk are kept as they are. Stops at the first month BTS has not
    published yet, so the returned list is always a run of months from the start of `months`.
    """
    assert dataset in ("market", "segment")
    paths: list[Path] = []
    missing = [m for m in months if not month_path(settings.data_dir, dataset, year, m).exists()]
    if not missing:
        return [month_path(settings.data_dir, dataset, year, m) for m in months]

    raw = None
    for attempt in range(1, 4):
        try:
            raw = download_period(dataset, year, "All")
            break
        except Exception as e:
            # Transient BTS or browser failures: try again with a fresh browser before giving up.
            print(f"Download of {dataset} {year} failed (attempt {attempt}/3): {e}")
            if attempt == 3:
                raise
            time.sleep(30)
    by_name = {str(c).strip().upper(): c for c in raw.columns}
    month_values = pd.to_numeric(raw[by_name["MONTH"]], errors="coerce")

    for m in months:
        path = month_path(settings.data_dir, dataset, year, m)
        if path.exists():
            paths.append(path)
            continue
        month_rows = raw[month_values == m]
        if month_rows.empty:
            print(f"BTS has no {dataset} rows for {year}-{m:02d} yet")
            break
        df = normalize(month_rows, year, m)
        write_month(df, path)
        print(f"Saved {len(df):,} rows to {path} ({path.stat().st_size / 1e6:.1f} MB)")
        paths.append(path)
    return paths
