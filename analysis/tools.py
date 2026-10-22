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
from pathlib import Path

import numpy as np

from . import store
from .context import DATA_DIR, Context, load
from .engine import BROAD, WEEKDAY_LAGS, YEAR, Engine, Finding, Stat, evaluate, windowed
from .semantic import describe_filter

WINDOW_HELP = {
    "day": "單日：當日 vs 近 4 週同星期平均",
    "week": "近 7 日 vs 前 7 日（已扣除去年同期的季節性）",
    "month": "近 28 日 vs 前 28 日（已扣除去年同期的季節性）",
    "year": "近 28 日 vs 去年同期（相對整體成長）",
}


def window_help() -> dict[str, str]:
    """Window descriptions for the configured windows (a window may override its text with `help`)."""
    return {w: spec.get("help", WINDOW_HELP.get(w, spec["label"])) for w, spec in ctx().config["windows"].items()}


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
    with store.open_db(read_only=True) as db:
        last = store.last_snapshot_date(db)
    if last and d.replace(day=1) > last:  # a month after the data ends is not "the latest day"
        raise ToolError(f"{date_str} 超出資料範圍：資料只到 {last}")
    d = clamp_to_data(d)
    w = window if window in ("month", "year") else "month"
    return d, w, f"「{date_str}」解讀為整個月：使用截至 {d} 的近 28 日（window={w}）"


def why_not(e: Engine, metric: str, dims: tuple) -> str:
    """Why a metric cannot be split by these dimensions."""
    c = ctx()
    cross = [c.bases[b] for b in c.formulas[metric].bases if b in e.skipped_bases]
    if cross:
        return (f"{metric} 是「{cross[0].distinct_of}」的不重複計數，同一個 {cross[0].distinct_of} 會出現在很多天，"
                "每天的不重複數不能加總成一段期間的不重複數，所以不做期間比較")
    filtered = {f for b in c.formulas[metric].bases for f in c.bases[b].filters}
    hit = [c.dims[k].label for k in dims if (c.dims[k].table, c.dims[k].column) in filtered]
    if hit:
        return (f"{metric} 的 DAX 用 CALCULATE 固定篩選了「{'、'.join(hit)}」，"
                f"Power BI 依這個欄位切分時每個成員都顯示同一個值，所以不依它分析")
    return f"{metric} 只能依 {order_dim_labels()} 切分"


def order_dim_labels() -> str:
    with store.open_db(read_only=True) as db:
        dims = store.order_dims(db)
    return "、".join(ctx().dims[k].label for k in dims)


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
    if name in c.model.measures:
        raise ToolError(f"模型中有量值「{name}」（DAX：{c.model.measures[name].expression}），但分析引擎無法翻譯它"
                        f"（{c.skipped.get(name, '不在事實資料表上')}），所以無法計算變化；可用 metric_lineage 查看定義")
    msg = f"沒有指標「{name}」。可用指標：{'、'.join(c.formulas)}"
    members = [(m, dim) for m, dim in member_index().items() if m in name]
    if members:
        m, dim = max(members, key=lambda x: len(x[0]))
        metric = max(close, key=len) if close else c.roles["sales"]
        msg += (f"。注意：「{m}」是維度「{dim}」的成員，不是指標；"
                f"請改用 metric=\"{metric}\"、filters={{\"{dim}\": \"{m}\"}}")
    elif close:
        msg += f"；你是不是指：{'、'.join(close)}"
    raise ToolError(msg)


def window_name(w: str) -> str:
    if w not in ctx().config["windows"]:
        raise ToolError(f"window 需為 {'、'.join(ctx().config['windows'])} 其中之一（{window_help()}）")
    return w


def default_window(w: str, note: str | None) -> tuple[str, str | None]:
    """Datasets too small for single-day comparisons have no "day" window: use the configured default."""
    windows = ctx().config["windows"]
    if w == "day" and "day" not in windows:
        d = ctx().config.get("default_window", next(iter(windows)))
        msg = f"這份資料每天的交易量太少，單日比較不可靠，改用「{windows[d]['label']}」（window={d}）"
        return d, f"{note}；{msg}" if note else msg
    return w, note


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
    return {"本期": cur, "比較基準": base, "說明": window_help().get(window, w["label"])}


def reading(e: Engine, s: Stat, scope: dict) -> str:
    """Three-level reading instead of a bare yes/no."""
    th = e.z_threshold(scope)
    if s.note.startswith("樣本太少"):
        return f"{s.note}，無法判斷是否異常"
    if s.significant:
        return "顯著異常"
    if abs(s.z) >= 2:
        return f"變化偏大，但未達顯著門檻（z 需 ≥ {th}），宜往下一層查看是哪個部分顯著"
    return "在正常波動範圍內"


