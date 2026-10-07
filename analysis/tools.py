"""Read-only analysis tools shared by the MCP server and tests.

Every function returns plain JSON-serialisable dicts. Numbers come with pre-formatted text
("display") so an LLM can quote them verbatim and a checker can verify the quote.
Each call opens a short-lived read-only DuckDB connection, so the daily snapshot job can still write.
"""
import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from functools import lru_cache

import numpy as np

from . import store
from .context import DATA_DIR, Context, load
from .engine import BROAD, WEEKDAY_LAGS, YEAR, Engine, Finding, Stat, evaluate, windowed

WINDOW_HELP = {
    "day": "單日：當日 vs 近 4 週同星期平均",
    "week": "近 7 日 vs 前 7 日（已扣除去年同期的季節性）",
    "month": "近 28 日 vs 前 28 日（已扣除去年同期的季節性）",
    "year": "近 28 日 vs 去年同期（相對整體同店成長）",
}


class ToolError(ValueError):
    """Invalid input; the message tells the caller how to fix it."""


@lru_cache(maxsize=1)
def ctx() -> Context:
    return load()


@contextmanager
def engine_at(d: date | None):
    db = store.open_db(read_only=True)
    try:
        e = Engine(ctx(), db)
        last = store.last_snapshot_date(db)
        d = d or last
        if d is None:
            raise ToolError("尚未有任何快照資料，請先執行 python -m analysis snapshot")
        first = db.execute("SELECT min(business_date) FROM snapshot_log").fetchone()[0]
        if not (first <= d <= last):
            raise ToolError(f"日期 {d} 超出資料範圍 {first} ～ {last}")
        e.prepare(d)
        yield e, d
    finally:
        db.close()


# ---------------------------------------------------------------------------------- helpers
def pct(kind: str, v: float | None) -> str:
    if v is None or v != v:
        return "—"
    return f"{v * 100:+.1f} 個百分點" if kind == "abs" else f"{v:+.1%}"


MONTH_ONLY = re.compile(r"^\s*(\d{4})\s*[-/年.]\s*(\d{1,2})\s*月?\s*$")
FULL_DATE = re.compile(r"^\s*(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?\s*$")


def parse_period(s: str | None) -> tuple[date | None, bool]:
    """'2026-06-30' / '2026/6/30' / '2026年6月30日' -> (date, False);
    '2026-06' / '2026/6' / '2026年6月' -> (last day of that month, True)."""
    if not s:
        return None, False
    if m := FULL_DATE.match(s):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), False
        except ValueError:
            pass
    elif m := MONTH_ONLY.match(s):
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12:
            nxt = date(y + (mo == 12), mo % 12 + 1, 1)
            return nxt - timedelta(days=1), True
    raise ToolError(f"看不懂日期 {s!r}：請用 YYYY-MM-DD（某一天）或 YYYY-MM（整個月）")


def parse_date(s: str | None) -> date | None:
    return parse_period(s)[0]


def clamp_to_data(d: date | None) -> date | None:
    """A month that is still running ends at the latest loaded day."""
    if d is None:
        return None
    with store.open_db(read_only=True) as db:
        last = store.last_snapshot_date(db)
    return min(d, last) if last else d


def resolve_period(date_str: str | None, window: str) -> tuple[date | None, str, str | None]:
    """Date + window from user input; a month means 'the 28 days ending that month'."""
    d, is_month = parse_period(date_str)
    if not is_month:
        return d, window, None
    d = clamp_to_data(d)
    w = window if window in ("month", "year") else "month"
    return d, w, f"「{date_str}」解讀為整個月：使用截至 {d} 的近 28 日（window={w}）"


@lru_cache(maxsize=1)
def member_index() -> dict[str, str]:
    """member name → dimension label, for spotting members typed into the metric name."""
    out = {}
    with store.open_db(read_only=True) as db:
        for k, d in ctx().dims.items():
            table = "order_snap" if k in store.order_dims(db) else "line_snap"
            for (m,) in db.execute(f"SELECT DISTINCT {k} FROM {table}").fetchall():
                out[m] = d.label
    return out


