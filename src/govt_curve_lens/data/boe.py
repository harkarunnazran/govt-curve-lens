"""Fetch and parse Bank of England nominal gilt curves (GLC)."""
import io
import logging
import zipfile
from datetime import date, timedelta

import holidays
import pandas as pd
import requests

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/glcnominalddata.zip"
LATEST_URL = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip"

# BoE renames sheets between files ("4. nominal spot curve" vs "4. spot curve"),
# so match on a keyword instead of the exact name.
SHEETS = {"spot": "spot curve", "forward": "fwd curve"}

# Placeholder: business days between curve_date and the Bank publishing it.
# Not yet confirmed against the Bank's release timing; change once known.
BOE_PUBLICATION_LAG_BUSINESS_DAYS = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def fetch_zip(url: str, min_bytes: int = 100_000) -> zipfile.ZipFile:
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    size = len(resp.content)
    log.info("fetched %s: %s, %s bytes", url, ctype, size)
    if size < min_bytes:
        raise ValueError(f"file too small ({size} bytes): {url}")
    if not zipfile.is_zipfile(io.BytesIO(resp.content)):
        raise ValueError(f"not a zip file: {url} ({ctype})")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def nominal_daily_files(zf: zipfile.ZipFile) -> list[str]:
    """Names of the nominal daily .xlsx files in the zip."""
    names = [
        n for n in zf.namelist()
        if n.lower().endswith(".xlsx") and "nominal" in n.lower() and "daily" in n.lower()
    ]
    if not names:
        raise ValueError(f"no nominal daily files in zip: {zf.namelist()}")
    return sorted(names)


def resolve_sheet(xlsx_bytes: bytes, keyword: str) -> str:
    """Actual sheet name containing `keyword` (case-insensitive)."""
    names = pd.ExcelFile(io.BytesIO(xlsx_bytes)).sheet_names
    matches = [n for n in names if keyword in n.lower()]
    if len(matches) != 1:
        raise ValueError(f"expected one sheet matching {keyword!r}, found {matches} in {names}")
    return matches[0]


def parse_sheet(xlsx_bytes: bytes, sheet: str, curve_type: str) -> pd.DataFrame:
    """Wide BoE sheet -> long DataFrame (curve_date, maturity_years, rate, curve_type)."""
    raw = pd.read_excel(io.BytesIO(xlsx_bytes), sheet_name=sheet, header=None)

    # The maturities row is the one labelled "years:" in column 0.
    label = raw.iloc[:, 0].astype(str).str.strip().str.lower()
    years_rows = raw.index[label == "years:"]
    if len(years_rows) != 1:
        raise ValueError(f"{sheet}: expected one 'years:' row, found {len(years_rows)}")
    maturities = raw.iloc[years_rows[0], 1:]

    # Data rows are the ones whose column 0 is a date.
    dates = pd.to_datetime(raw.iloc[:, 0], errors="coerce")
    data = raw[dates.notna()].copy()
    data.columns = ["curve_date", *maturities.tolist()]
    data["curve_date"] = pd.to_datetime(data["curve_date"]).dt.date

    long = data.melt(id_vars="curve_date", var_name="maturity_years", value_name="rate")
    long = long.dropna(subset=["maturity_years", "rate"])
    long["maturity_years"] = long["maturity_years"].astype(float).round(4)
    long["rate"] = long["rate"].astype(float)
    long["curve_type"] = curve_type
    return long.reset_index(drop=True)


GB_HOLIDAYS = holidays.country_holidays("GB", subdiv="ENG")


def published_on(curve_date: date, lag_business_days: int = BOE_PUBLICATION_LAG_BUSINESS_DAYS) -> date:
    """The date `lag_business_days` business days after curve_date, skipping weekends and English bank holidays."""
    day, remaining = curve_date, lag_business_days
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in GB_HOLIDAYS:
            remaining -= 1
    return day


def add_publication_dates(df: pd.DataFrame, lag: int = BOE_PUBLICATION_LAG_BUSINESS_DAYS) -> pd.DataFrame:
    """Estimate published_at for each row from its curve_date (one lookup per distinct date)."""
    out = df.copy()
    by_date = {d: published_on(d, lag) for d in out["curve_date"].unique()}
    out["published_at"] = out["curve_date"].map(by_date)
    out["published_at_source"] = "estimated"
    return out


def parse_zip(zf: zipfile.ZipFile) -> pd.DataFrame:
    frames = []
    for name in nominal_daily_files(zf):
        xlsx = zf.read(name)
        for curve_type, keyword in SHEETS.items():
            sheet = resolve_sheet(xlsx, keyword)
            df = parse_sheet(xlsx, sheet, curve_type)
            log.info("%s / %s: %s rows", name, sheet, len(df))
            frames.append(df)
    return pd.concat(frames, ignore_index=True)