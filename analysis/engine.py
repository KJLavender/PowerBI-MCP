"""Deterministic change analysis on the daily snapshots.

For a business date D the engine
  1. checks data quality (ETL status / row counts / units that suddenly lost data),
  2. measures the report's metrics for the total, every dimension member and every configured
     combination, over three windows:
        day    D vs the same weekday of the previous 4 weeks
        week   last 7 days vs the 7 days before
        month  last 28 days vs the 28 days before
     week/month changes are seasonally adjusted with the same change one year earlier (364 days,
     weekday-aligned), so "September is always quieter than August" is not reported,
  3. flags changes that are extreme for that series' own history (robust z-score),
  4. keeps the scope that actually explains a change:
        - specific vs broad: if the rest of the parent moves the same way, the effect is broad
          ("飲料 everywhere", not "北區 × 飲料"); if the rest is flat, the specific scope explains
          the parent ("北區 × 3C配件" explains "3C配件" and "北區"),
        - cross-dimension: "北區 discount up" is dropped when it comes entirely from "官網",
  5. decomposes total-level changes (member contributions, mix vs rate, model drivers).
Every number in the output is computed here; nothing is generated.
"""
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from . import store
from .context import Context

WEEKDAY_LAGS = (7, 14, 21, 28)
YEAR = 364
BROAD = 0.5   # complement moving ≥ 50% as much as the scope itself ⇒ effect is not specific

warnings.filterwarnings("ignore", message="Mean of empty slice")


def div(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b != 0, a / np.where(b != 0, b, 1), np.nan)


def evaluate(expr: str, bases: dict) -> np.ndarray:
    return np.asarray(eval(expr, {"div": div, "__builtins__": {}}, bases), dtype=float)  # noqa: S307


