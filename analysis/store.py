"""Daily snapshots: aggregate Oracle data along the model's relationships into a local DuckDB.

Two grains are kept:
  line_snap   date × every dimension           additive bases only (SUM / COUNT(*))
  order_snap  date × order-level dimensions    all bases incl. distinct counts (e.g. orders)
A dimension is "order-level" when every distinct-count key (ORDER_ID) maps to one value of it —
checked against the data, not assumed.
"""
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd

from simulator.db import connect

from .context import DATA_DIR, Context

DB_PATH = DATA_DIR / "analysis.duckdb"


def open_db(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DATA_DIR.mkdir(exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def _from_clause(ctx: Context, dims) -> tuple[str, dict[str, str]]:
    """FROM fact JOIN ... following the relationship paths; returns SQL and table→alias map."""
    src = lambda t: ctx.model.tables[t].source_table or t  # noqa: E731
    aliases = {ctx.fact: "f"}
    sql = f"FROM {src(ctx.fact)} f"
    for d in dims:
        for r in d.path:
            if r.to_table in aliases:
                continue
            alias = f"t{len(aliases)}"
            aliases[r.to_table] = alias
            fc = ctx.model.tables[r.from_table].columns[r.from_column].source_column
            tc = ctx.model.tables[r.to_table].columns[r.to_column].source_column
            sql += f"\nJOIN {src(r.to_table)} {alias} ON {aliases[r.from_table]}.{fc} = {alias}.{tc}"
    return sql, aliases


def _grouped_sql(ctx: Context, dim_keys: list[str], bases: list[str]) -> str:
    dims = [ctx.date_dim] + [ctx.dims[k] for k in dim_keys]
    from_sql, alias = _from_clause(ctx, dims)
    cols = [f"{alias[d.table]}.{d.source_column} AS \"{d.key}\"" for d in dims]
    aggs = [f'{ctx.bases[b].sql} AS "{b}"' for b in bases]
    group = ", ".join(f"{alias[d.table]}.{d.source_column}" for d in dims)
    date_col = f"{alias[ctx.date_dim.table]}.{ctx.date_dim.source_column}"
    return (f"SELECT {', '.join(cols + aggs)}\n{from_sql}\n"
            f"WHERE {date_col} BETWEEN :d0 AND :d1\nGROUP BY {group}")


def order_level_dims(ctx: Context, conn, d1: date) -> list[str]:
    """Dimensions that are constant within each distinct-count key (checked on the last 14 days)."""
    keys = {b.distinct_of for b in ctx.bases.values() if b.distinct_of}
    if not keys:
        return list(ctx.dims)
    dims = [ctx.date_dim] + list(ctx.dims.values())
    from_sql, alias = _from_clause(ctx, dims)
    date_col = f"{alias[ctx.date_dim.table]}.{ctx.date_dim.source_column}"
    checks = ", ".join(f"COUNT(DISTINCT {alias[d.table]}.{d.source_column}) AS {d.key}" for d in dims)
    result = set(ctx.dims) | {"business_date"}
    cur = conn.cursor()
    for key in keys:
        cur.execute(f"""SELECT {', '.join(f'MAX({d.key})' for d in dims)} FROM (
                            SELECT f.{key}, {checks} {from_sql}
                            WHERE {date_col} BETWEEN :d0 AND :d1 GROUP BY f.{key})""",
                    d0=d1 - timedelta(days=13), d1=d1)
        row = cur.fetchone()
        result &= {d.key for d, n in zip(dims, row) if n == 1}
    if "business_date" not in result:
        raise ValueError("distinct-count keys span several dates; cannot add them up over time")
    return [k for k in ctx.dims if k in result]


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
    if not exists:
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM incoming LIMIT 0")
    db.execute(f"DELETE FROM {table} WHERE business_date BETWEEN ? AND ?", [d0, d1])
    if not df.empty:
        db.execute(f"INSERT INTO {table} BY NAME SELECT * FROM incoming")
    db.unregister("incoming")


def snapshot(ctx: Context, d0: date, d1: date, verbose: bool = True) -> None:
    additive = [b for b, v in ctx.bases.items() if not v.distinct_of]
    with connect() as conn, open_db() as db:
        order_dims = order_level_dims(ctx, conn, d1)
        line_sql = _grouped_sql(ctx, list(ctx.dims), additive)
        order_sql = _grouped_sql(ctx, order_dims, list(ctx.bases))
        dq = ctx.config["data_quality"]
        etl = ctx.model.tables[dq["etl_table"]]
        rel = next(r for r in ctx.model.relationships
                   if r.from_table == dq["etl_table"] and r.to_table == ctx.date_dim.table)
        col = lambda t, c: ctx.model.tables[t].columns[c].source_column  # noqa: E731
        etl_sql = (f"SELECT d.{ctx.date_dim.source_column} AS \"business_date\", "
                   f"e.{col(dq['etl_table'], dq['etl_rows'])} AS \"etl_rows\", "
                   f"e.{col(dq['etl_table'], dq['etl_status'])} AS \"etl_status\" "
                   f"FROM {etl.source_table} e JOIN {ctx.model.tables[ctx.date_dim.table].source_table} d "
                   f"ON e.{col(rel.from_table, rel.from_column)} = d.{col(rel.to_table, rel.to_column)} "
                   f"WHERE d.{ctx.date_dim.source_column} BETWEEN :d0 AND :d1")
        cal_sql = (f"SELECT {ctx.date_dim.source_column} AS \"business_date\", {ctx.holiday_source} AS \"holiday\" "
                   f"FROM {ctx.model.tables[ctx.date_dim.table].source_table} "
                   f"WHERE {ctx.date_dim.source_column} BETWEEN :d0 AND :d1")

        start = d0
        while start <= d1:
            end = min(d1, (start.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
            lines = _fetch(conn, line_sql, start, end)
            orders = _fetch(conn, order_sql, start, end)
            _replace(db, "line_snap", lines, start, end)
            _replace(db, "order_snap", orders, start, end)
            _replace(db, "etl_log", _fetch(conn, etl_sql, start, end), start, end)
            _replace(db, "calendar", _fetch(conn, cal_sql, start, end), start, end)
            log = (orders.groupby("business_date")["rows"].sum().rename("fact_rows").reset_index()
                   if not orders.empty else pd.DataFrame(columns=["business_date", "fact_rows"]))
            log["taken_at"] = datetime.now()
            _replace(db, "snapshot_log", log, start, end)
            if verbose:
                print(f"  snapshot {start} .. {end}: {len(lines):,} line rows, {len(orders):,} order rows",
                      flush=True)
            start = end + timedelta(days=1)
        db.execute("CREATE OR REPLACE TABLE meta AS SELECT ? AS order_dims, ? AS updated_at",
                   [",".join(order_dims), datetime.now()])


def last_snapshot_date(db) -> date | None:
    try:
        return db.execute("SELECT max(business_date) FROM snapshot_log").fetchone()[0]
    except duckdb.CatalogException:
        return None


def order_dims(db) -> list[str]:
    return db.execute("SELECT order_dims FROM meta").fetchone()[0].split(",")
