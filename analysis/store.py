"""Daily snapshots: aggregate the source data along the model's relationships into a local DuckDB.

Two grains are kept:
  line_snap   date × every dimension           additive bases only (SUM / COUNT(*))
  order_snap  date × order-level dimensions    all bases incl. distinct counts (e.g. orders)
A dimension is "order-level" when every distinct-count key (ORDER_ID) maps to one value of it —
checked against the data, not assumed.
"""
import re
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd

from . import sources
from .context import DATA_DIR, Context
from .semantic import cond_sql, ident

DB_PATH = DATA_DIR / "analysis.duckdb"
BLANK = "(空白)"  # fact rows without a matching dimension row, like Power BI's blank member


def open_db(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def _from_clause(ctx: Context, dims) -> tuple[str, dict[str, str]]:
    """FROM fact LEFT JOIN ... following the relationship paths; returns SQL and table→alias map.
    LEFT JOIN as in the model: fact rows without a dimension row still count in the totals."""
    src = lambda t: ident(ctx.model.tables[t].source_table or t)  # noqa: E731
    aliases = {ctx.fact: "f"}
    sql = f"FROM {src(ctx.fact)} f"
    for d in dims:
        for r in d.path:
            if r.to_table in aliases:
                continue
            alias = f"t{len(aliases)}"
            aliases[r.to_table] = alias
            fc = ident(ctx.model.tables[r.from_table].columns[r.from_column].source_column)
            tc = ident(ctx.model.tables[r.to_table].columns[r.to_column].source_column)
            sql += f"\nLEFT JOIN {src(r.to_table)} {alias} ON {aliases[r.from_table]}.{fc} = {alias}.{tc}"
    return sql, aliases


class _Path:
    """A table the query must join only for a CALCULATE filter (not grouped by)."""
    def __init__(self, path):
        self.path = path


def _grouped_sql(ctx: Context, dim_keys: list[str], bases: list[str]) -> str:
    dims = [ctx.date_dim] + [ctx.dims[k] for k in dim_keys]
    filter_tables = sorted(({t for b in bases for t in ctx.bases[b].tables} | {c[0] for c in ctx.report_filters})
                           - {ctx.fact})
    from_sql, alias = _from_clause(ctx, dims + [_Path(ctx.model.join_path(ctx.fact, t)) for t in filter_tables])
    col = lambda d: f"{alias[d.table]}.{ident(d.source_column)}"  # noqa: E731
    cols = [f'{col(dims[0])} AS "{dims[0].key}"'] + [f"COALESCE({col(d)}, '{BLANK}') AS \"{d.key}\"" for d in dims[1:]]
    # CALCULATE filters on other tables refer to them as @@table@@: put in the join alias
    put_alias = lambda sql: re.sub(r"@@(.+?)@@", lambda m: alias[m.group(1)], sql)  # noqa: E731
    aggs = [put_alias(ctx.bases[b].sql) + f' AS "{b}"' for b in bases]
    # report-level filters: every number in the report is computed under them
    report_where = "".join(f"\n  AND {put_alias(cond_sql(ctx.model, ctx.fact, c))}" for c in ctx.report_filters)
    group = ", ".join(col(d) for d in dims)
    return (f"SELECT {', '.join(cols + aggs)}\n{from_sql}\n"
            f"WHERE {col(ctx.date_dim)} BETWEEN :d0 AND :d1{report_where}\nGROUP BY {group}")


def order_level_dims(ctx: Context, conn, d1: date) -> tuple[list[str], set[str]]:
    """Dimensions that are constant within each distinct-count key (checked on the last 14 days), and the keys
    that span several days (e.g. CUSTOMER_ID): daily distinct counts of those cannot be added over a window."""
    keys = {b.distinct_of for b in ctx.bases.values() if b.distinct_of}
    if not keys:
        return list(ctx.dims), set()
    dims = [ctx.date_dim] + list(ctx.dims.values())
    from_sql, alias = _from_clause(ctx, dims)
    date_col = f"{alias[ctx.date_dim.table]}.{ident(ctx.date_dim.source_column)}"
    checks = ", ".join(f'COUNT(DISTINCT {alias[d.table]}.{ident(d.source_column)}) AS "{d.key}"' for d in dims)
    result, cross_day = set(ctx.dims), set()
    cur = conn.cursor()
    for key in keys:
        cur.execute(f"""SELECT {', '.join(f'MAX("{d.key}")' for d in dims)} FROM (
                            SELECT f.{ident(key)}, {checks} {from_sql}
                            WHERE {date_col} BETWEEN :d0 AND :d1 GROUP BY f.{ident(key)}) x""",
                    d0=d1 - timedelta(days=13), d1=d1)
        row = cur.fetchone()
        if row[0] and row[0] > 1:
            cross_day.add(key)
            continue
        result &= {d.key for d, n in zip(dims[1:], row[1:]) if n == 1}
    return [k for k in ctx.dims if k in result], cross_day


def cross_day_bases(ctx: Context, keys: set[str]) -> list[str]:
    return [b for b, v in ctx.bases.items() if v.distinct_of in keys]


def _fetch(conn, sql: str, d0: date, d1: date) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql, d0=d0, d1=d1)
    df = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    if not df.empty:
        df["business_date"] = pd.to_datetime(df["business_date"]).dt.date
    return df


