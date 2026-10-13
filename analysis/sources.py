"""Read the report's source data the way the Power BI model does (from the M partitions).

  Oracle.Database(...)                 → the Oracle connection of the simulator (.env)
  Excel.Workbook(File.Contents(path))  → the workbook's sheets, loaded into an in-memory DuckDB

Both give a DB-API style connection that accepts the same SQL (named parameters :d0 / :d1),
so snapshots are taken with one code path whatever the source.
"""
import re
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from . import dataset
from .context import Context
from .semantic import ident


def excel_path(ref: str) -> Path:
    """The path in the model is absolute on the machine that built it; fall back to the dataset folder."""
    p = Path(ref)
    if p.exists():
        return p
    local = dataset.HOME / "data" / Path(ref.replace("\\", "/")).name
    if local.exists():
        return local
    raise FileNotFoundError(f"Excel source {ref} not found (also looked for {local})")


class ExcelSource:
    """Excel sheets as DuckDB tables named like the M navigation step (the sheet name)."""

    def __init__(self, ctx: Context):
        self.db = duckdb.connect()
        books: dict[Path, pd.ExcelFile] = {}
        for t in ctx.model.tables.values():
            if t.source.get("kind") != "excel" or not t.source_table:
                continue
            path = excel_path(t.source["file"])
            if dataset.VARIANT:
                path = path.with_name(ctx.config["variants"][dataset.VARIANT])
            book = books.setdefault(path, pd.ExcelFile(path))
            df = book.parse(t.source_table)  # first row = headers (Table.PromoteHeaders)
            self.db.register(f"_{t.name}", df)
            self.db.execute(f"CREATE TABLE {ident(t.source_table)} AS SELECT * FROM {ident('_' + t.name)}")
        self._res = None

    # --- minimal DB-API surface used by store.py ---
    def cursor(self):
        return self

    def execute(self, sql: str, **params):
        self._res = self.db.execute(re.sub(r"(?<!:):(\w+)", r"$\1", sql), params)
        return self

    def fetchall(self):
        return self._res.fetchall()

    def fetchone(self):
        return self._res.fetchone()

    @property
    def description(self):
        return self._res.description

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()


def connect(ctx: Context):
    if ctx.source["kind"] == "excel":
        return ExcelSource(ctx)
    from simulator.db import connect as oracle_connect
    return oracle_connect()


def date_range(ctx: Context) -> tuple[date, date]:
    """First and last business date available in the source."""
    if ctx.source["kind"] == "excel":
        with ExcelSource(ctx) as src:
            t = ctx.model.tables[ctx.date_dim.table]
            col = ident(ctx.date_dim.source_column)
            lo, hi = src.execute(f"SELECT min({col}), max({col}) FROM {ident(t.source_table)}").fetchone()
        return pd.Timestamp(lo).date(), pd.Timestamp(hi).date()
    from simulator import config as sim_cfg
    return sim_cfg.HISTORY_START, date.today() - timedelta(days=1)