def metric_name(name: str) -> str:
    c = ctx()
    if name in c.formulas:
        return name
    close = [m for m in c.formulas if m in name or name in m]
    msg = f"沒有指標「{name}」。可用指標：{'、'.join(c.formulas)}"
    members = [(m, dim) for m, dim in member_index().items() if m in name]
    if members:
        m, dim = max(members, key=lambda x: len(x[0]))
        metric = max(close, key=len) if close else "銷售額"
        msg += (f"。注意：「{m}」是維度「{dim}」的成員，不是指標；"
                f"請改用 metric=\"{metric}\"、filters={{\"{dim}\": \"{m}\"}}")
    elif close:
        msg += f"；你是不是指：{'、'.join(close)}"
    raise ToolError(msg)


def window_name(w: str) -> str:
    if w not in ctx().config["windows"]:
        raise ToolError(f"window 需為 {'、'.join(ctx().config['windows'])} 其中之一（{WINDOW_HELP}）")
    return w


def resolve_scope(e: Engine, filters: dict | None) -> dict[str, str]:
    """{"區域": "北區"} or {"region": "北區"} → {"region": "北區"}, members validated against the data."""
    c = ctx()
    if isinstance(filters, str):  # some models send the object as a JSON string
        try:
            filters = json.loads(filters) if filters.strip() else {}
        except json.JSONDecodeError:
            raise ToolError('filters 需為物件，例如 {"區域": "北區"}') from None
    by_label = {d.label: k for k, d in c.dims.items()}
    scope = {}
    for k, v in (filters or {}).items():
        dim = k if k in c.dims else by_label.get(k)
        if dim is None:
            raise ToolError(f"沒有維度「{k}」。可用維度：" + "、".join(f"{d.label}({d.key})" for d in c.dims.values()))
        members = [m[0] for m in e.cube((dim,)).members]
        if v not in members:
            close = [m for m in members if v in m or m in v]
            raise ToolError(f"{c.dims[dim].label}沒有成員「{v}」" +
                            (f"；你是不是指：{'、'.join(close)}" if close else f"。可用成員：{'、'.join(members[:30])}"))
        scope[dim] = v
    return scope


def scope_bases(e: Engine, scope: dict) -> dict:
    bases = e.bases_of(scope) if scope else e.cube(()).column(())
    if bases is None:
        raise ToolError(f"「{e.label(scope)}」在資料中沒有任何交易")
    return bases


def period_text(e: Engine, d: date, window: str) -> dict:
    w = e.windows[window]
    days, mode = w["days"], w.get("mode", "pop")
    cur = f"{d - timedelta(days=days - 1)} ～ {d}" if days > 1 else str(d)
    if window == "day":
        base = "、".join(str(d - timedelta(days=k)) for k in WEEKDAY_LAGS) + " 的平均"
    else:
        lag = YEAR if mode == "yoy" else days
        base = f"{d - timedelta(days=lag + days - 1)} ～ {d - timedelta(days=lag)}"
    return {"本期": cur, "比較基準": base, "說明": WINDOW_HELP.get(window, w["label"])}


def reading(e: Engine, s: Stat, scope: dict) -> str:
    """Three-level reading instead of a bare yes/no."""
    th = e.z_threshold(scope)
    if s.significant:
        return "顯著異常"
    if abs(s.z) >= 2:
        return f"變化偏大，但未達顯著門檻（z 需 ≥ {th}），宜往下一層查看是哪個部分顯著"
    return "在正常波動範圍內"


