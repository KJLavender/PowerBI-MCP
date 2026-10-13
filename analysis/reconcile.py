"""Reconcile the analysis engine with Power BI itself.

    python -m analysis reconcile [--port P] [--samples 6]

Power BI Desktop runs an Analysis Services engine in the background while a report is open. This asks that
engine — with DAX, through scripts/pbi-dax.ps1 on localhost — for the same numbers the analysis engine
computes from its own snapshots (sampled dates × 1/7/28-day windows × total, members of every dimension and
the configured combinations × every translated measure), and lists every difference.

It checks the part an LLM cannot: that the DAX → formula translation, the relationship joins and the
snapshots reproduce the report's numbers. Open the dataset's report in Power BI Desktop (and refresh it)
before running; dates after Power BI's last refresh are skipped.
"""
import json
import random
import subprocess
from datetime import date, timedelta

import numpy as np

from . import store
from .context import Context, load
from .dataset import REPORT_DIR, ROOT
from .engine import Engine, evaluate

SCRIPT = ROOT / "scripts" / "pbi-dax.ps1"


def ps(*args: str, stdin: str = "") -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
                         input=stdin.encode("utf-8"), capture_output=True, timeout=600)
    if out.returncode:
        raise RuntimeError(out.stderr.decode("utf-8", "replace"))
    return out.stdout.decode("utf-8-sig")


def engines() -> list[dict]:
    return json.loads(ps("-List") or "[]")


def run_dax(port: int, queries: list[str]) -> list[dict]:
    # ASCII-only JSON: Windows PowerShell reads stdin in the console code page
    res = json.loads(ps("-Port", str(port), stdin=json.dumps(queries)))
    return res if isinstance(res, list) else [res]


def dax_name(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def col(table: str, column: str) -> str:
    return f"{dax_name(table)}[{column}]"


def run(ctx: Context, port: int, samples: int = 6, seed: int = 11) -> dict:
    rng = random.Random(seed)
    date_col = col(ctx.date_dim.table, ctx.date_dim.column)
    metrics = [m for m in ctx.metrics]
    # the last day with data in the report (a date table usually runs past it)
    last_q = f"MAXX(FILTER(ALL({date_col}), NOT ISBLANK([{metrics[0]}])), {date_col})"
    (last_pbi,) = run_dax(port, [f"EVALUATE ROW(\"d\", FORMAT({last_q}, \"yyyy-MM-dd\"))"])[0]["rows"][0]
    last_pbi = date.fromisoformat(last_pbi)
    with store.open_db(read_only=True) as db:
        first, last = db.execute("SELECT min(business_date), max(business_date) FROM snapshot_log").fetchone()
        engine = Engine(ctx, db)
        end = min(last, last_pbi) - timedelta(days=3)  # recent days may still be corrected by the source
        start = max(first + timedelta(days=60), end - timedelta(days=400))
        days = sorted(rng.sample(range((end - start).days + 1), samples))
        groupings = [()] + [(k,) for k in ctx.dims] + [tuple(c) for c in ctx.config.get("combinations", [])][:2]
        checks, queries = [], []
        for offset in days:
            d = start + timedelta(days=offset)
            engine.prepare(d)
            for span in (1, 7, 28):
                d0 = d - timedelta(days=span - 1)
                for dims in groupings:
                    cube = engine.cube(dims)
                    usable = [m for m in metrics if engine.allowed(m, dims)]
                    if not usable:
                        continue
                    # the engine's numbers: sum the daily bases over the window, then apply the formula
                    members = list(range(len(cube.members)))
                    if dims:
                        weight = cube.data["rows"][-span:].sum(axis=0)
                        members = [i for i in np.argsort(-weight)[:3] if weight[i] > 0]
                    for i in members:
                        bases = {b: a[-span:, i].sum(keepdims=True)[None, :] for b, a in cube.data.items()}
                        values = {m: float(evaluate(ctx.formulas[m].expr, bases)[0, 0]) for m in usable}
                        checks.append({"date": d, "days": span, "scope": dict(zip(dims, cube.members[i])),
                                       "engine": values, "query": len(queries)})
                    group = ", ".join(col(ctx.dims[k].table, ctx.dims[k].column) for k in dims)
                    filt = f"FILTER(ALL({date_col}), {date_col} >= DATE({d0.year},{d0.month},{d0.day}) && " \
                           f"{date_col} <= DATE({d.year},{d.month},{d.day}))"
                    cols = ", ".join(f'"{m}", [{m}]' for m in usable)
                    queries.append((dims, usable, f"EVALUATE SUMMARIZECOLUMNS({group + ', ' if group else ''}{filt}, {cols})"))
    answers = run_dax(port, [q for _, _, q in queries])
    compared, diffs, errors = 0, [], []
    for c in checks:
        dims, usable, q = queries[c["query"]]
        res = answers[c["query"]]
        if "error" in res:
            errors.append(f"{q[:120]} → {res['error'][:200]}")
            continue
        want = tuple(c["scope"][k] for k in dims)
        row = next((r for r in res["rows"] if tuple(r[:len(dims)]) == want), None)
        for j, m in enumerate(usable):
            pbi = None if row is None or row[len(dims) + j] is None else float(row[len(dims) + j])
            ours = c["engine"][m]
            compared += 1
            same = (pbi is None and (ours == 0 or ours != ours)) or \
                   (pbi is not None and abs(ours - pbi) <= 1e-6 * max(1.0, abs(pbi)))
            if not same:
                diffs.append({"date": str(c["date"]), "days": c["days"], "scope": c["scope"], "metric": m,
                              "engine": ours, "power_bi": pbi})
    return {"compared": compared, "diffs": diffs, "errors": errors, "queries": len(queries),
            "dates": [str(start + timedelta(days=o)) for o in days], "power_bi_last": str(last_pbi)}


def render(ctx: Context, r: dict, title: str) -> str:
    lines = [f"# 與 Power BI 對帳（{title}）", "",
             f"- 由 Power BI Desktop 的本機引擎以 DAX 計算，與分析引擎的結果逐一比對。",
             f"- 抽樣日期：{'、'.join(r['dates'])}；時間窗：1 / 7 / 28 日；範圍：整體、各維度前 3 大成員、兩組交叉組合。",
             f"- 比對 {r['compared']:,} 個數字（{r['queries']} 個 DAX 查詢），**不一致 {len(r['diffs'])} 個**，"
             f"查詢錯誤 {len(r['errors'])} 個。", ""]
    if r["diffs"]:
        lines += ["| 日期 | 天數 | 範圍 | 指標 | 分析引擎 | Power BI |", "|---|---|---|---|---|---|"]
        for d in r["diffs"][:30]:
            scope = " × ".join(d["scope"].values()) or "整體"
            lines.append(f"| {d['date']} | {d['days']} | {scope} | {d['metric']} | {d['engine']:.6g} | {d['power_bi']} |")
    for e in r["errors"][:10]:
        lines.append(f"- 錯誤：{e}")
    if ctx.skipped:
        lines += ["", "無法翻譯、因此沒有分析也沒有對帳的量值：" + "、".join(ctx.skipped)]
    return "\n".join(lines) + "\n"


def main(port: int | None, samples: int) -> None:
    ctx = load()
    found = engines()
    if port is None:
        if not found:
            raise SystemExit("找不到執行中的 Power BI Desktop：請先開啟這份報表並重新整理")
        port = found[0]["port"]
    title = next((e["title"] for e in found if e["port"] == port), f"port {port}")
    r = run(ctx, port, samples)
    text = render(ctx, r, title)
    out = REPORT_DIR / "reconcile.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {out}")
