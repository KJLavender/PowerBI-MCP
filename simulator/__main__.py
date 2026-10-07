"""Simulated Oracle source system for Power BI.

  python -m simulator init [--reset]          create schema and load dimensions
  python -m simulator backfill --start --end  (re)generate sales for a date range
  python -m simulator daily [--date]          load one business day (default: yesterday)
  python -m simulator status                  show row counts and recent ETL batches
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as cfg
from . import dimensions, scenarios
from .db import connect, run_script
from .facts import Generator

TABLES = ["FACT_SALES", "ETL_BATCH_LOG", "DIM_CUSTOMER", "DIM_PRODUCT", "DIM_CHANNEL", "DIM_STORE", "DIM_DATE"]
INSERT_FACT = "INSERT INTO FACT_SALES VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9,:10,:11,:12)"


def cmd_init(args) -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM user_tables")
        existing = {r[0] for r in cur}
        if existing & set(TABLES):
            if not args.reset:
                sys.exit("Tables already exist. Use --reset to drop and recreate them.")
            for t in TABLES:
                if t in existing:
                    cur.execute(f"DROP TABLE {t} CASCADE CONSTRAINTS PURGE")
        run_script(cur, Path(__file__).with_name("schema.sql"))
        dimensions.load(cur)
        conn.commit()
    print("Schema created and dimensions loaded.")


def load_day(conn, gen: Generator, d: date, scen: list) -> int:
    result = gen.generate(d, scenarios.active(scen, d))
    date_key = int(d.strftime("%Y%m%d"))
    cur = conn.cursor()
    cur.execute("DELETE FROM FACT_SALES WHERE DATE_KEY = :1", [date_key])
    for i in range(0, len(result.rows), 20000):
        cur.executemany(INSERT_FACT, result.rows[i:i + 20000])
    cur.execute("DELETE FROM ETL_BATCH_LOG WHERE BUSINESS_DATE_KEY = :1", [date_key])
    cur.execute("INSERT INTO ETL_BATCH_LOG VALUES (:1, :2, :3, :4, :5)",
                [date_key, datetime.now(), len(result.rows), result.etl_status, result.etl_message])
    conn.commit()
    return len(result.rows)


def cmd_backfill(args) -> None:
    start, end = args.start, args.end
    gen, scen = Generator(), scenarios.load()
    t0, total = time.time(), 0
    with connect() as conn:
        d = start
        while d <= end:
            total += load_day(conn, gen, d, scen)
            if d.day == 1 or d == end:
                print(f"  {d}  cumulative rows: {total:,}  ({time.time() - t0:.0f}s)", flush=True)
            d += timedelta(days=1)
    print(f"Backfilled {start} .. {end}: {total:,} rows in {time.time() - t0:.0f}s")


def cmd_daily(args) -> None:
    """Load one date, or (default) every missing date from the last batch up to yesterday."""
    gen, scen = Generator(), scenarios.load()
    with connect() as conn:
        if args.date:
            days = [args.date]
        else:
            cur = conn.cursor()
            cur.execute("SELECT MAX(BUSINESS_DATE_KEY) FROM ETL_BATCH_LOG")
            last = cur.fetchone()[0]
            d = datetime.strptime(str(last), "%Y%m%d").date() + timedelta(days=1) if last else cfg.HISTORY_START
            days = []
            while d <= date.today() - timedelta(days=1):
                days.append(d)
                d += timedelta(days=1)
        if not days:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} already up to date")
        for d in days:
            n = load_day(conn, gen, d, scen)
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} loaded business date {d}: {n:,} rows")


def cmd_status(_args) -> None:
    with connect() as conn:
        cur = conn.cursor()
        for t in reversed(TABLES):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(f"{t:<15}{cur.fetchone()[0]:>12,}")
        cur.execute("""SELECT * FROM (SELECT BUSINESS_DATE_KEY, ROWS_LOADED, STATUS, RUN_AT
                       FROM ETL_BATCH_LOG ORDER BY BUSINESS_DATE_KEY DESC) WHERE ROWNUM <= 7""")
        print("\nRecent ETL batches:")
        for key, rows, status, run_at in cur:
            print(f"  {key}  {rows:>7,} rows  {status:<8} run at {run_at:%Y-%m-%d %H:%M}")


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    p = argparse.ArgumentParser(prog="simulator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--reset", action="store_true", help="drop existing tables first")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("backfill")
    s.add_argument("--start", type=parse_date, default=cfg.HISTORY_START)
    s.add_argument("--end", type=parse_date, default=date.today() - timedelta(days=1))
    s.set_defaults(func=cmd_backfill)
    s = sub.add_parser("daily")
    s.add_argument("--date", type=parse_date)
    s.set_defaults(func=cmd_daily)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