def stat_dict(e: Engine, s: Stat, member: bool = True) -> dict:
    c = ctx()
    exp_label = "整體同店同期變化" if e.windows[s.window].get("mode") == "yoy" else "去年同期間的變化"
    out = {"指標": s.metric, "本期": c.fmt(s.metric, s.current), "比較基準": c.fmt(s.metric, s.baseline),
           "變化": pct(s.kind, s.change), "z": round(s.z, 1), "顯著": bool(s.significant)}
    if s.expected is not None:
        out[exp_label] = pct(s.kind, s.expected)
    out["display"] = (f"{s.metric} {pct(s.kind, s.change)}（本期 {out['本期']}，比較基準 {out['比較基準']}"
                      + (f"，{exp_label} {out[exp_label]}" if s.expected is not None else "") + "）")
    return out


def finding_dict(e: Engine, f: Finding) -> dict:
    out = {"範圍": e.label(f.scope), "維度": {ctx().dims[k].label: v for k, v in f.scope.items()},
           "時間窗": e.windows[f.window]["label"], "新發現": f.new,
           "顯著指標": [stat_dict(e, s)["display"] for s in f.significant]}
    if f.since:
        out["持續自"] = str(f.since)
    if f.explains:
        out["也解釋了"] = list(dict.fromkeys(f.explains))
    if f.concentrated_in:
        c = f.concentrated_in
        out["變化集中在"] = (f"{ctx().dims[c['dimension']].label}「{c['member']}」：該部分 {c['metric']} "
                        f"{pct(c['kind'], c['inside_change'])}，其餘僅 {pct(c['kind'], c['rest_change'])}")
    if f.evidence.get("pattern"):
        out["型態"] = f.evidence["pattern"]
    if f.notes:
        out["注意"] = f.notes
    return out


# ---------------------------------------------------------------------------------- tools
def describe_model() -> dict:
    """What the Power BI model contains: metrics, dimensions, relationships, data range."""
    c = ctx()
    with store.open_db(read_only=True) as db:
        first, last = db.execute("SELECT min(business_date), max(business_date) FROM snapshot_log").fetchone()
    e_metrics = {}
    for m, f in c.formulas.items():
        meas = c.model.measures[m]
        e_metrics[m] = {"說明": meas.description, "DAX": meas.expression,
                        "類型": "比率" if f.is_ratio else "可加總",
                        "報表上有顯示": m in c.model.used_measures()}
        drivers = c.drivers(m)
        if drivers:
            e_metrics[m]["拆解關係"] = [f"{m} = {y} × {r}" for y, r in drivers]
    return {
        "資料期間": f"{first} ～ {last}",
        "指標": e_metrics,
        "維度": {d.label: {"key": k, "欄位": f"{d.table}[{d.column}]", "上層": c.dims[d.parent].label if d.parent else None,
                         "關聯": [str(r) for r in d.path]} for k, d in c.dims.items()},
        "時間窗": WINDOW_HELP,
    }


def list_members(dimension: str, contains: str | None = None) -> dict:
    with engine_at(None) as (e, _):
        dims = ctx().dims
        dim = dimension if dimension in dims else {d.label: k for k, d in dims.items()}.get(dimension)
        if dim is None:
            raise ToolError(f"沒有維度「{dimension}」，可用：{'、'.join(d.label for d in ctx().dims.values())}")
        cube = e.cube((dim,))
        rows = cube.data["rows"][-28:].sum(axis=0)
        members = sorted(zip([m[0] for m in cube.members], rows), key=lambda x: -x[1])
        if contains:
            members = [m for m in members if contains in m[0]]
        return {"維度": ctx().dims[dim].label, "成員（依近 28 日交易量排序）": [m for m, _ in members]}


