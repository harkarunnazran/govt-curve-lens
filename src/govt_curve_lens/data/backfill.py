"""Load DMO gilts-in-issue data into DuckDB.

Two entry points share one function, refresh_one():
  - backfill: month-end dates over a range (run once, re-run safely after failures)
  - daily:    the last working day that is at least 1 working day old (for cron)

Usage, from the repo root:
  uv run python -m govt_curve_lens.data.backfill backfill --start 2010-01-01 --end 2026-10-31
  uv run python -m govt_curve_lens.data.backfill daily
"""

import argparse
import calendar
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from govt_curve_lens.data.boe import (
    ARCHIVE_URL,
    LATEST_URL,
    add_publication_dates,
    fetch_zip,
    parse_zip,
)
from govt_curve_lens.data.dmo import fetch_gilts_in_issue, parse_gilts_in_issue

log = logging.getLogger(__name__)

# Anchored to the repo root so every entry point opens the same file.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "lab.duckdb"
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
SCHEMA_PATHS = [RAW_DIR / "create_gilt_table.sql", RAW_DIR / "create_boe_yield_curve.sql"]
BOE_URLS = {"archive": ARCHIVE_URL, "latest": LATEST_URL}

COLUMNS = """cob_date, data_date, isin, name, coupon_pct, maturity_date,
             first_issue_date, dividend_dates, next_ex_div_date,
             amount_in_issue_gbp_mn, bucket"""


# --- database ---------------------------------------------------------------

def connect(db_path: Path = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open the DuckDB file and make sure every table exists (safe to run every time)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    for schema_path in SCHEMA_PATHS:
        con.execute(schema_path.read_text())  # CREATE TABLE IF NOT EXISTS ...
    return con


def store_gilts(con: duckdb.DuckDBPyConnection, gilts_df: pd.DataFrame) -> int:
    """Insert one day's gilts; skip rows already stored. All or nothing."""
    con.register("gilts_df", gilts_df)

    con.execute("BEGIN TRANSACTION")
    try:
        # INSERT returns one row: the number of rows actually inserted.
        # fetchall() reads it fully, so no unfinished result blocks the next call.
        inserted = con.execute(f"""
            INSERT INTO gilt_data ({COLUMNS})
            SELECT {COLUMNS} FROM gilts_df
            ON CONFLICT (cob_date, isin) DO NOTHING
        """).fetchall()[0][0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("gilts_df")

    log.info("gilt_data: inserted %d, skipped %d", inserted, len(gilts_df) - inserted)
    return inserted

def store_curves(con: duckdb.DuckDBPyConnection, curves_df: pd.DataFrame) -> int:
    """Insert BoE yield curve rows; skip rows already stored. All or nothing."""
    missing = {"published_at", "published_at_source"} - set(curves_df.columns)
    if missing:
        raise ValueError(f"curves_df is missing {sorted(missing)}; run add_publication_dates first")
    con.register("curves_df", curves_df)

    con.execute("BEGIN TRANSACTION")
    try:
        inserted = con.execute("""
            INSERT INTO boe_yield_curve (curve_type, curve_date, maturity_years, rate_pct, published_at, published_at_source)
            SELECT curve_type, curve_date, maturity_years, rate, published_at, published_at_source FROM curves_df
            ON CONFLICT (curve_type, curve_date, maturity_years) DO NOTHING
        """).fetchall()[0][0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("curves_df")

    log.info("boe_yield_curve: inserted %d, skipped %d", inserted, len(curves_df) - inserted)
    return inserted

# --- one date: fetch -> parse (validates) -> store --------------------------

def refresh_one(con: duckdb.DuckDBPyConnection, cob_date: date) -> int:
    """Load one COB date. Returns rows inserted (0 if already stored)."""
    gilts = parse_gilts_in_issue(fetch_gilts_in_issue(cob_date))
    if gilts["cob_date"].iloc[0] != cob_date:
        # The DMO returns the previous working day for weekends and holidays.
        log.info("Requested %s, DMO returned COB %s", cob_date, gilts["cob_date"].iloc[0])
    return store_gilts(con, gilts)


# --- dates ------------------------------------------------------------------

def last_final_cob(today: date | None = None) -> date:
    """Latest COB date that is at least 1 working day old, so its data is final.

    Weekends only; UK bank holidays are handled because the DMO returns the
    previous working day, and the store skips duplicates.
    """
    day = (today or date.today()) - timedelta(days=1)
    while day.weekday() >= 5:  # skip Saturday and Sunday
        day -= timedelta(days=1)
    return day


def month_ends(start: date, end: date) -> list[date]:
    """Last calendar day of each month from start to end (inclusive)."""
    dates, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = date(year, month, calendar.monthrange(year, month)[1])
        if start <= last <= end:
            dates.append(last)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return dates


# --- runs -------------------------------------------------------------------

def backfill(con, start: date, end: date, pause_seconds: float = 1.0) -> list[date]:
    """Load month-end dates from start to end. Returns the dates that failed."""
    end = min(end, last_final_cob())
    dates = month_ends(start, end)
    log.info("Backfilling %d month-end dates from %s to %s", len(dates), start, end)

    failed = []
    for i, cob_date in enumerate(dates, 1):
        try:
            refresh_one(con, cob_date)
        except Exception as exc:  # one bad date must not kill the run
            log.error("FAILED %s: %s", cob_date, exc)
            failed.append(cob_date)
        log.info("Progress %d/%d", i, len(dates))
        time.sleep(pause_seconds)  # be polite to the DMO

    log.info("Done: %d succeeded, %d failed %s", len(dates) - len(failed), len(failed), failed)
    return failed


def daily(con) -> list[date]:
    """Load the latest final COB date. For cron."""
    cob_date = last_final_cob()
    try:
        refresh_one(con, cob_date)
        return []
    except Exception as exc:
        log.error("FAILED %s: %s", cob_date, exc)
        return [cob_date]


def load_boe(con, url: str) -> int:
    """Download a BoE yield curve zip, estimate publication dates, and store the curves."""
    curves = add_publication_dates(parse_zip(fetch_zip(url)))
    return store_curves(con, curves)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load DMO gilts and BoE yield curves into DuckDB")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    bf = sub.add_parser("backfill", help="DMO gilts over a range of month-ends")
    bf.add_argument("--start", type=date.fromisoformat, required=True)
    bf.add_argument("--end", type=date.fromisoformat, default=date.today())
    sub.add_parser("daily", help="DMO gilts for the last final COB date")
    boe = sub.add_parser("boe", help="BoE nominal spot and forward curves")
    boe.add_argument("--source", choices=sorted(BOE_URLS), default="archive")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    con = connect(args.db)
    try:
        if args.command == "backfill":
            failed = backfill(con, args.start, args.end)
        elif args.command == "daily":
            failed = daily(con)
        else:
            load_boe(con, BOE_URLS[args.source])
            failed = []
    finally:
        con.close()
    return 1 if failed else 0  # non-zero exit so cron notices


if __name__ == "__main__":
    sys.exit(main())