def stat_dict(e: Engine, s: Stat, member: bool = True) -> dict:
    c = ctx()
    exp_label = e.expected_label(s.window, member)
    out = {"指標": s.metric, "本期": c.fmt(s.metric, s.current), "比較基準": c.fmt(s.metric, s.baseline),
           "變化": pct(s.kind, s.change), "z": round(s.z, 1), "顯著": bool(s.significant)}
    if s.expected is not None:
        out[exp_label] = pct(s.kind, s.expected)
    out["display"] = (f"{s.metric} {pct(s.kind, s.change)}（本期 {out['本期']}，比較基準 {out['比較基準']}"
                      + (f"，{exp_label} {out[exp_label]}" if s.expected is not None else "") + "）")
    return out


def fact(scope: dict, metric: str, change: float, kind: str, significant: bool | None, verdict: str = "") -> dict:
    """Machine-readable claim for the answer checker (removed before the result reaches the model):
    which scope/metric moved which way, and whether a tool judged it significant (None = not judged)."""
    dims = ctx().dims
    return {"scope": {dims[k].label: v for k, v in scope.items()}, "metric": metric,
            "change": None if change is None or change != change else round(float(change), 6), "kind": kind,
            "significant": significant, "verdict": verdict}


def stat_fact(e: Engine, scope: dict, s: Stat) -> dict:
    # too small a sample is not significant either: "顯著" about it is wrong, "無法判斷" is right
    out = fact(scope, s.metric, s.change, s.kind, bool(s.significant), reading(e, s, scope))
    if s.expected is not None:  # the change the verdict is based on: vs last year's change / vs the total
        adj = s.change - s.expected if s.kind == "abs" else (1 + s.change) / (1 + s.expected) - 1
        out["adjusted"] = round(float(adj), 6)
    out["window"] = s.window
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
        "時間窗": window_help(),
        "報表篩選": report_filter_text(),
        "無法分析的量值": {m: {"說明": c.model.measures[m].description, "DAX": c.model.measures[m].expression,
                         "原因": f"分析引擎無法翻譯這個 DAX（{why}），可用 metric_lineage 查看定義，但無法計算變化"}
                     for m, why in c.skipped.items()},
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
                              "vs上週同日": pct(k["kind"], None if signed(k["metric"]) else k["vs_last_week"]),
                              "近7日vs前7日": pct(k["kind"], None if signed(k["metric"]) else k["week_vs_prev"])}
                             for k in raw["kpis"]],
                    "發現": [_finding_from_json(f) for f in findings],
                    "_facts": [fact(f["scope"], st["metric"], st["change"], st["kind"], True, "顯著異常")
                               for f in findings for st in f["stats"] if st["significant"]]}
        from . import report  # generate on the fly when the daily job hasn't produced it
        result = report.run(ctx(), d, e)
        return {"日期": str(d), "資料品質": [x["message"] for x in result["data_quality"]] or ["正常"],
                "發現": [finding_dict(e, f) for f in result["findings"]],
                "_facts": [stat_fact(e, f.scope, st) for f in result["findings"] for st in f.significant]}


def signed(metric: str) -> bool:
    """Metrics that can be negative (roles.signed): their % change is not shown."""
    return metric in ctx().roles.get("signed", [])


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
    window, note = default_window(window, note)
    window = window_name(window)
    with engine_at(d) as (e, d):
        scope = resolve_scope(e, filters)
        bases = scope_bases(e, scope)
        metrics = [metric_name(metric)] if metric else e.metrics
        stats, facts = [], []
        for m in metrics:
            if not e.allowed(m, tuple(scope)):
                stats.append({"指標": m, "說明": f"此指標在這個範圍無法計算（{why_not(e, m, tuple(scope))}）"})
                continue
            s = e.stat(bases, m, window, e.z_threshold(scope), member=bool(scope))
            if s:
                stats.append({**stat_dict(e, s), "判讀": reading(e, s, scope)})
                facts.append(stat_fact(e, scope, s))
            else:
                stats.append({"指標": m, "說明": untestable(e, bases, m, window)})
        out = {"範圍": e.label(scope), "日期": str(d), "期間": period_text(e, d, window), "結果": stats,
               "顯著的定義": f"z 值絕對值 ≥ {e.z_threshold(scope)} 且變動夠大"}
        if note:
            out["日期解讀"] = note
        out["_facts"] = facts
        return out


def untestable(e: Engine, bases: dict, metric: str, window: str) -> str:
    if e.signed(bases, metric, window):
        margin = ctx().roles.get("margin_rate")
        return (f"{metric} 可能為負或接近 0，變化率沒有意義，不判斷是否異常"
                + (f"；請改看 {margin}" if margin in ctx().formulas else ""))
    return "歷史資料不足，無法比較"