def daily_report(date_str: str | None = None) -> dict:
    """The daily change report for a business date (default: latest)."""
    d = parse_date(date_str)
    with engine_at(d) as (e, d):
        path = DATA_DIR / "reports" / f"{d}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            findings = raw["findings"]
            return {"日期": str(d), "資料品質": [x["message"] for x in raw["data_quality"]] or ["正常"],
                    "關鍵指標": [{"指標": k["metric"], "當日": ctx().fmt(k["metric"], k["value"]),
                              "vs上週同日": pct(k["kind"], k["vs_last_week"]),
                              "近7日vs前7日": pct(k["kind"], k["week_vs_prev"])} for k in raw["kpis"]],
                    "發現": [_finding_from_json(f) for f in findings]}
        from . import report  # generate on the fly when the daily job hasn't produced it
        result = report.run(ctx(), d, e)
        return {"日期": str(d), "資料品質": [x["message"] for x in result["data_quality"]] or ["正常"],
                "發現": [finding_dict(e, f) for f in result["findings"]]}


def _finding_from_json(f: dict) -> dict:
    windows = ctx().config["windows"]
    out = {"範圍": f["label"], "時間窗": windows[f["window"]]["label"], "新發現": f["new"],
           "顯著指標": []}
    for s in f["stats"]:
        if s["significant"]:
            out["顯著指標"].append(f"{s['metric']} {pct(s['kind'], s['change'])}（本期 {ctx().fmt(s['metric'], s['current'])}，"
                               f"比較基準 {ctx().fmt(s['metric'], s['baseline'])}）")
    for k_src, k_dst in (("explains", "也解釋了"), ("notes", "注意")):
        if f.get(k_src):
            out[k_dst] = f[k_src]
    if f.get("since"):
        out["持續自"] = f["since"]
    return out


def metric_change(metric: str | None = None, filters: dict | None = None, date_str: str | None = None,
                  window: str = "day") -> dict:
    """Value and change of one metric (or all report metrics) for a scope, date and window."""
    d, window, note = resolve_period(date_str, window)
    window = window_name(window)
    with engine_at(d) as (e, d):
        scope = resolve_scope(e, filters)
        bases = scope_bases(e, scope)
        metrics = [metric_name(metric)] if metric else e.metrics
        stats = []
        for m in metrics:
            if not e.allowed(m, tuple(scope)):
                stats.append({"指標": m, "說明": "此指標在這個範圍無法計算（訂單數類指標不能依品類/商品切分）"})
                continue
            s = e.stat(bases, m, window, e.z_threshold(scope), member=bool(scope))
            stats.append({**stat_dict(e, s), "判讀": reading(e, s, scope)} if s
                         else {"指標": m, "說明": "歷史資料不足，無法比較"})
        out = {"範圍": e.label(scope), "日期": str(d), "期間": period_text(e, d, window), "結果": stats,
               "顯著的定義": f"z 值絕對值 ≥ {e.z_threshold(scope)} 且變動夠大"}
        if note:
            out["日期解讀"] = note
        return out


