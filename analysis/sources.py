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
from .semantic import dax_col, dax_table, ident

BLANK = "(空白)"  # same label as store.BLANK


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
    if ctx.source["kind"] == "powerbi":
        return PowerBISource(ctx).date_range()
    if ctx.source["kind"] == "excel":
        with ExcelSource(ctx) as src:
            t = ctx.model.tables[ctx.date_dim.table]
            col = ident(ctx.date_dim.source_column)
            lo, hi = src.execute(f"SELECT min({col}), max({col}) FROM {ident(t.source_table)}").fetchone()
        return pd.Timestamp(lo).date(), pd.Timestamp(hi).date()
    from simulator import config as sim_cfg
    return sim_cfg.HISTORY_START, date.today() - timedelta(days=1)


class PowerBISource:
    """The data already imported into an open Power BI Desktop report, read with DAX.

    Works whatever the report's own source is (SQL Server, SharePoint, CSV …): the engine answers the same
    grouped aggregates the SQL path asks the database for. Blank members come back as BLANK → "(空白)"."""

    def __init__(self, ctx: Context):
        from . import pbi
        self.ctx, self.pbi = ctx, pbi
        self.port = pbi.find_port(ctx.source.get("title"))
        self.date = dax_col(ctx.date_dim.table, ctx.date_dim.column)

    def between(self, d0: date, d1: date) -> str:
        return (f"FILTER(ALL({self.date}), {self.date} >= DATE({d0.year},{d0.month},{d0.day}) && "
                f"{self.date} <= DATE({d1.year},{d1.month},{d1.day}))")

    def query(self, columns: list[str], names: list[str], d0: date, d1: date, measures: dict[str, str]) -> pd.DataFrame:
        exprs = "".join(f', "{k}", {v}' for k, v in measures.items())
        q = f"EVALUATE SUMMARIZECOLUMNS({', '.join(columns + [self.between(d0, d1)])}{exprs})"
        (df,) = self.pbi.frames(self.port, [q])
        df.columns = names + list(measures)
        return df

    def grouped(self, dim_keys: list[str], bases: list[str], d0: date, d1: date) -> pd.DataFrame:
        dims = [self.ctx.dims[k] for k in dim_keys]
        df = self.query([self.date] + [dax_col(d.table, d.column) for d in dims], ["business_date"] + dim_keys,
                        d0, d1, {b: self.ctx.bases[b].dax for b in bases})
        for k in dim_keys:
            df[k] = df[k].fillna(BLANK)
        for b in bases:
            df[b] = pd.to_numeric(df[b]).astype(float).fillna(0)  # float: an all-integer first month must not make the column integer
        if not df.empty:
            df["business_date"] = pd.to_datetime(df["business_date"]).dt.date
        return df

    def order_level_dims(self, d1: date) -> list[str]:
        """Dimensions with one value per distinct-count key (e.g. ORDER_ID), checked on the last 14 days."""
        keys = {b.distinct_of for b in self.ctx.bases.values() if b.distinct_of}
        result = set(self.ctx.dims)
        for key in keys:
            col = next(c for c in self.ctx.model.tables[self.ctx.fact].columns.values()
                       if (c.source_column or c.name) == key)
            dims = list(self.ctx.dims)
            df = self.query([dax_col(self.ctx.fact, col.name), self.date] +
                            [dax_col(self.ctx.dims[k].table, self.ctx.dims[k].column) for k in dims],
                            ["key", "business_date"] + dims, d1 - timedelta(days=13), d1,
                            {"n": self.ctx.bases["rows"].dax})
            if df.groupby("key")["business_date"].nunique().max() > 1:
                raise ValueError("distinct-count keys span several dates; cannot add them up over time")
            per_key = df.groupby("key")[dims].nunique(dropna=False).max()
            result &= {k for k in dims if per_key[k] <= 1}
        return [k for k in self.ctx.dims if k in result]

    def calendar(self, d0: date, d1: date) -> pd.DataFrame:
        if self.ctx.holiday_source:
            holiday = next(c.name for c in self.ctx.model.tables[self.ctx.date_dim.table].columns.values()
                           if c.source_column == self.ctx.holiday_source)
            df = self.query([self.date, dax_col(self.ctx.date_dim.table, holiday)], ["business_date", "holiday"],
                            d0, d1, {})
            df["business_date"] = pd.to_datetime(df["business_date"]).dt.date
            return df.drop_duplicates("business_date")
        days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
        return pd.DataFrame({"business_date": days, "holiday": [None] * len(days)})

    def etl(self, d0: date, d1: date) -> pd.DataFrame | None:
        dq = self.ctx.dq
        if not dq.get("etl_table"):
            return None
        t = dq["etl_table"]
        df = self.query([self.date, dax_col(t, dq["etl_rows"]), dax_col(t, dq["etl_status"])],
                        ["business_date", "etl_rows", "etl_status"], d0, d1, {"n": f"COUNTROWS({dax_table(t)})"})
        df = df.drop(columns="n")
        df["etl_rows"] = pd.to_numeric(df["etl_rows"])
        if not df.empty:
            df["business_date"] = pd.to_datetime(df["business_date"]).dt.date
        return df

    def date_range(self) -> tuple[date, date]:
        has = f"NOT ISBLANK(CALCULATE({self.ctx.bases['rows'].dax}))"
        q = (f'EVALUATE ROW("a", FORMAT(MINX(FILTER(ALL({self.date}), {has}), {self.date}), "yyyy-MM-dd"), '
             f'"b", FORMAT(MAXX(FILTER(ALL({self.date}), {has}), {self.date}), "yyyy-MM-dd"))')
        (a, b), = self.pbi.run_dax(self.port, [q])[0]["rows"]
        return date.fromisoformat(a), date.fromisoformat(b)
