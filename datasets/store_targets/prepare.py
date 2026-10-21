"""Build the finance team's monthly store targets workbook (data/store_targets.xlsx) from the retail Oracle data.

    python datasets/store_targets/prepare.py

Target = the store's sales in the same month last year × 1.06, rounded to 10,000; stores without a full year
of history get 60 % of their region's average store. Like a hand-kept workbook it is not perfectly in line
with the database, on purpose (the catalog must find these):

  - 高雄鳳山店 (S025, opened 2026-06) has no targets: nobody added the new store
  - S026 (a planned store that has not opened) already has targets
  - one row has the code typed as "s013 " (lower case, trailing space)
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from simulator.db import connect  # noqa: E402

OUT = HERE / "data" / "store_targets.xlsx"
YEAR = 2026
GROWTH = 1.06


def query(conn, sql: str, **params) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def main() -> None:
    with connect() as conn:
        stores = query(conn, "SELECT STORE_CODE, REGION_NAME, OPEN_DATE FROM DIM_STORE ORDER BY STORE_CODE")
        sales = query(
            conn,
            "SELECT s.STORE_CODE, d.MONTH_NUM, SUM(f.SALES_AMOUNT) AS SALES FROM FACT_SALES f "
            "JOIN DIM_DATE d ON d.DATE_KEY = f.DATE_KEY JOIN DIM_STORE s ON s.STORE_ID = f.STORE_ID "
            "WHERE d.YEAR_NUM = :y GROUP BY s.STORE_CODE, d.MONTH_NUM", y=YEAR - 1)
    last_year = sales.set_index(["STORE_CODE", "MONTH_NUM"])["SALES"]
    region_avg = sales.merge(stores, on="STORE_CODE").groupby(["REGION_NAME", "MONTH_NUM"])["SALES"].mean()
    rows = []
    for s in stores.itertuples():
        if s.STORE_CODE == "S025":
            continue
        for month in range(1, 13):
            if pd.Timestamp(s.OPEN_DATE) > pd.Timestamp(YEAR, month, 1) + pd.offsets.MonthEnd(0):
                continue
            base = last_year.get((s.STORE_CODE, month))
            target = float(base) * GROWTH if base is not None and pd.Timestamp(s.OPEN_DATE).year < YEAR - 1 \
                else float(region_avg[(s.REGION_NAME, month)]) * 0.6
            rows.append({"門市代碼": s.STORE_CODE, "年月": f"{YEAR}-{month:02d}", "目標營收": round(target, -4)})
    for month in range(10, 13):  # a planned store
        rows.append({"門市代碼": "S026", "年月": f"{YEAR}-{month:02d}", "目標營收": 1_200_000.0})
    df = pd.DataFrame(rows)
    typo = df.index[(df["門市代碼"] == "S013") & (df["年月"] == f"{YEAR}-07")]
    df.loc[typo, "門市代碼"] = "s013 "
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(OUT, sheet_name="門市月目標", index=False)
    print(f"written {OUT}: {len(df)} rows, {df['門市代碼'].nunique()} codes")


if __name__ == "__main__":
    main()