def explain_change(metric: str, filters: dict | None = None, date_str: str | None = None,
                   window: str = "day") -> dict:
    """Why did a metric change for a scope? Evidence from the data along the model's relationships."""
    d, window, note = resolve_period(date_str, window)
    window = window_name(window)
    metric = metric_name(metric)
    c = ctx()
    with engine_at(d) as (e, d):
        scope = resolve_scope(e, filters)
        bases = scope_bases(e, scope)
        if not e.allowed(metric, tuple(scope)):
            raise ToolError(f"{metric} 不能依 {e.label(scope)} 切分（訂單數類指標只能依區域、門市、通路、顧客分群）")
        s = e.stat(bases, metric, window, e.z_threshold(scope), member=bool(scope))
        if s is None:
            raise ToolError("歷史資料不足，無法比較")
        out = {"問題": f"{e.label(scope)} 的 {metric}（{e.windows[window]['label']}，{d}）",
               "期間": period_text(e, d, window), "變化": stat_dict(e, s), "判讀": reading(e, s, scope)}
        if note:
            out["日期解讀"] = note

        # 1) same scope, other metrics → volume / price / discount / cost pattern
        others = []
        for m in list(e.metrics) + [x for x in ("成本", "毛利", "折扣金額") if x in c.formulas]:
            if m == metric or not e.allowed(m, tuple(scope)):
                continue
            st = e.stat(bases, m, window, e.z_threshold(scope), member=bool(scope))
            if st:
                others.append(stat_dict(e, st)["display"])
        out["同範圍其他指標"] = others
        pattern = volume_price_reading(e, bases, scope, window)
        if pattern:
            out["量價判讀"] = pattern

        # 2) model identities (e.g. 銷售額 = 訂單數 × 客單價)
        ident = []
        for y, r in c.drivers(metric):
            if e.allowed(y, tuple(scope)):
                ys = e.stat(bases, y, window, member=bool(scope))
                rs = e.stat(bases, r, window, member=bool(scope))
                if ys and rs:
                    ident.append(f"{metric} = {y} × {r}：{y} {pct(ys.kind, ys.change)}、{r} {pct(rs.kind, rs.change)}")
        if ident:
            out["模型拆解"] = ident

        # 3) breakdown by every other dimension (contribution / rate-mix)
        out["依維度拆解"] = breakdown(e, scope, metric, window, s)

        # 3b) one level deeper: test the biggest contributors as scopes of their own
        out["深入一層"] = drill_down(e, scope, metric, window)

        # 4) is it specific to this scope or part of something broader?
        out["範圍判斷"] = broader_context(e, scope, metric, window, s)

        # 5) context: data quality, calendar, new stores
        notes = []
        for issue in e.data_quality(d):
            if issue.get("unit") and scope.get(c.config["data_quality"]["unit"], issue["unit"]) == issue["unit"]:
                notes.append(f"資料品質：{issue['message']}")
            elif not issue.get("unit"):
                notes.append(f"資料品質：{issue['message']}")
        f = Finding(scope, window, [s], 1.0)
        e.annotate(f, d, [])
        notes += [n for n in f.notes if n not in notes]
        out["脈絡"] = notes or ["無節日、新店或資料品質問題"]
        return out


def volume_price_reading(e: Engine, bases: dict, scope: dict, window: str) -> list[str]:
    """Deterministic reading of volume / price / discount / margin moves for the scope, so a small model
    doesn't have to infer them (e.g. 'price flat but margin down → unit cost went up')."""
    th = e.th
    st = {}
    for m in ("銷售數量", "訂單數", "平均成交單價", "折扣率", "毛利率"):
        if m in ctx().formulas and e.allowed(m, tuple(scope)):
            s = e.stat(bases, m, window, member=bool(scope))
            if s:
                st[m] = s

    def moved(m: str) -> int:
        if m not in st:
            return 0
        s = st[m]
        limit = th["min_ratio_change"] if s.kind == "abs" else th["min_change_pct"]
        return 0 if abs(s.change) < limit else (1 if s.change > 0 else -1)

    def show(m: str) -> str:
        return f"{m} {pct(st[m].kind, st[m].change)}"

    out = []
    vol = "銷售數量" if "銷售數量" in st else "訂單數"
    tags = []
    if moved(vol):
        tags.append(("量增" if moved(vol) > 0 else "量減") + f"（{show(vol)}）")
    index = like_for_like_price(e, scope, window)
    price_move = 0
    if index is not None and abs(index) >= 0.02:
        price_move = 1 if index > 0 else -1
        tags.append(("價升" if index > 0 else "價降") + f"（同品項售價 {index:+.1%}，已排除商品組合影響）")
    elif index is None and moved("平均成交單價"):
        price_move = moved("平均成交單價")
        tags.append(("價升" if price_move > 0 else "價降") + f"（{show('平均成交單價')}）")
    if moved("折扣率"):
        tags.append(("折扣加深" if moved("折扣率") > 0 else "折扣減少") + f"（{show('折扣率')}）")
    if moved("毛利率"):
        tags.append(("毛利率上升" if moved("毛利率") > 0 else "毛利率下降") + f"（{show('毛利率')}）")
    if tags:
        out.append("型態：" + "、".join(tags))
    margin, price, disc = moved("毛利率"), price_move, moved("折扣率")
    if margin > 0 and price > 0:
        out.append("毛利率上升時售價也上升：主要來自價格調整（漲價）")
    elif margin < 0 and disc > 0:
        out.append("毛利率下降時折扣加深：主要來自折扣／促銷")
    elif margin < 0 and price <= 0 and disc <= 0:
        out.append("售價與折扣沒有同步變化，但毛利率下降：代表單位成本上升（或低毛利商品占比提高）")
    elif margin > 0 and price <= 0 and disc >= 0:
        out.append("售價沒有上升，但毛利率上升：代表單位成本下降（或高毛利商品占比提高）")
    if moved(vol) < 0 and price > 0:
        out.append("售價上升同時銷量下降：可能是漲價影響買氣（資料只能顯示同時發生，無法證明因果）")
    return out


