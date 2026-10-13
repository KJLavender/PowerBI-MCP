"""Score the engine against the injected scenarios (ground truth).

This is the ONLY analysis module allowed to read the scenario file (scenarios/scenarios.yaml for the
retail simulator, datasets/<name>/scenarios.yaml otherwise). For every business date in a
range it runs the engine exactly as the daily job would and checks:
  - recall: was each scenario reported, and how many days after it started (latency)?
  - precision: how many findings per day match no scenario and are not explained by a holiday?
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

import yaml

from simulator import scenarios as sim_scen

from . import dataset
from .context import Context, load
from .engine import Engine, Finding


def load_scenarios(ctx: Context) -> tuple[list, dict[str, dict[str, str]]]:
    """Scenarios and code → name maps (the retail simulator filters on store/product codes)."""
    if dataset.NAME == "retail":
        from simulator import dimensions as sim_dims
        names = {"store": {s.code: s.name for s in sim_dims.stores()},
                 "product": {p.code: p.name for p in sim_dims.products()}}
        return sim_scen.load(), names
    path = dataset.HOME / "scenarios.yaml"
    scenarios = sim_scen.load(path, set(ctx.dims))
    flags = {x["id"]: x.get("detectable", True) for x in yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]}
    for sc in scenarios:
        sc.detectable = flags.get(sc.id, True)  # False: deliberately smaller than the data's noise
    return scenarios, {}


def expected_moves(ctx: Context, effects: dict) -> list[tuple[str, int]]:
    """Metric/direction pairs a scenario should produce."""
    r = ctx.roles
    out = []
    v = effects.get("volume", 1.0)
    if v != 1.0:
        sign = 1 if v > 1 else -1
        out += [(r["sales"], sign), (r["volume"], sign), (r["orders"], sign)]
    p = effects.get("price", 1.0)
    if p != 1.0:
        sign = 1 if p > 1 else -1
        out += [(r["unit_price"], sign), (r["margin_rate"], sign)]
    if effects.get("cost", 1.0) != 1.0:
        out.append((r["margin_rate"], -1 if effects["cost"] > 1 else 1))
    if effects.get("extra_discount", 0):
        out += [(r["discount_rate"], 1), (r["margin_rate"], -1)]
    if effects.get("missing_rate", 0):
        out += [(r["sales"], -1), (r["orders"], -1), (r["volume"], -1)]
    return out


@dataclass
class Match:
    scenario: str
    day: date
    quality: str     # exact | partial | data_quality
    text: str


def match(ctx: Context, f: Finding, scen, names) -> str | None:
    want = {k: {names.get(k, {}).get(v, v) for v in vals} for k, vals in scen.filter.items()}
    if not f.scope or any(k in want and v not in want[k] for k, v in f.scope.items()):
        return None
    overlap = set(f.scope) & set(want)
    if not overlap:
        return None
    moves = expected_moves(ctx, scen.effects)
    if not any((s.metric, 1 if s.change > 0 else -1) in moves for s in f.significant):
        return None
    return "exact" if overlap == set(want) and set(f.scope) == set(want) else "partial"


def describe(f: Finding, label) -> str:
    stats = ", ".join(f"{s.metric} {s.change * 100:+.1f} 個百分點" if s.kind == "abs" else f"{s.metric} {s.change:+.1%}"
                      for s in f.significant)
    return f"[{f.window}] {label(f.scope)}: {stats}"


def run(d0: date, d1: date, verbose: bool = False) -> dict:
    ctx = load()
    engine = Engine(ctx)
    scenarios, names = load_scenarios(ctx)
    first: dict[str, Match] = {}
    first_exact: dict[str, date] = {}
    fp_days, fp_scopes, total_findings = [], Counter(), 0
    d = d0
    while d <= d1:
        result = engine.run(d)
        unmatched = 0
        active = [s for s in scenarios if s.start <= d <= (s.end or d1) + timedelta(days=35)]
        for issue in result["data_quality"]:
            for s in active:
                code = next(iter(s.filter.get("store", [])), None)
                if issue.get("unit") and code and names.get("store", {}).get(code) == issue["unit"] and s.id not in first:
                    first[s.id] = Match(s.id, d, "data_quality", issue["message"])
        for f in result["findings"]:
            if not f.scope or not f.new:
                continue
            total_findings += 1
            hits = [(s, q) for s in active if (q := match(ctx, f, s, names))]
            for s, q in hits:
                prev = first.get(s.id)
                if prev is None or (prev.quality == "partial" and q == "exact" and d == prev.day):
                    first[s.id] = Match(s.id, d, q, describe(f, engine.label))
                if q == "exact" and s.id not in first_exact:
                    first_exact[s.id] = d
            if not hits and not any("節日" in n or "資料品質" in n for n in f.notes):
                unmatched += 1
                fp_scopes[engine.label(f.scope)] += 1
        fp_days.append(unmatched)
        if verbose:
            print(f"  {d}: {len(result['findings'])} findings, {unmatched} unexplained", flush=True)
        d += timedelta(days=1)

    rows = []
    for s in scenarios:
        if s.start > d1 or (s.end or d1) < d0:
            continue
        m = first.get(s.id)
        rows.append({"id": s.id, "description": s.description, "start": s.start,
                     "detectable": getattr(s, "detectable", True),
                     "detected": m.day if m else None, "latency": (m.day - s.start).days if m else None,
                     "quality": m.quality if m else None, "evidence": m.text if m else "",
                     "exact_latency": (first_exact[s.id] - s.start).days if s.id in first_exact else None})
    return {"range": (d0, d1), "scenarios": rows,
            "unexplained_per_day": sum(fp_days) / len(fp_days), "findings_per_day": total_findings / len(fp_days),
            "top_unexplained": fp_scopes.most_common(8)}


def render(r: dict) -> str:
    d0, d1 = r["range"]
    lines = [f"# 評分結果 {d0} ～ {d1}", "",
             "| 情境 | 開始 | 偵測到 | 延遲 | 比對 | 完全比對延遲 | 證據 |", "|---|---|---|---|---|---|---|"]
    for s in r["scenarios"]:
        det = s["detected"] or "**未偵測**"
        lat = f"{s['latency']} 天" if s["latency"] is not None else "—"
        exact = f"{s['exact_latency']} 天" if s["exact_latency"] is not None else "—"
        name = s["id"] if s["detectable"] else f"{s['id']}（刻意小於雜訊）"
        lines.append(f"| {name} | {s['start']} | {det} | {lat} | {s['quality'] or '—'} | {exact} | {s['evidence']} |")
    target = [s for s in r["scenarios"] if s["detectable"]]
    found = sum(1 for s in target if s["detected"])
    lines += ["", f"- 偵出率：{found}/{len(target)}" + (f"（另有 {len(r['scenarios']) - len(target)} 個刻意小於雜訊的情境不計入）"
                                                    if len(target) < len(r["scenarios"]) else ""),
              f"- 每日新發現數（不含整體、不含持續中）：{r['findings_per_day']:.1f}",
              f"- 其中無法對應情境、也非節日 / 資料品質的：{r['unexplained_per_day']:.1f}",
              "- 最常出現的未對應範圍：" + "、".join(f"{k}（{v}）" for k, v in r["top_unexplained"])]
    return "\n".join(lines)


__all__ = ["run", "render"]
