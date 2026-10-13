"""Download the Global Superstore workbook and write the copy with injected scenarios.

    python datasets/superstore/prepare.py

  data/GlobalSuperstore_original.xlsx   the public workbook, unchanged (verified by SHA-256)
  data/GlobalSuperstore.xlsx            same workbook + the business events in scenarios.yaml

The original keeps all of its real-world noise; the injected events are the ground truth for scoring.
Only this script and the evaluators read scenarios.yaml — the analysis and the agent see the workbook only.
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
from simulator.scenarios import load  # noqa: E402

DATA = HERE / "data"
ORIGINAL = DATA / "GlobalSuperstore_original.xlsx"
INJECTED = DATA / "GlobalSuperstore.xlsx"
# Global Superstore 2016 (Tableau sample data), as redistributed on GitHub
URL = "https://github.com/karankumar26/Global_Superstore_Dashboard_PowerBI/raw/HEAD/global_superstore_2016.xlsx"
SHA256 = "5ffb10b2b3c34645251b33b91570e153c272639fa2a179c2f8013185e6a0d482"

# scenario filter keys → Excel columns
COLUMNS = {"market": "Market", "region": "Region", "country": "Country", "category": "Category",
           "sub_category": "Sub-Category", "segment": "Segment", "ship_mode": "Ship Mode", "product": "Product Name"}
LINE_LEVEL = {"category", "sub_category", "product"}  # effects act on order lines, otherwise on whole orders
MAX_DISCOUNT = 0.85


def download() -> None:
    if ORIGINAL.exists() and hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == SHA256:
        return
    DATA.mkdir(exist_ok=True)
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as r:
        body = r.read()
    digest = hashlib.sha256(body).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"unexpected file (sha256 {digest}); the source may have changed")
    ORIGINAL.write_bytes(body)


def inject(orders: pd.DataFrame, scenarios, seed: int = 20151231) -> tuple[pd.DataFrame, list[dict]]:
    rng = np.random.default_rng(seed)
    o = orders.copy()
    day = o["Order Date"].dt.date
    log = []
    for s in scenarios:
        rows = pd.Series(True, index=o.index)
        for key, members in s.filter.items():
            rows &= o[COLUMNS[key]].isin(members)
        active_days = {d for d in day[rows].unique() if s.active_on(d)}
        rows &= day.isin(active_days)
        if not rows.any():
            raise SystemExit(f"scenario {s.id}: no rows match")
        eff = {name: day[rows].map(lambda d, n=name: s.at(d).effect(n)) for name in s.effects}
        entry = {"id": s.id, "rows_in_scope": int(rows.sum())}
        if "price" in eff:
            f = eff["price"]
            o.loc[rows, "Profit"] += o.loc[rows, "Sales"] * (f - 1)
            o.loc[rows, "Sales"] *= f
        if "cost" in eff:
            cost = o.loc[rows, "Sales"] - o.loc[rows, "Profit"]
            o.loc[rows, "Profit"] -= cost * (eff["cost"] - 1)
        if "extra_discount" in eff:
            old = o.loc[rows, "Discount"]
            new = np.minimum(old + eff["extra_discount"], MAX_DISCOUNT)
            sales = o.loc[rows, "Sales"] / (1 - old) * (1 - new)
            o.loc[rows, "Profit"] -= o.loc[rows, "Sales"] - sales
            o.loc[rows, "Sales"], o.loc[rows, "Discount"] = sales, new
        # volume / missing data: drop or duplicate whole orders (or order lines for product-level scopes)
        unit = (o.index.to_series() if LINE_LEVEL & s.filter.keys() else o["Order ID"])[rows]
        draw = unit.map(dict(zip(unit.unique(), rng.random(unit.nunique()))))
        keep = pd.Series(1.0, index=draw.index)
        if "volume" in eff:
            keep *= eff["volume"]
        if "missing_rate" in eff:
            keep *= 1 - eff["missing_rate"]
        drop = draw.index[draw >= keep.clip(upper=1)]
        extra = draw.index[(keep > 1) & (draw < keep - 1)]
        if len(extra):
            dup = o.loc[extra].copy()
            dup["Order ID"] = dup["Order ID"] + "-P"
            dup["Row ID"] = np.arange(len(dup)) + o["Row ID"].max() + 1
            o = pd.concat([o, dup])
            day = o["Order Date"].dt.date
        o = o.drop(index=drop)
        day = day.drop(index=drop)
        entry.update(rows_dropped=len(drop), rows_added=len(extra))
        log.append(entry)
        o = o.reset_index(drop=True)
        day = o["Order Date"].dt.date
    return o.sort_values(["Order Date", "Row ID"]).reset_index(drop=True), log


def main() -> None:
    download()
    book = pd.ExcelFile(ORIGINAL)
    sheets = {name: book.parse(name) for name in book.sheet_names}
    scenarios = load(HERE / "scenarios.yaml", set(COLUMNS))
    sheets["Orders"], log = inject(sheets["Orders"], scenarios)
    with pd.ExcelWriter(INJECTED, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False)
    (DATA / "injection_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    for e in log:
        print(f"  {e['id']}: {e['rows_in_scope']:,} rows in scope, -{e['rows_dropped']:,} / +{e['rows_added']:,}")
    print(f"wrote {INJECTED}")


if __name__ == "__main__":
    main()