def explain_change(metric: str, filters: dict | None = None, date_str: str | None = None,
                   window: str = "day") -> dict:
    """Why did a metric change for a scope? Evidence from the data along the model's relationships."""
    d, window, note = resolve_period(date_str, window)
    window, note = default_window(window, note)
    window = window_name(window)
    metric = metric_name(metric)
    c = ctx()
    with engine_at(d) as (e, d):
        scope = resolve_scope(e, filters)
        bases = scope_bases(e, scope)
        if not e.allowed(metric, tuple(scope)):
            raise ToolError(f"{metric} 不能依 {e.label(scope)} 切分：{why_not(e, metric, tuple(scope))}")
        s = e.stat(bases, metric, window, e.z_threshold(scope), member=bool(scope))
        if s is None:
            raise ToolError(untestable(e, bases, metric, window))
        out = {"問題": f"{e.label(scope)} 的 {metric}（{e.windows[window]['label']}，{d}）",
               "期間": period_text(e, d, window), "變化": stat_dict(e, s), "判讀": reading(e, s, scope)}
        if note:
            out["日期解讀"] = note
        facts = [stat_fact(e, scope, s)]

        # 1) same scope, other metrics → volume / price / discount / cost pattern
        others = []
        # for a ratio question, amounts (cost, profit…) mostly follow volume and read like causes: leave them out
        ratio_q = c.formulas[metric].is_ratio
        extra = [] if ratio_q else c.roles.get("extra", [])
        volume = {c.roles["sales"], c.roles["volume"], c.roles["orders"]}
        for m in list(e.metrics) + [x for x in extra if x in c.formulas and x not in e.metrics]:
            if m == metric or not e.allowed(m, tuple(scope)):
                continue
            if ratio_q and not c.formulas[m].is_ratio and m not in volume:
                continue
            st = e.stat(bases, m, window, e.z_threshold(scope), member=bool(scope))
            if st:
                others.append(stat_dict(e, st)["display"])
                facts.append(stat_fact(e, scope, st))
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
        out["依維度拆解"] = breakdown(e, scope, metric, window, s, facts=facts)

        # 3b) one level deeper: test the biggest contributors as scopes of their own
        drilled = []
        out["深入一層"] = drill_down(e, scope, metric, window, found=drilled)
        facts += [stat_fact(e, sub, st) for sub, st in drilled]

        # 4) is it specific to this scope or part of something broader?
        out["範圍判斷"] = broader_context(e, scope, metric, window, s)

        # 5) context: data quality, calendar, new stores
        notes = []
        for issue in e.data_quality(d):
            if issue.get("unit") and scope.get(c.dq.get("unit"), issue["unit"]) == issue["unit"]:
                notes.append(f"資料品質：{issue['message']}")
            elif not issue.get("unit"):
                notes.append(f"資料品質：{issue['message']}")
        f = Finding(scope, window, [s], 1.0)
        e.annotate(f, d, [])
        notes += [n for n in f.notes if n not in notes]
        out["脈絡"] = notes or ["無節日、新店或資料品質問題" if c.dq else "無節日或新單位等特殊脈絡"]
        out["重點"], key = key_point(e, scope, metric, s, drilled, out)
        out["_facts"] = facts
        out["_key"] = key
        return out


def key_point(e: Engine, scope: dict, metric: str, s: Stat, drilled: list, out: dict) -> tuple[str, list[str]]:
    """Deterministic one-line reading the answer's conclusion should follow, and the members it must name."""
    c = ctx()
    verdict = reading(e, s, scope)
    way = "上升" if s.change > 0 else "下降" if s.change < 0 else "持平"
    ref = f"（{e.expected_label(s.window, bool(scope))} {pct(s.kind, s.expected)}）" if s.expected is not None else ""
    head = f"{e.label(scope)} 的 {metric}{way} {pct(s.kind, s.change)}{ref}，{verdict.split('，')[0]}"
    parts, key = [head], []
    sig = [(sub, st) for sub, st in drilled if st.significant and np.sign(st.change) == np.sign(s.change)]
    if sig:
        sub, st = sig[0]
        # a sub-scope explains the change only if it stands out; moving like the whole scope means a broad effect
        if not s.significant or abs(st.change) >= 1.5 * abs(s.change):
            parts.append(f"顯著異常的部分：{e.label(sub)}（{metric} {pct(st.kind, st.change)}）")
            key = [v for k, v in sub.items() if k not in scope]
        else:
            parts.append(f"各部分普遍同向變化（例如 {e.label(sub)} {pct(st.kind, st.change)}），不是集中在單一部分")
    elif out.get("依維度拆解"):
        top = out["依維度拆解"][0]
        if top["集中度"] >= 0.5 and top["變動最大的成員"] and abs(s.z) >= 2:
            member = top["變動最大的成員"][0].split("：")[0]
            dim = next(k for k, d in c.dims.items() if d.label == top["維度"])
            sub = e.bases_of({**scope, dim: member})
            whole = e.bases_of(scope) if scope else e.cube(()).column(())
            # its weight in the comparison period: a member holding half of the scope naturally carries
            # half of a broad change, which is not a concentration
            weight = e.sample_size(sub, s.window)[1] / max(e.sample_size(whole, s.window)[1], 1) if sub else 1.0
            if top["集中度"] >= 1.5 * weight:
                parts.append(f"變化集中在{top['維度']}「{member}」（佔變動 {top['集中度']:.0%}，"
                             f"比較期間只佔明細 {weight:.0%}）")
    if any(n.startswith("資料品質") for n in out.get("脈絡", [])):
        parts.append("注意：可能是資料缺漏，而非實際營運變化")
    reading_lines = out.get("量價判讀") or []
    r = c.roles
    if metric in (r["margin_rate"], r["discount_rate"], r["unit_price"]):
        # margin / price questions: the sentence that names the mechanism (price, discount, cost)
        parts += [x for x in reading_lines[1:] if metric in x or "售價" in x][:1] or reading_lines[:1]
    else:
        # volume / sales questions: the volume-price pattern, not the margin story
        parts += [x for x in reading_lines if x.startswith("型態") or "銷量" in x][:2]
    return "；".join(parts), key


