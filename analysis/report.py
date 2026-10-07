"""Render the engine result as a Markdown report (for people) and JSON (for Phase 3 agents).

Sentences are fixed templates filled with computed numbers; there is no free-text generation.
"""
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .context import DATA_DIR, Context
from .engine import Engine, Finding, Stat
from .semantic import ROOT

REPORT_DIR = ROOT / "reports"
WEEKDAYS = "一二三四五六日"


def change_text(kind: str, value: float | None) -> str:
    if value is None or value != value:
        return "—"
    return f"{value * 100:+.1f} 個百分點" if kind == "abs" else f"{value:+.1%}"


def run(ctx: Context, d: date, engine: Engine | None = None) -> dict:
    """Run the engine for d, warming up the 'already reported' memory on the preceding days."""
    engine = engine or Engine(ctx)
    if not engine.reported:
        for i in range(ctx.config["thresholds"]["repeat_days"], 0, -1):
            engine.run(d - timedelta(days=i))
    result = engine.run(d)
    result["engine"] = engine
    return result


class Writer:
    def __init__(self, ctx: Context, result: dict):
        self.ctx, self.r, self.e = ctx, result, result["engine"]
        self.windows = ctx.config["windows"]
        self.lines: list[str] = []

    def w(self, *lines: str) -> None:
        self.lines.extend(lines)

    def stat_row(self, s: Stat) -> str:
        expected = change_text(s.kind, s.expected) if s.expected is not None else "—"
        flag = " **⬤**" if s.significant else ""
        return (f"| {s.metric}{flag} | {self.ctx.fmt(s.metric, s.current)} | {self.ctx.fmt(s.metric, s.baseline)} | "
                f"{change_text(s.kind, s.change)} | {expected} | {s.z:+.1f} |")

    def path_text(self, f: Finding) -> str:
        rels = []
        for k in f.scope:
            for r in self.ctx.dims[k].path:
                if str(r) not in rels:
                    rels.append(str(r))
        cols = "、".join(f"{self.ctx.dims[k].table}[{self.ctx.dims[k].column}] = {v}" for k, v in f.scope.items())
        return f"{cols}（關聯：{'；'.join(rels)}）"

    def headline(self, f: Finding) -> str:
        ref = "整體同店" if self.windows[f.window].get("mode") == "yoy" else "去年同期"
        parts = [f"{s.metric} {change_text(s.kind, s.change)}"
                 + (f"（{ref} {change_text(s.kind, s.expected)}）" if s.expected is not None else "")
                 for s in f.significant]
        return f"{self.e.label(f.scope)}：{'、'.join(parts)}（{self.windows[f.window]['label']}）"

    def finding(self, n: int, f: Finding) -> None:
        self.w(f"### {n}. {self.headline(f)}", "")
        mode = self.windows[f.window].get("mode")
        base_label = {"day": "近 4 週同星期平均", "yoy": "去年同期"}.get("day" if f.window == "day" else mode, "前一期間")
        exp_label = "整體同店同期變化" if mode == "yoy" else "去年同期間的變化"
        self.w(f"| 指標 | 本期 | {base_label} | 變化 | {exp_label} | z |", "|---|---|---|---|---|---|")
        for s in sorted(f.stats, key=lambda s: not s.significant):
            self.w(self.stat_row(s))
        self.w("")
        if f.evidence.get("pattern"):
            self.w(f"- 型態：{'、'.join(f.evidence['pattern'])}")
        c = f.concentrated_in
        if c:
            label = self.ctx.dims[c["dimension"]].label
            self.w(f"- **變化集中在{label}「{c['member']}」**：{self.e.label(f.scope)} × {c['member']} 的{c['metric']} "
                   f"{change_text(c['kind'], c['inside_change'])}，扣除這部分後其餘僅 {change_text(c['kind'], c['rest_change'])}")
        if f.explains:
            self.w(f"- 這也解釋了「{'」「'.join(dict.fromkeys(f.explains))}」的變化（扣除這部分後，其餘成員沒有明顯變化）")
        self.w(f"- 範圍：{self.path_text(f)}，佔整體明細 {f.share:.1%}")
        for note in f.notes:
            self.w(f"- 注意：{note}")
        self.w("")

    def effect_text(self, metric: str, value: float) -> str:
        """Mix/rate effects are in the metric's own unit: points for percentages, values otherwise."""
        if self.e.kind(metric) == "abs":
            return change_text("abs", value)
        return f"{value:+,.1f}"

    def decomposition(self, f: Finding) -> None:
        self.w(f"### {self.headline(f)}", "")
        for metric, ev in f.evidence.items():
            for drv in ev["drivers"]:
                parts = [f"{k} {change_text(self.e.kind(k), v)}" for k, v in drv.items() if k != "identity"]
                self.w(f"- **{metric}** 依模型關係 `{drv['identity']}` 拆解：{'、'.join(parts)}")
            for dim, info in ev["by_dimension"].items():
                label = self.ctx.dims[dim].label
                if info["type"] == "contribution":
                    tops = "、".join(f"{t['member']} {t['delta']:+,.0f}" + (f"（佔變動 {t['share_of_change']:.0%}）"
                                     if t["share_of_change"] is not None else "") for t in info["top"])
                    self.w(f"- **{metric}** 依{label}拆解，變動最大：{tops}")
                else:
                    fx = lambda v: self.effect_text(metric, v)  # noqa: E731
                    tops = "、".join(f"{t['member']}（本身 {fx(t['rate_effect'])}，比重 {fx(t['mix_effect'])}）"
                                     for t in info["top"])
                    self.w(f"- **{metric}** 依{label}拆解：各{label}本身的變化貢獻 {fx(info['rate_effect'])}，"
                           f"{label}比重改變貢獻 {fx(info['mix_effect'])}；主要來自 {tops}")
        for note in f.notes:
            self.w(f"- 注意：{note}")
        self.w("")

    def render(self) -> str:
        d = self.r["date"]
        self.w(f"# 每日營運變動報告：{d}（週{WEEKDAYS[d.weekday()]}）", "",
               f"> 產生時間 {datetime.now():%Y-%m-%d %H:%M}。所有數字由程式依 Power BI 模型的量值定義與關聯計算，"
               "不含推測；原因判斷僅限於資料能證明的範圍。", "")

        self.w("## 1. 資料品質", "")
        if self.r["data_quality"]:
            for issue in self.r["data_quality"]:
                self.w(f"- ⚠️ {issue['message']}")
            self.w("", "> 以下與上述範圍相關的單日變化，可能是資料缺漏而非實際營運變化。")
        else:
            self.w("- ✅ ETL 批次成功，載入筆數與明細一致，各門市資料量正常。")
        self.w("")

        self.w("## 2. 關鍵指標", "", "| 指標 | 當日 | vs 上週同日 | vs 近 4 週同星期平均 | 近 7 日 | vs 前 7 日 |",
               "|---|---|---|---|---|---|")
        for k in self.r["kpis"]:
            m = k["metric"]
            self.w(f"| {m} | {self.ctx.fmt(m, k['value'])} | {change_text(k['kind'], k['vs_last_week'])} | "
                   f"{change_text(k['kind'], k['vs_4wk_avg'])} | {self.ctx.fmt(m, k['week'])} | "
                   f"{change_text(k['kind'], k['week_vs_prev'])} |")
        self.w("")

        findings = self.r["findings"]
        totals = [f for f in findings if not f.scope]
        new = [f for f in findings if f.scope and f.new]
        ongoing = [f for f in findings if f.scope and not f.new]

        self.w("## 3. 新發現", "")
        if not new:
            self.w("- 沒有新的顯著變化。", "")
        for i, f in enumerate(new, 1):
            self.finding(i, f)

        if totals:
            self.w("## 4. 整體指標變化拆解", "")
            for f in totals:
                self.decomposition(f)

        self.w("## 5. 持續中的變化", "")
        if not ongoing:
            self.w("- 無。", "")
        for f in ongoing:
            self.w(f"- {self.headline(f)}，自 {f.since} 起持續" + (f"；{f.notes[0]}" if f.notes else ""))
        self.w("")

        self.w("## 6. 判斷方式與指標定義", "",
               "- **顯著**：變化量與該序列自身過去半年的正常波動相比，超過門檻個標準差（z 值；整體 ≥ "
               f"{self.ctx.config['thresholds']['z_alert']}，維度成員 ≥ {self.ctx.config['thresholds']['z_alert_member']}），"
               "且幅度夠大。7 日與 28 日的比較已扣除去年同期的季節性變化。",
               "- **範圍取捨**：若「北區 × 3C配件」的變化扣除後，3C配件 其餘區域都沒有變化，就只報告 北區 × 3C配件；"
               "若其他成員也同向變動，則報告較大的範圍。", "")
        self.w("| 指標 | 說明（Power BI 模型） | DAX |", "|---|---|---|")
        for m in self.r["metrics"]:
            meas = self.ctx.model.measures[m]
            self.w(f"| {m} | {meas.description} | `{meas.expression}` |")
        return "\n".join(self.lines) + "\n"


def to_json(result: dict, engine: Engine) -> dict:
    def conv(o):
        if is_dataclass(o):
            return conv(asdict(o))
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, (np.floating, float)):
            return None if o != o else float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        return o
    out = {k: v for k, v in result.items() if k != "engine"}
    out["findings"] = [dict(conv(f), label=engine.label(f.scope), new=f.new) for f in result["findings"]]
    return conv(out)


def write(ctx: Context, d: date) -> Path:
    result = run(ctx, d)
    REPORT_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "reports").mkdir(parents=True, exist_ok=True)
    md = REPORT_DIR / f"{d}.md"
    md.write_text(Writer(ctx, result).render(), encoding="utf-8")
    (DATA_DIR / "reports" / f"{d}.json").write_text(
        json.dumps(to_json(result, result["engine"]), ensure_ascii=False, indent=2), encoding="utf-8")
    return md