def like_for_like_price(e: Engine, scope: dict, window: str, item_dim: str = "product") -> float | None:
    """Laspeyres price index for the scope: Σ p1·q0 / Σ p0·q0 − 1 over the items (products) sold in both
    periods. Unlike the average price, it is not moved by a shift in product mix."""
    f = ctx().formulas.get("平均成交單價")
    if item_dim not in ctx().dims or not f or not f.numerator or not f.denominator or item_dim in scope:
        return None
    cube = e.cube(tuple(scope) + (item_dim,))
    values = tuple(scope.values())
    idx = [i for i, m in enumerate(cube.members) if m[:len(values)] == values]
    if not idx:
        return None
    data = {b: a[:, idx] for b, a in cube.data.items()}
    days, mode = e.windows[window]["days"], e.windows[window].get("mode", "pop")
    cur_b, base_b = windowed(data, window, days, mode)
    cur, base = {b: a[-1] for b, a in cur_b.items()}, {b: a[-1] for b, a in base_b.items()}
    n1, d1 = evaluate(f.numerator, cur), evaluate(f.denominator, cur)
    n0, d0 = evaluate(f.numerator, base), evaluate(f.denominator, base)
    ok = (d1 > 0) & (d0 > 0)
    if not ok.any():
        return None
    p1, p0, q0 = n1[ok] / d1[ok], n0[ok] / d0[ok], d0[ok]
    return float((p1 * q0).sum() / (p0 * q0).sum() - 1)