def volume_price_reading(e: Engine, bases: dict, scope: dict, window: str) -> list[str]:
    """Deterministic reading of volume / price / discount / margin moves for the scope, so a small model
    doesn't have to infer them (e.g. 'price flat but margin down → unit cost went up')."""
    th = e.th
    r = ctx().roles
    vol_m, ord_m, price_m, disc_m, margin_m = r["volume"], r["orders"], r["unit_price"], r["discount_rate"], r["margin_rate"]
    st = {}
    for m in (vol_m, ord_m, price_m, disc_m, margin_m):
        if m in ctx().formulas and e.allowed(m, tuple(scope)):
            s = e.stat(bases, m, window, member=bool(scope))
            if s:
                st[m] = s

    def moved(m: str) -> int:
        if m not in st:
            return 0
        s = st[m]
        limit = th["min_ratio_change"] if s.kind == "abs" else th["min_change_pct"]
        # on noisy data a move only counts when it is at least unusual for this scope (pattern_min_z)
        if abs(s.change) < limit or abs(s.z) < th.get("pattern_min_z", 0):
            return 0
        return 1 if s.change > 0 else -1

    def show(m: str) -> str:
        return f"{m} {pct(st[m].kind, st[m].change)}"

    out = []
    vol = vol_m if vol_m in st else ord_m
    tags = []
    if moved(vol):
        tags.append(("量增" if moved(vol) > 0 else "量減") + f"（{show(vol)}）")
    index = like_for_like_price(e, scope, window)
    price_move = 0
    if index is not None and abs(index) >= th.get("price_index_move", 0.02):
        price_move = 1 if index > 0 else -1
        tags.append(("價升" if index > 0 else "價降") + f"（同品項售價 {index:+.1%}，已排除商品組合影響）")
    elif index is None and moved(price_m):
        price_move = moved(price_m)
        tags.append(("價升" if price_move > 0 else "價降") + f"（{show(price_m)}）")
    if moved(disc_m):
        tags.append(("折扣加深" if moved(disc_m) > 0 else "折扣減少") + f"（{show(disc_m)}）")
    if moved(margin_m):
        tags.append((f"{margin_m}上升" if moved(margin_m) > 0 else f"{margin_m}下降") + f"（{show(margin_m)}）")
    if tags:
        out.append("型態：" + "、".join(tags))
    margin, price, disc = moved(margin_m), price_move, moved(disc_m)
    if margin > 0 and price > 0:
        out.append(f"{margin_m}上升時售價也上升：主要來自價格調整（漲價）")
    elif margin < 0 and disc > 0:
        out.append(f"{margin_m}下降時折扣加深：主要來自折扣／促銷")
    elif margin < 0 and price <= 0 and disc <= 0:
        out.append(f"售價與折扣沒有同步變化，但{margin_m}下降：代表單位成本上升（或低{margin_m}商品占比提高）")
    elif margin > 0 and price <= 0 and disc >= 0:
        out.append(f"售價沒有上升，但{margin_m}上升：代表單位成本下降（或高{margin_m}商品占比提高）")
    if moved(vol) < 0 and price > 0:
        out.append("售價上升同時銷量下降：可能是漲價影響買氣（資料只能顯示同時發生，無法證明因果）")
    return out


def like_for_like_price(e: Engine, scope: dict, window: str) -> float | None:
    """Laspeyres price index for the scope: Σ p1·q0 / Σ p0·q0 − 1 over the items (products) sold in both
    periods. Unlike the average price, it is not moved by a shift in product mix."""
    item_dim = ctx().config.get("price_item", "product")
    f = ctx().formulas.get(ctx().roles["unit_price"])
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