def _replace(db, table: str, df: pd.DataFrame, d0: date, d1: date) -> None:
    db.register("incoming", df)
    exists = db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                        [table]).fetchone()[0]
    if not exists and df.empty:  # e.g. a month removed by a report filter: create the table from real rows
        db.unregister("incoming")
        return
    if not exists:
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM incoming LIMIT 0")
    else:  # a measure added to the model brings a new aggregate: add the column (older days stay empty)
        have = {r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                                         [table]).fetchall()}
        for c in df.columns:
            if c not in have:
                db.execute(f'ALTER TABLE {table} ADD COLUMN "{c}" DOUBLE')
                print(f"  {table}: new column {c} — re-take the whole history: python -m analysis snapshot --start <first day>")
    db.execute(f"DELETE FROM {table} WHERE business_date BETWEEN ? AND ?", [d0, d1])
    if not df.empty:
        db.execute(f"INSERT INTO {table} BY NAME SELECT * FROM incoming")
    db.unregister("incoming")


def _etl_sql(ctx: Context) -> str | None:
    """Batch log of the load job (rows loaded, status), if the model has one."""
    dq = ctx.dq
    if not dq.get("etl_table"):
        return None
    etl = ctx.model.tables[dq["etl_table"]]
    rel = next(r for r in ctx.model.relationships
               if r.from_table == dq["etl_table"] and r.to_table == ctx.date_dim.table)
    col = lambda t, c: ident(ctx.model.tables[t].columns[c].source_column)  # noqa: E731
    return (f"SELECT d.{ident(ctx.date_dim.source_column)} AS \"business_date\", "
            f"e.{col(dq['etl_table'], dq['etl_rows'])} AS \"etl_rows\", "
            f"e.{col(dq['etl_table'], dq['etl_status'])} AS \"etl_status\" "
            f"FROM {ident(etl.source_table)} e JOIN {ident(ctx.model.tables[ctx.date_dim.table].source_table)} d "
            f"ON e.{col(rel.from_table, rel.from_column)} = d.{col(rel.to_table, rel.to_column)} "
            f"WHERE d.{ident(ctx.date_dim.source_column)} BETWEEN :d0 AND :d1")


def _calendar(ctx: Context, conn, d0: date, d1: date) -> pd.DataFrame:
    """Business dates with their holiday name; a plain calendar when the model has no holiday column."""
    if ctx.holiday_source:
        table = ident(ctx.model.tables[ctx.date_dim.table].source_table)
        col = ident(ctx.date_dim.source_column)
        return _fetch(conn, f'SELECT {col} AS "business_date", {ident(ctx.holiday_source)} AS "holiday" '
                            f"FROM {table} WHERE {col} BETWEEN :d0 AND :d1", d0, d1).drop_duplicates("business_date")
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    return pd.DataFrame({"business_date": days, "holiday": pd.Series([None] * len(days), dtype="object")})