def breakdown(e: Engine, scope: dict, metric: str, window: str, s: Stat, top: int = 4) -> list[dict]:
    c = ctx()
    formula = c.formulas[metric]
    days, mode = e.windows[window]["days"], e.windows[window].get("mode", "pop")
    rows = []
    for k, dim in c.dims.items():
        if k in scope or not e.allowed(metric, tuple(scope) + (k,)):
            continue
        cube = e.cube(tuple(scope) + (k,))
        values = tuple(scope.values())
        idx = [i for i, m in enumerate(cube.members) if m[:len(values)] == values]
        if len(idx) < 2:
            continue
        names = [cube.members[i][-1] for i in idx]
        data = {b: a[:, idx] for b, a in cube.data.items()}
        cur_b, base_b = windowed(data, window, days, mode)
        cur, base = {b: a[-1] for b, a in cur_b.items()}, {b: a[-1] for b, a in base_b.items()}
        if not formula.is_ratio:
            cv, bv = evaluate(formula.expr, cur), evaluate(formula.expr, base)
            delta = np.nan_to_num(cv - bv)
            total, spread = delta.sum(), np.abs(delta).sum()
            order = np.argsort(-np.abs(delta))[:top]
            items = [f"{names[i]}：{c.fmt(metric, cv[i])}（基準 {c.fmt(metric, bv[i])}，變動 {delta[i]:+,.0f}"
                     + (f"，佔總變動 {delta[i] / total:.0%}" if total else "") + "）" for i in order]
            rows.append({"維度": dim.label, "集中度": round(float(abs(delta[order[0]]) / spread), 2) if spread else 0,
                         "變動最大的成員": items})
        elif formula.numerator and formula.denominator:
            n1, n0 = evaluate(formula.numerator, cur), evaluate(formula.numerator, base)
            d1, d0 = evaluate(formula.denominator, cur), evaluate(formula.denominator, base)
            if d1.sum() == 0 or d0.sum() == 0:
                continue
            w1, w0 = d1 / d1.sum(), d0 / d0.sum()
            r1, r0 = n1 / np.where(d1 == 0, np.nan, d1), n0 / np.where(d0 == 0, np.nan, d0)
            rate = np.nan_to_num(w1 * (r1 - r0))
            mix = np.nan_to_num((w1 - w0) * (r0 - n0.sum() / d0.sum()))
            unit = (lambda v: f"{v * 100:+.2f} 個百分點") if e.kind(metric) == "abs" else (lambda v: f"{v:+,.1f}")
            order = np.argsort(-(np.abs(rate) + np.abs(mix)))[:top]
            items = [f"{names[i]}：{c.fmt(metric, r1[i])}（基準 {c.fmt(metric, r0[i])}，本身變化貢獻 {unit(rate[i])}，"
                     f"比重變化貢獻 {unit(mix[i])}）" for i in order]
            spread = np.abs(rate).sum() + np.abs(mix).sum()
            rows.append({"維度": dim.label,
                         "集中度": round(float((abs(rate[order[0]]) + abs(mix[order[0]])) / spread), 2) if spread else 0,
                         "合計": f"各{dim.label}本身變化 {unit(rate.sum())}、{dim.label}比重改變 {unit(mix.sum())}",
                         "變動最大的成員": items})
    rows.sort(key=lambda r: -r["集中度"])
    return rows


def drill_down(e: Engine, scope: dict, metric: str, window: str, top: int = 5) -> list[str]:
    """Sub-scopes (scope + one more dimension member), most anomalous first."""
    c = ctx()
    cands = []
    for k, dim in c.dims.items():
        if k in scope or not e.allowed(metric, tuple(scope) + (k,)):
            continue
        cube = e.cube(tuple(scope) + (k,))
        values = tuple(scope.values())
        for m in cube.members:
            if m[:len(values)] != values:
                continue
            sub = {**scope, k: m[-1]}
            bases = cube.column(m)
            if e.share(bases, window) < e.th["min_share"] / 2:
                continue
            st = e.stat(bases, metric, window, e.z_threshold(sub), member=True)
            if st:
                cands.append((abs(st.z), sub, st))
    cands.sort(key=lambda x: -x[0])
    out = []
    for _, sub, st in cands[:top]:
        flag = "顯著異常" if st.significant else ("偏大" if abs(st.z) >= 2 else "正常")
        out.append(f"{e.label(sub)}：{stat_dict(e, st)['display']}，z {st.z:+.1f}，{flag}")
    return out


def broader_context(e: Engine, scope: dict, metric: str, window: str, s: Stat) -> list[str]:
    if not scope:
        return ["範圍為整體"]
    own = s.change if s.expected is None else s.change - s.expected
    out = []
    for p in e.parents(scope) + ([{}] if len(scope) == 1 else []):
        rest = e.complement(p, scope) if p else _total_minus(e, scope)
        rs = e.stat(rest, metric, window, member=True) if rest else None
        if rs is None:
            continue
        rest_adj = rs.change if rs.expected is None else rs.change - rs.expected
        breadth = e.breadth(scope, p, metric, window, own) if p else None
        broad = (own and rest_adj / own >= BROAD) or (breadth is not None and breadth >= BROAD)
        label = e.label(p)
        verdict = ("也有同向且幅度相當的變化 → 屬於更大範圍的現象" if broad
                   else "變動幅度不到本範圍的一半 → 主要是這個範圍特有的變化")
        out.append(f"「{label}」扣除「{e.label(scope)}」後，{metric} {pct(rs.kind, rs.change)}"
                   + (f"，同層成員有 {breadth:.0%}（依交易量加權）同向變動" if breadth is not None else "")
                   + f"：{verdict}")
    return out