def breakdown(e: Engine, scope: dict, metric: str, window: str, s: Stat, top: int = 4,
              facts: list | None = None) -> list[dict]:
    c = ctx()
    formula = c.formulas[metric]
    days, mode = e.windows[window]["days"], e.windows[window].get("mode", "pop")
    rows = []
    for k, dim in c.dims.items():
        if k in scope or not dim.scan or not e.allowed(metric, tuple(scope) + (k,)):
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
            if facts is not None:
                facts += [fact({**scope, k: names[i]}, metric, cv[i] / bv[i] - 1 if bv[i] > 0 else None, "pct", None)
                          for i in order]
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
            if facts is not None:
                kind = e.kind(metric)
                facts += [fact({**scope, k: names[i]}, metric,
                               (r1[i] - r0[i]) if kind == "abs" else (r1[i] / r0[i] - 1 if r0[i] else None), kind, None)
                          for i in order]
            spread = np.abs(rate).sum() + np.abs(mix).sum()
            rows.append({"維度": dim.label,
                         "集中度": round(float((abs(rate[order[0]]) + abs(mix[order[0]])) / spread), 2) if spread else 0,
                         "合計": f"各{dim.label}本身變化 {unit(rate.sum())}、{dim.label}比重改變 {unit(mix.sum())}",
                         "變動最大的成員": items})
    rows.sort(key=lambda r: -r["集中度"])
    return rows


def drill_down(e: Engine, scope: dict, metric: str, window: str, top: int = 5, found: list | None = None) -> list[str]:
    """Sub-scopes (scope + one more dimension member), most anomalous first."""
    c = ctx()
    cands = []
    for k, dim in c.dims.items():
        if k in scope or not dim.scan or not e.allowed(metric, tuple(scope) + (k,)):
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
        flag = ("樣本太少" if st.note.startswith("樣本太少") else "顯著異常" if st.significant
                else "偏大" if abs(st.z) >= 2 else "正常")
        out.append(f"{e.label(sub)}：{stat_dict(e, st)['display']}，z {st.z:+.1f}，{flag}")
        if found is not None:
            found.append((sub, st))
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
        out = {"日期": str(d)}
        if ctx().dq.get("etl_table"):
            etl = e.db.execute("SELECT etl_rows, etl_status FROM etl_log WHERE business_date = ?", [d]).fetchone()
            out["ETL"] = {"狀態": etl[1], "回報筆數": f"{int(etl[0]):,}"} if etl else "無紀錄"
        else:
            rows = e.db.execute("SELECT fact_rows FROM snapshot_log WHERE business_date = ?", [d]).fetchone()
            out["明細筆數"] = f"{int(rows[0]):,}" if rows else "無資料"
            out["ETL"] = "資料來源沒有載入紀錄可比對（例如 Excel），只能檢查明細筆數"
        out["問題"] = [i["message"] for i in issues] or ["未發現問題"]
        return out


def metric_lineage(metric: str) -> dict:
    """Where a metric comes from: DAX, referenced measures, physical columns, report visuals."""
    c = ctx()
    if metric not in c.model.measures:
        raise ToolError(f"模型中沒有量值「{metric}」，可用：{'、'.join(c.model.measures)}")
    meas = c.model.measures[metric]
    f = c.formulas.get(metric)
    fact = c.model.tables[c.fact]
    cols = sorted({b.readable() for b in c.bases.values() if f and b.name in f.bases})
    return {"量值": metric, "所在資料表": meas.table, "說明": meas.description, "DAX": meas.expression,
            "格式": meas.format_string, "引用的量值": f.refs if f else [],
            "實際計算": cols, "來源資料表": source_text(fact),
            "報表中使用的位置": [f"{u.page} / {u.title or u.visual}" + (f"（{'；'.join(u.filters)}）" if u.filters else "")
                           for u in c.model.usage if metric in u.measures] or ["報表未直接使用"],
            "報表篩選": report_filter_text()}


def report_filter_text() -> dict:
    """Which report filters the analysis applies, and which ones only hold on some pages / visuals."""
    c = ctx()
    out = {"整份報表（分析已套用）": [describe_filter(f) for f in c.report_filters] or ["無"]}
    pages = {}
    for u in c.model.usage:
        for f in u.filters:
            if f.startswith("頁面篩選"):
                pages.setdefault(u.page, set()).add(f)
    if pages:
        out["只在某些頁面有效（分析未套用）"] = {p: sorted(fs) for p, fs in pages.items()}
    visuals = [f"{u.page} / {u.title or u.visual}：{f}" for u in c.model.usage for f in u.filters if f.startswith("此圖")]
    if visuals:
        out["只在某些圖表有效（分析未套用）"] = visuals
    if c.warnings:
        out["無法套用"] = [w for w in c.warnings if w.startswith("報表篩選")]
    return out


def source_text(table) -> str:
    src = table.source or {"kind": "oracle"}
    if src["kind"] == "excel":
        return f"{table.source_table}（Excel 工作表，檔案 {Path(src['file'].replace(chr(92), '/')).name}）"
    return f"{table.source_table}（Oracle）"


# ---------------------------------------------------------------------------------- other reports, author's intent
@lru_cache(maxsize=1)
def catalog_():
    """All reports in one graph (built once per process; reading the key columns takes a few seconds)."""
    from . import catalog
    return catalog.build(catalog.load_reports())