def snapshot(ctx: Context, d0: date, d1: date, verbose: bool = True) -> None:
    if ctx.source["kind"] == "powerbi":
        return snapshot_from_powerbi(ctx, d0, d1, verbose)
    additive = [b for b, v in ctx.bases.items() if not v.distinct_of]
    with sources.connect(ctx) as conn, open_db() as db:
        order_dims, cross_day = order_level_dims(ctx, conn, d1)
        skipped = cross_day_bases(ctx, cross_day)
        line_sql = _grouped_sql(ctx, list(ctx.dims), additive)
        order_sql = _grouped_sql(ctx, order_dims, [b for b in ctx.bases if b not in skipped])
        etl_sql = _etl_sql(ctx)

        start = d0
        while start <= d1:
            end = min(d1, (start.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
            lines = _fetch(conn, line_sql, start, end)
            orders = _fetch(conn, order_sql, start, end)
            _replace(db, "line_snap", lines, start, end)
            _replace(db, "order_snap", orders, start, end)
            if etl_sql:
                _replace(db, "etl_log", _fetch(conn, etl_sql, start, end), start, end)
            cal = _calendar(ctx, conn, start, end)
            cal["holiday"] = cal["holiday"].astype("object")
            _replace(db, "calendar", cal, start, end)
            log = (orders.groupby("business_date")["rows"].sum().rename("fact_rows").reset_index()
                   if not orders.empty else pd.DataFrame(columns=["business_date", "fact_rows"]))
            # a day without any rows is still a business day that was loaded (zero sales)
            days = pd.DataFrame({"business_date": [start + timedelta(days=i) for i in range((end - start).days + 1)]})
            log = days.merge(log, on="business_date", how="left").fillna({"fact_rows": 0})
            log["taken_at"] = datetime.now()
            _replace(db, "snapshot_log", log, start, end)
            if verbose:
                print(f"  snapshot {start} .. {end}: {len(lines):,} line rows, {len(orders):,} order rows",
                      flush=True)
            start = end + timedelta(days=1)
        db.execute("CREATE OR REPLACE TABLE meta AS SELECT ? AS order_dims, ? AS updated_at, ? AS skipped_bases",
                   [",".join(order_dims), datetime.now(), ",".join(skipped)])


def snapshot_from_powerbi(ctx: Context, d0: date, d1: date, verbose: bool = True) -> None:
    """Same snapshots, but the aggregates come from the open Power BI report (DAX) instead of the source DB."""
    src = sources.PowerBISource(ctx)
    additive = [b for b, v in ctx.bases.items() if not v.distinct_of]
    with open_db() as db:
        order_dims, cross_day = src.order_level_dims(d1)
        skipped = cross_day_bases(ctx, cross_day)
        start = d0
        while start <= d1:
            end = min(d1, (start.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
            lines = src.grouped(list(ctx.dims), additive, start, end)
            orders = src.grouped(order_dims, [b for b in ctx.bases if b not in skipped], start, end)
            _replace(db, "line_snap", lines, start, end)
            _replace(db, "order_snap", orders, start, end)
            etl = src.etl(start, end)
            if etl is not None:
                _replace(db, "etl_log", etl, start, end)
            cal = src.calendar(start, end)
            cal["holiday"] = cal["holiday"].astype("object")
            _replace(db, "calendar", cal, start, end)
            log = (orders.groupby("business_date")["rows"].sum().rename("fact_rows").reset_index()
                   if not orders.empty else pd.DataFrame(columns=["business_date", "fact_rows"]))
            days = pd.DataFrame({"business_date": [start + timedelta(days=i) for i in range((end - start).days + 1)]})
            log = days.merge(log, on="business_date", how="left").fillna({"fact_rows": 0})
            log["taken_at"] = datetime.now()
            _replace(db, "snapshot_log", log, start, end)
            if verbose:
                print(f"  snapshot {start} .. {end} (Power BI): {len(lines):,} line rows, {len(orders):,} order rows",
                      flush=True)
            start = end + timedelta(days=1)
        db.execute("CREATE OR REPLACE TABLE meta AS SELECT ? AS order_dims, ? AS updated_at, ? AS skipped_bases",
                   [",".join(order_dims), datetime.now(), ",".join(skipped)])


def last_snapshot_date(db) -> date | None:
    try:
        return db.execute("SELECT max(business_date) FROM snapshot_log").fetchone()[0]
    except duckdb.CatalogException:
        return None


def order_dims(db) -> list[str]:
    return db.execute("SELECT order_dims FROM meta").fetchone()[0].split(",")


def skipped_bases(db) -> set[str]:
    """Aggregates not in the snapshots: distinct counts of keys that span several days."""
    try:
        value = db.execute("SELECT skipped_bases FROM meta").fetchone()[0]
    except duckdb.BinderException:  # snapshots taken before this column existed
        return set()
    return set(value.split(",")) if value else set()
