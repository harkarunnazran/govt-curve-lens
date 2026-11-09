"""Fetch and parse the DMO 'Gilts in Issue' report (D1A)."""

import io
import re
from datetime import date, datetime
from fractions import Fraction

import pandas as pd
import requests

DMO_EXPORT_URL = "https://www.dmo.gov.uk/umbraco/surface/DataExport/GetDataExport"
XLS_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # old-style Excel (OLE2) file
UNICODE_FRACTIONS = {"¼": " 1/4", "½": " 1/2", "¾": " 3/4", "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8"}
ISIN_PATTERN = re.compile(r"^GB[0-9A-Z]{9}[0-9]$")


def fetch_gilts_in_issue(cob_date: date) -> bytes:
    """Download the raw D1A .xls file for one close-of-business date."""
    params = {
        "reportCode": "D1A",
        "exportFormatValue": "xls",
        "parameters": f"&COBDate={cob_date:%d/%m/%Y}",
    }
    response = requests.get(DMO_EXPORT_URL, params=params, timeout=30)
    response.raise_for_status()
    if not response.content.startswith(XLS_SIGNATURE):
        raise ValueError("DMO response is not an .xls file (maybe an error page)")
    return response.content


def parse_coupon(name: str) -> float:
    """'4 1/8% Treasury Gilt 2027' -> 4.125, '3¾% ...' -> 3.75, '6% ...' -> 6.0"""
    text = name.split("%")[0]
    for symbol, fraction in UNICODE_FRACTIONS.items():
        text = text.replace(symbol, fraction)
    return float(sum(Fraction(part) for part in text.split()))


def parse_gilts_in_issue(content: bytes) -> pd.DataFrame:
    """Turn the raw D1A file into one clean row per conventional gilt."""
    raw = pd.read_excel(io.BytesIO(content), engine="xlrd", header=None)

    # Row 0 holds both dates: when the data is true (COB) and when the file was made.
    cob_text = str(raw.iloc[0, 2]).split(" ON ")[-1].strip()      # '30 OCTOBER 2026'
    data_text = str(raw.iloc[0, 0]).split(":")[-1].strip()        # '03-Nov-2026'
    cob_date = datetime.strptime(cob_text.title(), "%d %B %Y").date()
    data_date = datetime.strptime(data_text, "%d-%b-%Y").date()

    first_col = raw[0].astype(str).str.strip()
    header_row = first_col[first_col.eq("Conventional Gilts")].index[0]

    records, bucket = [], None
    for _, row in raw.iloc[header_row + 1 :].iterrows():
        label = row[0]
        if pd.isna(label):
            continue
        label = str(label).strip()
        if label.lower().startswith(("index-linked", "note")):
            break  # end of the conventional section
        if pd.isna(row[1]):
            bucket = label  # section heading such as 'Ultra-Short' or 'Long'
            continue
        records.append(
            {
                "cob_date": cob_date,
                "data_date": data_date,
                "isin": str(row[1]).strip(),
                "name": label,
                "coupon_pct": parse_coupon(label),
                "maturity_date": pd.to_datetime(row[2]).date(),
                "first_issue_date": pd.to_datetime(row[3]).date(),
                "dividend_dates": str(row[4]).strip(),
                "next_ex_div_date": pd.to_datetime(row[5]).date(),
                "amount_in_issue_gbp_mn": float(row[6]),
                "bucket": bucket,
            }
        )

    df = pd.DataFrame(records)
    _validate(df)
    return df


def _validate(df: pd.DataFrame) -> None:
    """Reject bad data instead of storing it."""
    if df.empty:
        raise ValueError("No gilts parsed: has the file format changed?")
    bad_isins = df.loc[~df["isin"].str.match(ISIN_PATTERN), "isin"]
    if not bad_isins.empty:
        raise ValueError(f"Unexpected ISINs: {list(bad_isins)}")
    if df["isin"].duplicated().any():
        raise ValueError("Duplicate ISINs in one file")
    if (df["maturity_date"] <= df["cob_date"]).any():
        raise ValueError("A gilt in issue has already matured")
    if not df["coupon_pct"].between(0, 20).all():
        raise ValueError("Coupon out of range")