def _report(cat, name: str | None):
    from . import dataset
    name = name or dataset.NAME
    r = cat.report(name)
    if r is None:
        raise ToolError(f"沒有報表「{name}」，可用：{'、'.join(f'{x.name}（{x.title}）' for x in cat.reports)}")
    return r


def report_emphasis(report: str | None = None) -> dict:
    """What the report's author set to stand out: rules and thresholds, target lines, single members in colour,
    Top N filters, comparison values on cards; layout hints separately (inferred, not stated)."""
    from . import dataset, intent
    cat = catalog_()
    r = _report(cat, report)
    explicit = [e for e in r.emphasis if e.explicit and e.kind not in ("bookmarks", "hidden_page")]
    out = {"報表": f"{r.title}（{r.name}）",
           "作者明確設定": [f"{e.page}／{e.visual}：{e.text}" for e in explicit] or ["沒有條件式格式、目標線或凸顯設定"],
           "版面提示（依位置與大小推斷，不是作者明確設定）": [f"{e.page}：{e.text}" for e in r.emphasis
                                                 if e.kind == "layout"]}
    if explicit:
        out["哪些成員目前是哪個顏色"] = ("規則逐列（每個門市、品類…）套用，不是看整體；"
                                "要知道目前哪些成員落在哪個顏色，用 explain_visual 指定那張圖")
    return out


def cross_report(name: str = "", other: str | None = None) -> dict:
    """A measure / column / table across all reports: its meaning in physical columns, measures in other reports
    with the same meaning (any name) or the same name but another meaning, the sources and keys it shares, the
    author's settings on it. With other: how the two are connected. Without name: an overview of all reports."""
    from . import catalog, dataset
    cat = catalog_()
    if not name.strip():
        return {"報表": [f"{r.title}（{r.name}）：{len(r.model.tables)} 張表、{len(r.model.measures)} 個量值"
                       for r in cat.reports],
                "共用的資料來源": [f"{catalog.source_label(s)}：" + "、".join(f"{r}／{t}" for r, t in uses)
                             for s, uses in cat.shared_sources.items()],
                "名稱不同但意思相同": [" = ".join(f"{c.report}「{c.measure}」" for c in g) for g in cat.same
                               if len({c.measure for c in g}) > 1 and len({c.report for c in g}) > 1],
                "同名但定義不同": [f"「{cf.name}」：" + "；".join(
                    f"{'、'.join(c.report for c in g)} 的「{cf.name}」= {g[0].readable()}"
                    + (f"（= {dataset.NAME} 的 {t}）" if g[0].report != dataset.NAME
                       and (t := cat.in_terms_of(g[0], dataset.NAME)) else "")
                    for g in cf.groups) for cf in cat.conflicts if cf.same_system],
                "代碼比對": [_key_text(k) for k in cat.keys]}
    if (r := cat.report(name)) and not other:
        return _report_view(cat, r)
    node = _find_node(cat, name)
    d = cat.nodes[node]
    out = {"對象": cat.label(node)}
    if d["kind"] == "measure":
        c = cat.measures[(d["report"], d["name"])]
        out["DAX"] = c.expression
        out["展開到來源欄位"] = c.readable()
        same = [x for x in cat.group_of(c.report, c.measure) if (x.report, x.measure) != (c.report, c.measure)]
        out["其他報表中意思相同的量值"] = [f"{x.report}「{x.measure}」" + ("（名稱不同）" if x.measure != c.measure else "")
                                for x in same] or ["沒有"]
        here = dataset.NAME
        if c.report != here and (t := cat.in_terms_of(c, here)):
            out[f"用 {here} 報表的量值表示"] = f"{c.report}「{c.measure}」= {t}"
        mine = (c.report, c.measure)
        clashes, listed = [], set()
        for cf in sorted(cat.conflicts, key=lambda cf: not cf.same_system):  # same database first
            for g in cf.groups if cf.name == c.measure else []:
                ids = frozenset((x.report, x.measure) for x in g)
                if mine in ids or ids in listed:
                    continue
                listed.add(ids)
                text = f"{'、'.join(x.report for x in g)}：{g[0].readable()}"
                t = cat.in_terms_of(g[0], here) if cf.same_system and g[0].report != here else None
                if t:
                    text += f"，也就是 {here} 報表的 {t}"
                clashes.append(text + ("" if cf.same_system else "（不同系統的資料，非同一件事）"))
        if clashes:
            out["同名但定義不同"] = list(dict.fromkeys(clashes))
    elif d["kind"] == "column":
        src = [b for a, r, b in cat.edges if a == node and r == "來自"]
        if src:
            out["來源欄位"] = cat.nodes[src[0]]["name"]
            out["其他報表讀同一個來源欄位"] = [cat.label(a) for a, r, b in cat.edges
                                    if b == src[0] and r == "來自" and a != node] or ["沒有"]
        keys = [k for k in cat.keys if node[len("column:"):] in k.left + k.right]
        if keys:
            out["代碼比對"] = [_key_text(k) for k in keys]
    elif d["kind"] == "table":
        src = [b for a, r, b in cat.edges if a == node and r == "讀取"]
        if src:
            out["來源"] = cat.nodes[src[0]]["name"]
            out["其他報表讀同一張表"] = [cat.label(a) for a, r, b in cat.edges
                                 if b == src[0] and r == "讀取" and a != node] or ["沒有"]
    marks = [cat.nodes[a]["name"] for a, r, b in cat.edges if b == node and r == "凸顯"]
    if marks:
        out["報表作者的設定"] = marks
    if other:
        target = _find_node(cat, other)
        path = cat.path(node, target)
        out[f"與「{other}」的關係"] = cat.path_text(path, node) if path else \
            ["在所有報表的模型、來源與代碼對應中都找不到關聯"]
    return out