def shift(arr: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    if k < len(arr):
        out[k:] = arr[:-k]
    return out


def windowed(bases: dict[str, np.ndarray], window: str, days: int, mode: str = "pop"):
    """(current, baseline) base sums for every end date.
    pop: previous period of the same length; yoy: same period one year (364 days) earlier."""
    if window == "day":
        base = {b: np.nanmean([shift(a, k) for k in WEEKDAY_LAGS], axis=0) for b, a in bases.items()}
        return bases, base
    cur = {}
    for b, a in bases.items():
        cs = np.cumsum(a, axis=0)
        out = cs.astype(float).copy()
        out[days:] = cs[days:] - cs[:-days]
        out[:days - 1] = np.nan
        cur[b] = out
    lag = YEAR if mode == "yoy" else days
    return cur, {b: shift(a, lag) for b, a in cur.items()}


# ----------------------------------------------------------------------------------------
@dataclass
class Cube:
    dims: tuple[str, ...]
    members: list[tuple]
    dates: list[date]
    data: dict[str, np.ndarray]  # base -> [date, member]

    def column(self, member: tuple) -> dict[str, np.ndarray] | None:
        try:
            i = self.members.index(member)
        except ValueError:
            return None
        return {b: a[:, i:i + 1] for b, a in self.data.items()}


def load_cube(db, dims: tuple[str, ...], bases: list[str], d0: date, d1: date, table: str) -> Cube:
    group = ", ".join(("business_date",) + dims)
    sums = ", ".join(f'SUM("{b}") AS "{b}"' for b in bases)
    df = db.execute(f"SELECT {group}, {sums} FROM {table} WHERE business_date BETWEEN ? AND ? "
                    f"GROUP BY {group}", [d0, d1]).df()
    dates = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    df["business_date"] = [x.date() if hasattr(x, "date") else x for x in df["business_date"]]
    df["_m"] = list(zip(*[df[d] for d in dims])) if dims else [()] * len(df)
    members = sorted(set(df["_m"])) if len(df) else [()]
    mi, di = {m: i for i, m in enumerate(members)}, {d: i for i, d in enumerate(dates)}
    rows, cols = df["business_date"].map(di).to_numpy(), df["_m"].map(mi).to_numpy()
    data = {}
    for b in bases:
        arr = np.zeros((len(dates), len(members)))
        arr[rows, cols] = df[b].to_numpy(dtype=float)
        data[b] = arr
    return Cube(dims, members, dates, data)


@dataclass
class Stat:
    metric: str
    window: str
    current: float
    baseline: float
    change: float           # pct change (or absolute change for percentage metrics)
    expected: float | None  # same change one year earlier (seasonal expectation)
    z: float
    kind: str               # "pct" | "abs"
    significant: bool = False
    note: str = ""


@dataclass
class Finding:
    scope: dict[str, str]
    window: str
    stats: list[Stat]
    share: float
    notes: list[str] = field(default_factory=list)
    explains: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    concentrated_in: dict | None = None  # the change sits mostly inside one member of another dimension
    new: bool = True          # False: the same change was already reported within repeat_days
    since: date | None = None  # first report date of an ongoing change

    def signatures(self) -> list[tuple]:
        return [(key(self.scope), s.window, s.metric, int(np.sign(s.change))) for s in self.significant]

    @property
    def significant(self) -> list[Stat]:
        return [s for s in self.stats if s.significant]

    @property
    def score(self) -> float:
        z = max((abs(s.z) for s in self.significant), default=0.0)
        holiday = 0.5 if any("節日" in n for n in self.notes) else 1.0  # calendar effects rank lower
        return z * max(self.share, 1e-3) ** 0.25 * holiday


def key(scope: dict) -> tuple:
    return tuple(sorted(scope.items()))


class Engine:
    def __init__(self, ctx: Context, db=None):
        self.ctx = ctx
        self.db = db or store.open_db(read_only=True)
        self.order_dims = set(store.order_dims(self.db))
        self.th = ctx.config["thresholds"]
        self.windows = ctx.config["windows"]
        used = [m for m in ctx.model.used_measures() if m in ctx.formulas]
        self.metrics = used or list(ctx.formulas)   # what the report shows
        self.cache: dict[tuple, Cube] = {}
        # signature -> (first reported, last reported); persisted by the report module
        self.reported: dict[tuple, tuple[date, date]] = {}

    # ---------------- data access ----------------
    def cube(self, dims: tuple[str, ...]) -> Cube:
        if dims not in self.cache:
            table = "order_snap" if set(dims) <= self.order_dims else "line_snap"
            bases = [b for b in self.ctx.bases if table == "order_snap" or not self.ctx.bases[b].distinct_of]
            self.cache[dims] = load_cube(self.db, dims, bases, self.d0, self.d1, table)
        return self.cache[dims]

    def groupings(self) -> list[tuple[str, ...]]:
        return ([()] + [(k,) for k in self.ctx.dims]
                + [tuple(c) for c in self.ctx.config.get("combinations", [])])

    def bases_of(self, scope: dict) -> dict | None:
        dims = next((g for g in self.groupings() if set(g) == set(scope)), None)
        if dims is None:
            dims = tuple(scope)
        return self.cube(dims).column(tuple(scope[k] for k in dims))

    def complement(self, outer: dict, inner: dict) -> dict | None:
        o, i = self.bases_of(outer), self.bases_of(inner)
        if o is None or i is None:
            return None
        return {b: o[b] - i[b] for b in o if b in i}

    # ---------------- statistics ----------------
    def kind(self, metric: str) -> str:
        return "abs" if "%" in self.ctx.model.measures[metric].format_string else "pct"

    def allowed(self, metric: str, dims) -> bool:
        needs_distinct = any(self.ctx.bases[b].distinct_of for b in self.ctx.formulas[metric].bases)
        return not needs_distinct or set(dims) <= self.order_dims

    def changes(self, bases: dict, metric: str, window: str, member: bool = True):
        """cur, base, raw change, expected change, adjusted change (all series over dates).
        day:  vs same weekday of previous 4 weeks, no adjustment
        pop:  vs previous period, minus the same change one year earlier (seasonality)
        yoy:  vs same period last year, relative to the total's YoY (members only)"""
        f = self.ctx.formulas[metric]
        if any(b not in bases for b in f.bases):
            return None
        mode = self.windows[window].get("mode", "pop")
        cur_b, base_b = windowed(bases, window, self.windows[window]["days"], mode)
        cur, base = evaluate(f.expr, cur_b)[:, 0], evaluate(f.expr, base_b)[:, 0]
        abs_kind = self.kind(metric) == "abs"
        with np.errstate(divide="ignore", invalid="ignore"):
            chg = cur - base if abs_kind else np.where(base > 0, cur / base - 1, np.nan)
        if window == "day":
            return cur, base, chg, None, chg
        if mode == "yoy":
            if not member:
                return cur, base, chg, None, chg
            ref_cur, ref_base = self.like_for_like(window)
            rc, rb = evaluate(f.expr, ref_cur)[:, 0], evaluate(f.expr, ref_base)[:, 0]
            with np.errstate(divide="ignore", invalid="ignore"):
                ly = rc - rb if abs_kind else np.where(rb > 0, rc / rb - 1, np.nan)
        else:
            ly = shift(chg, YEAR)
        with np.errstate(divide="ignore", invalid="ignore"):
            adj = chg - ly if self.kind(metric) == "abs" else (1 + chg) / (1 + ly) - 1
        adj = np.where(np.isnan(ly), chg, adj)  # no history a year ago: fall back to raw change
        return cur, base, chg, ly, adj

    def z_threshold(self, scope_dims) -> float:
        return self.th["z_alert"] if not scope_dims else self.th["z_alert_member"]

    def like_for_like(self, window: str) -> tuple[dict, dict]:
        """Windowed (current, comparison) totals over units present in BOTH periods.

        Units come from the window's like_for_like dimension (e.g. store). Without it the plain
        total is used. New stores therefore don't make every existing store look like it shrank."""
        cache_key = ("lfl", window)
        if cache_key not in self.cache:
            unit = self.windows[window].get("like_for_like")
            days, mode = self.windows[window]["days"], self.windows[window].get("mode", "pop")
            cube = self.cube((unit,) if unit else ())
            cur, base = windowed(cube.data, window, days, mode)
            ok = (np.nan_to_num(cur["rows"]) > 0) & (np.nan_to_num(base["rows"]) > 0)
            self.cache[cache_key] = (
                {b: np.where(ok, np.nan_to_num(a), 0).sum(axis=1, keepdims=True) for b, a in cur.items()},
                {b: np.where(ok, np.nan_to_num(a), 0).sum(axis=1, keepdims=True) for b, a in base.items()},
            )
        return self.cache[cache_key]

    def stat(self, bases: dict, metric: str, window: str, z_th: float | None = None,
             member: bool = True) -> Stat | None:
        out = self.changes(bases, metric, window, member)
        if out is None:
            return None
        cur, base, chg, ly, adj = out
        days = self.windows[window]["days"]
        t = len(cur) - 1
        lo, hi = t - self.th["history_days"], t - (1 if window == "day" else days)
        hist = adj[max(lo, 0):max(hi, 0) + 1]
        hist = hist[~np.isnan(hist)]
        if np.isnan(adj[t]) or len(hist) < 20:
            return None
        kind = self.kind(metric)
        med = np.median(hist)
        scale = max(1.4826 * np.median(np.abs(hist - med)), 0.003 if kind == "abs" else 0.02)
        z = (adj[t] - med) / scale
        min_move = self.th["min_ratio_change"] if kind == "abs" else self.th["min_change_pct"]
        expected = None if ly is None or np.isnan(ly[t]) else float(ly[t])
        s = Stat(metric, window, float(cur[t]), float(base[t]), float(chg[t]), expected, float(z), kind)
        s.significant = abs(z) >= (z_th or self.th["z_alert"]) and abs(adj[t]) >= min_move
        return s

    def share(self, bases: dict, window: str) -> float:
        days = self.windows[window]["days"]
        total = self.cube(()).data["rows"][-days:].sum()
        return float(bases["rows"][-days:].sum() / total) if total else 0.0

    def label(self, scope: dict) -> str:
        return " × ".join(scope.values()) if scope else "整體"

    # ---------------- pipeline ----------------
    def prepare(self, d: date) -> None:
        """Position the engine on business date d (cubes end at d)."""
        if getattr(self, "d1", None) != d:
            self.d1 = d
            self.d0 = d - timedelta(days=self.th["history_days"] + YEAR + 2 * 28 + 7)
            self.cache.clear()

    def run(self, d: date) -> dict:
        self.prepare(d)
        dq = self.data_quality(d)
        table = self.scan()
        findings = []
        for window in self.windows:
            findings += self.refine({k: f for (k, w), f in table.items() if w == window}, window)
        best: dict[tuple, Finding] = {}
        for f in findings:
            if f.significant:
                self.annotate(f, d, dq)
                k = key(f.scope)
                if k not in best or f.score > best[k].score:
                    best[k] = f
        horizon = d - timedelta(days=self.th["repeat_days"])
        for f in best.values():
            seen = [self.reported[s] for s in f.signatures()
                    if s in self.reported and horizon <= self.reported[s][1] < d]
            f.new = len(seen) < len(f.signatures())
            f.since = min((first for first, _ in seen), default=None)
        kept = sorted(best.values(), key=lambda f: (bool(f.scope), not f.new, -f.score))[:self.th["max_findings"]]
        for f in kept:
            f.evidence = self.decompose(f) if not f.scope else self.profile(f)
            for s in f.signatures():
                first, last = self.reported.get(s, (d, d))
                self.reported[s] = (first if horizon <= last < d else d, d)
        return {"date": d, "kpis": self.kpis(), "data_quality": dq, "findings": kept,
                "metrics": self.metrics}

    def scan(self) -> dict[tuple, Finding]:
        table = {}
        for dims in self.groupings():
            cube = self.cube(dims)
            for member in cube.members:
                bases = cube.column(member)
                scope = dict(zip(dims, member))
                for window in self.windows:
                    share = self.share(bases, window) if dims else 1.0
                    if dims and share < self.th["min_share"]:
                        continue
                    stats = [s for m in self.metrics if self.allowed(m, dims)
                             if (s := self.stat(bases, m, window, self.z_threshold(dims), member=bool(dims)))]
                    table[(key(scope), window)] = Finding(scope, window, stats, share)
        return table

    def parents(self, scope: dict) -> list[dict]:
        """Broader scopes containing this one: drop one dimension, or go up the hierarchy."""
        if len(scope) > 1:
            return [{k2: v for k2, v in scope.items() if k2 != k} for k in scope]
        (k, v), = scope.items()
        parent = self.ctx.dims[k].parent
        if not parent:
            return []
        cube = self.cube((parent, k))
        return [{parent: m[0]} for m in cube.members if m[1] == v][:1]

    def siblings(self, scope: dict, parent: dict) -> list[dict]:
        """Other members under the same parent (e.g. other products of the category,
        or 南區 × 3C配件 next to 北區 × 3C配件 under 3C配件)."""
        dims = tuple(parent) + tuple(k for k in scope if k not in parent)
        cube = self.cube(dims)
        pv = tuple(parent.values())
        out = []
        for m in cube.members:
            if m[:len(pv)] == pv:
                member = dict(zip(dims, m))
                child = {k: member[k] for k in scope}
                if child != scope:
                    out.append(cube.column(m))
        return out

    def breadth(self, scope: dict, parent: dict, metric: str, window: str, own: float) -> float:
        """Row-weighted share of siblings moving the same way by at least half as much."""
        days = self.windows[window]["days"]
        moved = total = 0.0
        for bases in self.siblings(scope, parent):
            out = self.changes(bases, metric, window)
            if out is None:
                continue
            adj = out[4][-1]
            w = float(bases["rows"][-days:].sum())
            total += w
            if not np.isnan(adj) and np.sign(adj) == np.sign(own) and abs(adj) >= BROAD * abs(own):
                moved += w
        return moved / total if total else 0.0

    def refine(self, table: dict[tuple, Finding], window: str) -> list[Finding]:
        def get(scope, metric):
            f = table.get(key(scope))
            return next((s for s in f.stats if s.metric == metric), None) if f else None

        def ratio(outer: dict, inner: dict, metric: str, change: float) -> float | None:
            rest = self.complement(outer, inner)
            s = self.stat(rest, metric, window) if rest else None
            if s is None or change == 0:
                return None
            adj = s.change if s.expected is None else s.change - s.expected
            return adj / change

        explained: set[tuple] = set()  # (scope key, metric) explained by a more specific scope
        specific_first = sorted(table.values(), key=lambda f: -(len(f.scope) + sum(
            1 for k in f.scope if self.ctx.dims[k].parent)))
        # 1) specific vs broad, along parents (combinations and hierarchies)
        for f in specific_first:
            if not f.scope:
                continue
            for s in f.significant:
                own = s.change if s.expected is None else s.change - s.expected
                for p in self.parents(f.scope):
                    r = ratio(p, f.scope, s.metric, own)
                    if r is None:
                        continue
                    ps = get(p, s.metric)
                    broad = r >= BROAD or self.breadth(f.scope, p, s.metric, window, own) >= BROAD
                    if broad:
                        s.significant = False
                        if (ps and (key(p), s.metric) not in explained and np.sign(ps.change) == np.sign(s.change)
                                and abs(ps.z) >= 2 / 3 * self.z_threshold(p)):
                            ps.significant = True  # promote the broader scope
                        break
                    if ps and np.sign(ps.change) == np.sign(s.change):
                        explained.add((key(p), s.metric))
                        if ps.significant:
                            ps.significant = False
                            f.explains.append(self.label(p))
        # 2) cross-dimension: a single member's change that comes entirely from another dimension's member
        singles = [f for f in table.values() if len(f.scope) == 1]
        for a in singles:
            for s in a.significant:
                own = s.change if s.expected is None else s.change - s.expected
                for b in singles:
                    if b is a or set(b.scope) == set(a.scope):
                        continue
                    bs = next((x for x in b.significant if x.metric == s.metric), None)
                    if not bs or np.sign(bs.change) != np.sign(s.change):
                        continue
                    r = ratio(a.scope, {**a.scope, **b.scope}, s.metric, own)
                    if r is not None and abs(r) < BROAD:
                        s.significant = False
                        if self.label(a.scope) not in b.explains:
                            b.explains.append(self.label(a.scope))
                        break
        return list(table.values())

    def annotate(self, f: Finding, d: date, dq: list[dict]) -> None:
        days = self.windows[f.window]["days"]
        lag = YEAR if self.windows[f.window].get("mode") == "yoy" else days
        cal = dict(self.db.execute("SELECT business_date, holiday FROM calendar WHERE business_date BETWEEN ? AND ?",
                                   [d - timedelta(days=lag + days + 28), d]).fetchall())
        cur_days = [d - timedelta(days=i) for i in range(days)]
        base_days = ([d - timedelta(days=k) for k in WEEKDAY_LAGS] if f.window == "day"
                     else [d - timedelta(days=lag + i) for i in range(days)])
        for label, ds in (("期間", cur_days), ("比較基準", base_days)):
            hol = sorted({cal[x] for x in ds if cal.get(x)})
            if hol:
                f.notes.append(f"{label}包含節日：{'、'.join(hol)}")
        unit = self.ctx.config["data_quality"]["unit"]
        for issue in dq:
            if f.window == "day" and issue.get("unit") and f.scope.get(unit, issue["unit"]) == issue["unit"]:
                f.notes.append(f"資料品質：{issue['message']}")
        # only additive metrics: for ratios, "the rest" is distorted by mix and the test is unreliable
        additive = [s for s in f.significant if not self.ctx.formulas[s.metric].is_ratio]
        if len(f.scope) == 1 and additive:
            f.concentrated_in = self.concentration(f, additive[0])
        if f.window != "day" and unit not in f.scope:
            opened, closed = self.unit_changes(f.scope, f.window, unit)
            label = self.ctx.dims[unit].label
            if opened:
                f.notes.append(f"範圍內有比較期間尚未營業的{label}：{'、'.join(opened)}（比較基準中沒有它們的資料）")
            if closed:
                f.notes.append(f"範圍內有本期沒有資料的{label}：{'、'.join(closed)}")

    def concentration(self, f: Finding, s: Stat) -> dict | None:
        """Does this change come mostly from one member of another dimension?
        (e.g. 美妝保養 up ← almost all of it inside 美妝保養 × 官網)."""
        own = s.change if s.expected is None else s.change - s.expected
        if own == 0:
            return None
        best = None
        for k in self.ctx.dims:
            # skip the scope's own hierarchy and very fine dimensions (too many members to be a useful "why")
            if k in f.scope or self.ctx.dims[k].parent in f.scope or len(self.cube((k,)).members) > 30:
                continue
            cube = self.cube(tuple(f.scope) + (k,))
            values = tuple(f.scope.values())
            for m in cube.members:
                if m[:len(values)] != values:
                    continue
                inner = {**f.scope, k: m[-1]}
                rest = self.complement(f.scope, inner)
                rs = self.stat(rest, s.metric, f.window) if rest else None
                ins = self.stat(self.bases_of(inner), s.metric, f.window)
                if rs is None or ins is None:
                    continue
                r = (rs.change if rs.expected is None else rs.change - rs.expected) / own
                if abs(r) >= BROAD:
                    continue
                # prefer the most specific explanation: much of the change inside a small slice
                weight = self.share(self.bases_of(inner), f.window) / max(f.share, 1e-9)
                density = (1 - abs(r)) / max(weight, 1e-3)
                if best is None or density > best["density"]:
                    best = {"dimension": k, "member": m[-1], "rest_ratio": float(r), "metric": s.metric,
                            "inside_change": ins.change, "rest_change": rs.change, "kind": s.kind,
                            "weight": float(weight), "density": float(density)}
        return best if best and best["weight"] < 0.5 else None

    def unit_changes(self, scope: dict, window: str, unit: str) -> tuple[list[str], list[str]]:
        """Units (e.g. stores) inside the scope that exist in only one of the two compared periods."""
        dims = tuple(scope) + (unit,)
        cube = self.cube(dims)
        days, mode = self.windows[window]["days"], self.windows[window].get("mode", "pop")
        cur, base = windowed({"rows": cube.data["rows"]}, window, days, mode)
        c, b = np.nan_to_num(cur["rows"][-1]), np.nan_to_num(base["rows"][-1])
        values = tuple(scope.values())
        opened, closed = [], []
        for i, m in enumerate(cube.members):
            if m[:len(values)] != values:
                continue
            if c[i] > 0 and b[i] == 0:
                opened.append(m[-1])
            elif b[i] > 0 and c[i] == 0:
                closed.append(m[-1])
        return opened, closed

    # ---------------- data quality ----------------
    def data_quality(self, d: date) -> list[dict]:
        out = []
        etl = self.db.execute("SELECT etl_rows, etl_status FROM etl_log WHERE business_date = ?", [d]).fetchone()
        fact = self.db.execute("SELECT fact_rows FROM snapshot_log WHERE business_date = ?", [d]).fetchone()
        if not etl:
            out.append({"type": "etl_missing", "message": "找不到當日 ETL 批次紀錄"})
        else:
            rows, status = etl
            if status != "SUCCESS":
                out.append({"type": "etl_status", "message": f"ETL 狀態為 {status}"})
            if fact and int(fact[0]) != int(rows):
                out.append({"type": "row_mismatch", "message": f"ETL 回報 {rows:,} 筆，實際明細 {int(fact[0]):,} 筆"})
        unit = self.ctx.config["data_quality"]["unit"]
        cube = self.cube((unit,))
        rows = cube.data["rows"]
        t = len(cube.dates) - 1
        med = np.median(np.array([rows[t - k] for k in WEEKDAY_LAGS]), axis=0)
        for i, m in enumerate(cube.members):
            if med[i] <= 0:
                continue
            r = rows[t, i] / med[i]
            if r < self.th["dq_store_ratio"]:
                out.append({"type": "unit_drop", "unit": m[0], "ratio": float(r),
                            "message": (f"{self.ctx.dims[unit].label}「{m[0]}」當日明細 {int(rows[t, i]):,} 筆，"
                                        f"僅為近 4 週同星期中位數 {int(med[i]):,} 筆的 {r:.0%}，疑似資料缺漏")})
        return out

    # ---------------- explanation ----------------
    def kpis(self) -> list[dict]:
        bases = self.cube(()).column(())
        out = []
        for m in self.metrics:
            if not self.allowed(m, ()):
                continue
            f = self.ctx.formulas[m]
            day = evaluate(f.expr, bases)[:, 0]
            cur7, prev7 = windowed(bases, "week", 7)
            w7, p7 = evaluate(f.expr, cur7)[-1, 0], evaluate(f.expr, prev7)[-1, 0]
            _, base4 = windowed(bases, "day", 1)
            avg4 = evaluate(f.expr, base4)[-1, 0]
            kind = self.kind(m)
            change = (lambda a, b: a - b) if kind == "abs" else (lambda a, b: a / b - 1 if b else float("nan"))
            out.append({"metric": m, "value": float(day[-1]), "kind": kind,
                        "vs_last_week": float(change(day[-1], day[-8])), "vs_4wk_avg": float(change(day[-1], avg4)),
                        "week": float(w7), "week_vs_prev": float(change(w7, p7))})
        return out

    def decompose(self, f: Finding) -> dict:
        """Where a total-level change came from: by every dimension, plus model drivers."""
        result = {}
        days = self.windows[f.window]["days"]
        for s in f.significant:
            formula = self.ctx.formulas[s.metric]
            per_dim = {}
            for k in self.ctx.dims:
                if not self.allowed(s.metric, (k,)):
                    continue
                cube = self.cube((k,))
                cur_b, base_b = windowed(cube.data, f.window, days, self.windows[f.window].get("mode", "pop"))
                cur, base = {b: a[-1] for b, a in cur_b.items()}, {b: a[-1] for b, a in base_b.items()}
                names = [m[0] for m in cube.members]
                if not formula.is_ratio:
                    c, b = evaluate(formula.expr, cur), evaluate(formula.expr, base)
                    delta = c - b
                    total, spread = np.nansum(delta), np.nansum(np.abs(delta))
                    top = sorted(zip(names, c, b, delta), key=lambda r: -abs(r[3]))[:3]
                    per_dim[k] = {"type": "contribution", "concentration": float(abs(top[0][3]) / spread) if spread else 0,
                                  "total_delta": float(total),
                                  "top": [{"member": n, "current": float(x), "baseline": float(y), "delta": float(dl),
                                           "share_of_change": float(dl / total) if total else None}
                                          for n, x, y, dl in top]}
                elif formula.numerator and formula.denominator:
                    n1, n0 = evaluate(formula.numerator, cur), evaluate(formula.numerator, base)
                    d1, d0 = evaluate(formula.denominator, cur), evaluate(formula.denominator, base)
                    w1, w0 = d1 / d1.sum(), d0 / d0.sum()
                    r1, r0 = div(n1, d1), div(n0, d0)
                    mix = np.nan_to_num((w1 - w0) * (r0 - n0.sum() / d0.sum()))
                    rate = np.nan_to_num(w1 * (r1 - r0))
                    top = sorted(zip(names, r1, r0, rate, mix), key=lambda r: -abs(r[3]) - abs(r[4]))[:3]
                    spread = np.abs(rate).sum() + np.abs(mix).sum()
                    per_dim[k] = {"type": "mix_rate", "mix_effect": float(mix.sum()), "rate_effect": float(rate.sum()),
                                  "concentration": float((abs(top[0][3]) + abs(top[0][4])) / spread) if spread else 0,
                                  "top": [{"member": n, "current": float(a), "baseline": float(b),
                                           "rate_effect": float(re), "mix_effect": float(me)}
                                          for n, a, b, re, me in top]}
            best = sorted(per_dim.items(), key=lambda kv: -kv[1]["concentration"])[:2]
            drivers = []
            for y, r in self.ctx.drivers(s.metric):
                total = self.cube(()).column(())
                ys, rs = self.stat(total, y, f.window, member=False), self.stat(total, r, f.window, member=False)
                if ys and rs:
                    drivers.append({"identity": f"{s.metric} = {y} × {r}", y: ys.change, r: rs.change})
            result[s.metric] = {"by_dimension": dict(best), "drivers": drivers}
        return result

    def profile(self, f: Finding) -> dict:
        """Volume / price / discount / margin pattern of a scope, from its own metric changes."""
        moves = {s.metric: s.change for s in f.stats}
        tags = []
        for metrics, up, down, th in ((("銷售數量", "訂單數"), "量增", "量減", self.th["min_change_pct"]),
                                      (("平均成交單價", "客單價"), "價升", "價降", self.th["min_change_pct"]),
                                      (("折扣率",), "折扣加深", "折扣減少", self.th["min_ratio_change"]),
                                      (("毛利率",), "毛利率上升", "毛利率下降", self.th["min_ratio_change"])):
            m = next((x for x in metrics if x in moves), None)
            if m and abs(moves[m]) >= th:
                tags.append(up if moves[m] > 0 else down)
        return {"pattern": tags}