def _total_minus(e: Engine, scope: dict) -> dict | None:
    inner = e.bases_of(scope)
    total = e.cube(()).column(())
    return {b: total[b] - inner[b] for b in total if b in inner} if inner else None


def metric_trend(metric: str, filters: dict | None = None, start: str | None = None, end: str | None = None,
                 grain: str = "week") -> dict:
    """Time series of a metric for a scope (grain: day / week)."""
    metric = metric_name(metric)
    d1 = clamp_to_data(parse_date(end))
    with engine_at(d1) as (e, d1):
        d0, start_is_month = parse_period(start)
        d0 = d0.replace(day=1) if start_is_month else (d0 or d1 - timedelta(days=83))
        if (d1 - d0).days > 400:
            raise ToolError("期間最多 400 天")
        scope = resolve_scope(e, filters)
        if not e.allowed(metric, tuple(scope)):
            raise ToolError(f"{metric} 不能依 {e.label(scope)} 切分")
        bases = scope_bases(e, scope)
        dates = e.cube(()).dates
        i0 = max(dates.index(d0) if d0 in dates else 0, 0)
        f = ctx().formulas[metric]
        points = []
        step = 7 if grain == "week" else 1
        end_idx = len(dates) - 1
        idxs = list(range(end_idx, i0 - 1, -step))[::-1]
        for i in idxs:
            lo = max(i - step + 1, 0)
            agg = {b: a[lo:i + 1].sum(axis=0, keepdims=True) for b, a in bases.items()}
            v = float(evaluate(f.expr, agg)[0, 0])
            label = str(dates[i]) if step == 1 else f"{dates[lo]}～{dates[i]}"
            points.append({"期間": label, "值": ctx().fmt(metric, v)})
        return {"指標": metric, "範圍": e.label(scope), "粒度": "週（7 日加總後計算）" if step == 7 else "日", "序列": points,
                "注意": "本工具只列出數值，不判斷是否異常或顯著。要判斷請用 metric_change 或 explain_change；"
                        "與去年同期比較（含扣除整體成長）請用 window=\"year\"。"}


def data_quality(date_str: str | None = None) -> dict:
    d = parse_date(date_str)
    with engine_at(d) as (e, d):
        issues = e.data_quality(d)
        etl = e.db.execute("SELECT etl_rows, etl_status FROM etl_log WHERE business_date = ?", [d]).fetchone()
        return {"日期": str(d), "ETL": {"狀態": etl[1], "回報筆數": f"{int(etl[0]):,}"} if etl else "無紀錄",
                "問題": [i["message"] for i in issues] or ["未發現問題"]}


def metric_lineage(metric: str) -> dict:
    """Where a metric comes from: DAX, referenced measures, physical columns, report visuals."""
    c = ctx()
    if metric not in c.model.measures:
        raise ToolError(f"模型中沒有量值「{metric}」，可用：{'、'.join(c.model.measures)}")
    meas = c.model.measures[metric]
    f = c.formulas.get(metric)
    fact = c.model.tables[c.fact]
    cols = sorted({b.sql for b in c.bases.values() if f and b.name in f.bases})
    return {"量值": metric, "所在資料表": meas.table, "說明": meas.description, "DAX": meas.expression,
            "格式": meas.format_string, "引用的量值": f.refs if f else [],
            "實際計算": cols, "來源資料表": f"{fact.source_table}（Oracle）",
            "報表中使用的位置": [f"{u.page} / {u.title or u.visual}" for u in c.model.usage if metric in u.measures]
            or ["報表未直接使用"]}