def _report_view(cat, r) -> dict:
    """Every measure of another report next to this report's measures: same meaning, same name only, or new."""
    from . import dataset
    from .catalog import source_label
    here = dataset.NAME
    rows = []
    for (rep, m), c in cat.measures.items():
        if rep != r.name:
            continue
        same = [x.measure for x in cat.group_of(rep, m) if x.report == here]
        if same:
            rows.append(f"{r.name}「{m}」= {here}「{same[0]}」" + ("（名稱不同、意思相同）" if same[0] != m else "（相同）"))
        elif (here, m) in cat.measures:
            t = cat.in_terms_of(c, here)
            rows.append(f"{r.name}「{m}」與 {here}「{m}」同名但定義不同：{r.name} 的是 {c.readable()}"
                        + (f"，也就是 {here} 的 {t}" if t else ""))
        else:
            rows.append(f"{r.name}「{m}」（{here} 沒有對應的量值）= {cat.in_terms_of(c, here, partial=True)}")
    shared = [f"{source_label(sid)}：{'、'.join(f'{x}／{t}' for x, t in uses)}"
              for sid, uses in cat.shared_sources.items() if {x for x, _ in uses} >= {r.name, here}]
    return {"報表": f"{r.title}（{r.name}）", f"量值與 {here} 的對照": rows, "共用的資料來源": shared or ["沒有"],
            "代碼比對": [_key_text(k) for k in cat.keys if any(x.startswith(r.name + ":") for x in k.left + k.right)]
            or ["沒有跨系統的代碼"]}


def _find_node(cat, name: str) -> str:
    from . import dataset
    found = cat.find(name, ("measure", "column", "table"))
    if not found:
        raise ToolError(f"所有報表中都找不到「{name}」。可以用 報表:名稱（例如 store_targets:營收）或 表[欄位] 指定")
    mine = [n for n in found if cat.nodes[n].get("report") == dataset.NAME]
    return (mine or found)[0]


def _key_text(k) -> str:
    from .catalog import short
    near = {a for a, _ in k.near} | {b for _, b in k.near}
    parts = [f"{'、'.join(k.left)} ↔ {'、'.join(k.right)}：相同 {k.shared} 個"]
    if k.near:
        parts.append("只差大小寫或空白 " + "、".join(f"「{a}」≈「{b}」" for a, b in k.near))
    lo = [v for v in k.left_only if v not in near]
    ro = [v for v in k.right_only if v not in near]
    if lo:
        parts.append(f"只在 {short(k.left_source)}：" + "、".join(lo[:10]) + ("…" if len(lo) > 10 else ""))
    if ro and k.kind != "orphans":
        parts.append(f"只在 {short(k.right_source)}：" + "、".join(ro[:10]) + ("…" if len(ro) > 10 else ""))
    return "；".join(parts)


# ---------------------------------------------------------------------------------- the visual the user means
@lru_cache(maxsize=1)
def report_visuals():
    from . import dataset, visuals
    cat = catalog_()
    report = cat.report(dataset.NAME)
    return cat, report, visuals.load(report)


def explain_visual(what: str, page: str | None = None, date_str: str | None = None, window: str | None = None,
                   filters: dict | None = None) -> dict:
    """Find the visual the user describes ("紅色的", "北區那根長條", "門市績效") and explain it: what it shows, how
    its measures are computed, its filters, the author's settings, and its numbers over the date range the report
    shows (with which members fall in which colour band of the author's rules)."""
    from . import visuals
    cat, report, infos = report_visuals()
    c = ctx()
    member_cols = {m: f"{c.dims[k].table}[{c.dims[k].column}]"
                   for m, label in member_index().items() for k in c.dims if c.dims[k].label == label}
    dim_cols = {d.label: f"{d.table}[{d.column}]" for d in c.dims.values()}
    found = visuals.locate(infos, f"{what} {page or ''}", page, member_cols, dim_cols)
    if not found:
        raise ToolError("報表上找不到符合的圖表。各頁的圖表：" + "；".join(
            f"{p}：{'、'.join(v.title or v.kind for v in infos if v.page == p)}" for p in dict.fromkeys(v.page for v in infos)))
    top = found[0][0]
    close = [f for f in found if f[0] >= top - 1.5]
    if len(close) > 1:
        return {"結果": "有好幾張圖符合，請使用者指出是哪一張（不要猜）",
                "候選": [f"{v.label}（{v.kind}）：{'；'.join(why) or '部分符合'}" for _, v, why in close[:6]]}
    v = found[0][1]
    out = {"找到的圖表": f"{v.label}（{v.kind}）", "為什麼是這張": found[0][2], **visuals.describe(cat, report, v)}
    out.update(_visual_values(cat, report, v, date_str, window, filters))
    return out


def _visual_values(cat, report, v, date_str, window, filters) -> dict:
    """The visual's numbers over the report's date range (current dataset only), grouped like the visual."""
    from . import visuals
    c = ctx()
    by_col = {f"{d.table}[{d.column}]": k for k, d in c.dims.items()}
    date_cols = {f"{c.date_dim.table}[{c.date_dim.column}]"} | {
        f"{c.date_dim.table}[{col}]" for col in c.model.tables[c.date_dim.table].columns}
    group = [by_col[col] for role in visuals.ROWS for col in v.fields.get(role, []) if col in by_col]
    unmapped = [col for role in visuals.ROWS for col in v.fields.get(role, [])
                if "[" in col and col not in by_col and col not in date_cols]
    on_time_axis = any(col in date_cols for role in visuals.ROWS for col in v.fields.get(role, []))
    dims = tuple(group[-1:])  # the finest grouping of the visual
    scope = {}
    for label, member in (filters or {}).items():
        key = next((k for k, d in c.dims.items() if label in (k, d.label)), None)
        if key is None:
            raise ToolError(f"沒有維度「{label}」，可用：{'、'.join(d.label for d in c.dims.values())}")
        scope[key] = member
    with store.open_db(read_only=True) as db:
        first, last = db.execute("SELECT min(business_date), max(business_date) FROM snapshot_log").fetchone()
    if date_str:
        d1 = clamp_to_data(parse_date(date_str))
        w = window_name(window or "day")
        d0 = max(first, d1 - timedelta(days=visuals.REPORT_SPAN.get(w, 28)))
        period = f"{d0} ～ {d1}（面板選 {d1}、{w}，報表上的日期範圍）"
    else:
        d0, d1 = first, last
        period = f"{d0} ～ {d1}（面板沒有選日期：報表顯示全部期間）"
    out = {"期間": period}
    notes = []
    if v.filters:
        notes.append("這張圖另有篩選（" + "；".join(v.filters) + "），下面的數值沒有套用，可能和畫面不同")
    if on_time_axis:
        notes.append("這張圖的橫軸是日期，下面是整段期間的合計，不是每一天的值")
    if unmapped and not dims:
        notes.append(f"這張圖依 {'、'.join(unmapped)} 分組，但分析設定沒有這個維度，下面只有整體的值，"
                     "沒有每個分組的值（可以在分析設定加上這個維度）")
    with engine_at(d1) as (e, d):
        e.d0 = min(e.d0, d0 - timedelta(days=400))  # room for DATEADD / last-year shifts
        e.cache.clear()
        ev = visuals.RangeEvaluator(cat, report, e)
        values, bands = {}, []
        for m in [re.sub(r"^\w+\((.*)\)$", r"\1", x) for x in v.measures][:5]:
            res = ev.values(m, dims, scope, d0, d1)
            if isinstance(res, str):
                values[m] = f"無法從快照計算：{res}"
                continue
            label = c.dims[dims[0]].label if dims else "整體"
            rows = sorted(((k[0] if k else "整體", x) for k, x in res.items() if x is not None), key=lambda r: r[1])
            shown = rows if len(rows) <= 30 else rows[:10] + rows[-10:]
            values[m] = {f"{label}：{name}": c.fmt(m, x) for name, x in shown}
            if len(rows) > 30:
                values[m]["（說明）"] = f"共 {len(rows)} 個{label}，只列最低與最高各 10 個"
            for em in v.emphasis:
                cases = [cs for cs in em.cases if cs["field"] == m]
                if not cases:
                    continue
                groups: dict[str, list[str]] = {}
                for name, x in rows:
                    cs = visuals.band(cases, x)
                    key = f"{cs['tone'] or cs['result']}（{cs['label']}）" if cs else "不符合任何規則（預設樣式）"
                    groups.setdefault(key, []).append(f"{name} {c.fmt(m, x)}")
                bands.append({"規則": em.text, "目前的分類": {k: "、".join(x[:15]) + (f"…等 {len(x)} 個" if len(x) > 15 else "")
                                                       for k, x in groups.items()}})
    out["數值（依報表的日期範圍由快照計算）"] = values
    if bands:
        out["依作者規則的顏色"] = bands
    if scope:
        out["套用的面板篩選"] = {c.dims[k].label: m for k, m in scope.items()}
    if notes:
        out["注意"] = notes
    return out
