"""The visuals of a report: which one the user means, what it shows, and its numbers as the report shows them.

Users ask about what they see: "what does the red mean?", "why is the North bar so low?", "how is this rate
computed?". A screenshot cannot answer that without guessing; the report file can:

  - locate(): which visual "the red one" / "北區那根長條" / "右上角那張" is, from colours the author set, member
    names, measure names, titles, chart types and position on the page. One clear match is used; several are
    listed for the user to pick, never guessed.
  - describe(): what the visual shows (fields and their roles), how each measure is computed (DAX and the same
    formula in source columns / this report's own measures), its filters and the author's settings on it.
  - values(): the visual's numbers over the date range the report is showing, from the daily snapshots, with
    DATEADD / SAMEPERIODLASTYEAR / TOTALMTD evaluated by shifting that range; and which members fall in which
    colour band of the author's rules. Measures the snapshots cannot reproduce are named, not estimated.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from . import catalog, intent

# Chinese name and the words users use for each visual type
TYPES = {
    "barChart": ("橫條圖", "橫條 長條 條形 bar"), "clusteredBarChart": ("橫條圖", "橫條 長條 條形 bar"),
    "columnChart": ("直條圖", "直條 柱狀 柱子 長條 bar column"),
    "clusteredColumnChart": ("直條圖", "直條 柱狀 柱子 長條 bar column"),
    "lineChart": ("折線圖", "折線 線圖 趨勢 走勢 line"), "areaChart": ("區域圖", "區域圖 面積"),
    "pivotTable": ("矩陣", "矩陣 表格 表 matrix table"), "tableEx": ("表格", "表格 表 table"),
    "cardVisual": ("卡片", "卡片 KPI 數字 card"), "card": ("卡片", "卡片 KPI 數字 card"),
    "multiRowCard": ("多列卡片", "卡片 card"), "pieChart": ("圓餅圖", "圓餅 pie"), "donutChart": ("環圈圖", "甜甜圈 環圈 圓餅 donut"),
    "gauge": ("量測計", "儀表 量測計 gauge"), "map": ("地圖", "地圖 map"), "filledMap": ("地圖", "地圖 map"),
    "azureMap": ("地圖", "地圖 map"), "shapeMap": ("地圖", "地圖 map"), "scatterChart": ("散佈圖", "散佈 散點 scatter"),
    "waterfallChart": ("瀑布圖", "瀑布 waterfall"), "treemap": ("樹狀圖", "樹狀 treemap"),
    "lineStackedColumnComboChart": ("折線與直條組合圖", "組合 折線 直條 combo"),
    "lineClusteredColumnComboChart": ("折線與直條組合圖", "組合 折線 直條 combo"),
}
COLORS = {"紅": "紅", "綠": "綠", "黃": "黃", "橘": "橘", "橙": "橘", "藍": "藍", "紫": "紫", "灰": "灰"}
POSITIONS = {"左上": (0, 0), "右上": (1, 0), "左下": (0, 1), "右下": (1, 1), "上面": (None, 0), "上方": (None, 0),
             "下面": (None, 1), "下方": (None, 1), "左邊": (0, None), "左側": (0, None), "右邊": (1, None),
             "右側": (1, None)}
LINE_WORDS = ("線", "目標", "底線", "門檻", "基準")
ROWS = ("Category", "Rows", "Axis", "Series", "Legend", "Group", "Columns", "Details")
# the report's date range for each panel window: the shared filter bar puts [date - days, date] on the report
REPORT_SPAN = {"day": 28, "week": 13, "month": 55, "year": 27}


@dataclass
class VisualInfo:
    page: str
    name: str
    title: str
    vtype: str
    box: tuple
    page_size: tuple
    fields: dict            # role → readable fields ("T[c]" for columns, the name for measures)
    measures: list
    columns: list           # "T[c]"
    emphasis: list = field(default_factory=list)
    filters: list = field(default_factory=list)
    hidden_page: bool = False

    @property
    def kind(self) -> str:
        return TYPES.get(self.vtype, (self.vtype or "圖表",))[0]

    @property
    def label(self) -> str:
        return f"{self.page}／{self.title or self.kind}"


def load(report: catalog.Report) -> list[VisualInfo]:
    """The data visuals of a report (decoration, slicers and navigation left out)."""
    report_dir = next(report.pbip.glob("*.Report"))
    visuals, hidden, _ = intent.load(report_dir)
    measures = set(report.model.measures)
    usage = {(u.page, u.visual): u for u in report.model.usage}
    out = []
    for v in visuals:
        if v.vtype in intent.DECORATION:
            continue
        flat = [f for fs in v.fields.values() for f in fs if f]
        u = usage.get((v.page, v.name))
        out.append(VisualInfo(
            v.page, v.name, v.title, v.vtype, v.box, v.page_size, v.fields,
            [f for f in dict.fromkeys(flat) if re.sub(r"^\w+\((.*)\)$", r"\1", f) in measures],
            [f for f in dict.fromkeys(flat) if "[" in f],
            [e for e in report.emphasis if e.page == v.page and e.where == v.name],
            list(u.filters) if u else [], v.page in hidden))
    return out


# ------------------------------------------------------------------------------------------- locate
def locate(infos: list[VisualInfo], text: str, page: str | None = None, members: dict[str, str] | None = None,
           dims: dict[str, str] | None = None) -> list[tuple[float, VisualInfo, list[str]]]:
    """Visuals matching what the user said, best first: (score, visual, reasons).
    members: member → "T[c]"; dims: dimension label (門市, 品類 …) → "T[c]"."""
    text = text or ""
    squeezed = re.sub(r"\s+", "", text).lower()
    colours = {c for w, c in COLORS.items() if w in text}
    found_members = {m: col for m, col in (members or {}).items() if len(m) >= 2 and m in text}
    found_dims = {label: col for label, col in (dims or {}).items() if len(label) >= 2 and label in text}
    # chart-type words, after removing the generic 報表 / 圖表 (「報表上紅色的」 does not mean a table)
    plain = re.sub(r"報表|圖表|[這那]張表(?!格)", "", squeezed)
    named_types = {t for t, (_, words) in TYPES.items() if any(w.lower() in plain for w in words.split())}
    out = []
    for v in infos:
        score, why = 0.0, []
        tones = {e_tone for e in v.emphasis for e_tone in _tones(e)}
        if colours & tones:
            score += 4
            why.append(f"作者在這張圖設了{'、'.join(sorted(colours & tones))}色")
        elif colours and not tones:
            score -= 1  # asked about a colour this visual never sets
        for m, col in found_members.items():
            if col in v.columns:
                score += 3
                why.append(f"依 {col} 分組，含「{m}」")
                if any(e.kind == "highlight" and m in e.text for e in v.emphasis):
                    score += 2
                    why.append(f"「{m}」被作者特別上色")
        for m in v.measures:
            name = re.sub(r"^\w+\((.*)\)$", r"\1", m)
            if len(name) >= 2 and name.replace(" ", "").lower() in squeezed:
                score += 3
                why.append(f"顯示「{name}」")
        title = re.sub(r"\s+", "", v.title)
        if len(title) >= 2 and title in squeezed:
            score += 5
            why.append(f"標題「{v.title}」")
        elif title:
            grams = {title[i:i + 2] for i in range(len(title) - 1)}
            hit = sum(1 for g in grams if g in squeezed)
            if hit >= 2:
                score += min(3.0, hit * 0.75)
                why.append(f"標題「{v.title}」相近")
        for label, col in found_dims.items():
            if col in v.columns:
                score += 2
                why.append(f"依{label}分組")
        if named_types:  # the user named a chart type: others are unlikely
            if v.vtype in named_types:
                score += 2
                why.append(f"是{v.kind}")
            else:
                score -= 1.5
        for words, prop in (("字", "字色"), ("底色", "背景色"), ("背景", "背景色"), ("圖示", "圖示")):
            if words in text and any(prop in e.text for e in v.emphasis):
                score += 1.5
                why.append(f"作者設了{prop}")
        if any(w in text for w in LINE_WORDS) and any(e.kind in ("reference_line", "analytics") for e in v.emphasis):
            score += 2
            why.append("有作者畫的線")
        for word, (cx, cy) in POSITIONS.items():
            if word in text and _at(v, cx, cy):
                score += 1
                why.append(f"位在頁面{word}")
        if page and v.page == page:
            score += 1.5
        elif v.page and v.page in text:
            score += 2
            why.append(f"在「{v.page}」頁")
        if v.hidden_page:
            score -= 1
        if score > 0:
            out.append((round(score, 2), v, why))
    return sorted(out, key=lambda x: -x[0])


def _tones(e) -> set[str]:
    out = {c.get("tone") for c in e.cases if c.get("tone")} | {t.get("tone") for t in e.thresholds if t.get("tone")}
    return out | {t for t in COLORS.values() if f"（{t}）" in e.text}


def _at(v: VisualInfo, cx, cy) -> bool:
    w, h = v.page_size
    x, y = v.box[0] + v.box[2] / 2, v.box[1] + v.box[3] / 2
    return (cx is None or (x < w / 2) == (cx == 0)) and (cy is None or (y < h / 2) == (cy == 0))


# ------------------------------------------------------------------------------------------- describe
def describe(cat: catalog.Catalog, report: catalog.Report, v: VisualInfo) -> dict:
    roles = {"Category": "類別", "Rows": "列", "Columns": "欄", "Series": "圖例（系列）", "Legend": "圖例", "Values": "值",
             "Y": "數值", "Data": "數值", "TargetValue": "目標值", "Indicator": "指標", "Goal": "目標"}
    out = {"圖表": f"{v.label}（{v.kind}）",
           "顯示的欄位": [f"{roles.get(r, r)}：{'、'.join(fs)}" for r, fs in v.fields.items() if fs]}
    defs = {}
    for m in v.measures:
        name = re.sub(r"^\w+\((.*)\)$", r"\1", m)
        c = cat.measures.get((report.name, name))
        if c is None:
            continue
        text = f"DAX：{c.expression.strip()}"
        own = cat.in_terms_of(c, report.name, partial=True) if c.tree else None
        if own and own != f"「{name}」":
            text += f"；用本報表的量值表示：{name} = {own}"
        elif c.tree:
            text += f"；展開到來源欄位：{c.readable()}"
        else:
            text += f"（無法展開：{c.reason}）"
        desc = report.model.measures[name].description
        defs[name] = (f"{desc}。" if desc else "") + text
    if defs:
        out["量值怎麼算"] = defs
    out["篩選"] = v.filters or ["只有報表與面板的篩選"]
    explicit = [e.text for e in v.emphasis if e.explicit]
    out["作者在這張圖的設定"] = explicit or ["沒有條件式格式、目標線或特別上色"]
    return out


# ------------------------------------------------------------------------------------------- values
class RangeEvaluator:
    """Measure values over a date range per member, from the analysis snapshots (the current dataset only)."""

    def __init__(self, cat: catalog.Catalog, report: catalog.Report, engine):
        self.cat, self.report, self.e = cat, report, engine
        ctx = engine.ctx
        self.ctx = ctx
        ex = catalog.Expander(report)
        self.atom_base = {}  # catalog aggregate → analysis base
        for name, b in ctx.bases.items():
            if b.name in engine.skipped_bases or not b.dax:
                continue
            try:
                tree = ex.expression(b.dax)
            except Exception:  # noqa: BLE001 - a base the catalog cannot read is simply not used
                continue
            if tree[0] == "atom":
                self.atom_base.setdefault(tree[1], name)
        self.date_key = catalog.short(catalog.column_source(ctx.model.tables[ctx.date_dim.table],
                                                            ctx.date_dim.column) or "")

    def plan(self, measure: str):
        """(tree, {atom: (base, shift)}) or the reason the measure cannot be computed from the snapshots."""
        c = self.cat.measures.get((self.report.name, measure))
        if c is None or not c.tree:
            return None, (c.reason if c else "量值不在模型中")
        plan = {}
        for atom in c.atoms:
            agg, _, mods = atom.partition(" | ")
            shift, rest = ("days", 0), []
            for mod in filter(None, mods.split("；")):
                m = re.fullmatch(r"日期位移 ([+-]\d+) (DAY|MONTH|YEAR)（(.+)）", mod)
                t = re.fullmatch(r"(MTD|QTD|YTD) 累計（(.+)）", mod)
                if m and m.group(3) == self.date_key:
                    shift = (m.group(2).lower() + "s", int(m.group(1)))
                elif t and t.group(2) == self.date_key:
                    shift = (t.group(1), 0)
                else:
                    rest.append(mod)
            key = agg + (f" | {'；'.join(sorted(rest))}" if rest else "")
            if key not in self.atom_base:
                return None, f"用到快照沒有的彙總：{catalog._render(('atom', atom))}"
            plan[atom] = (self.atom_base[key], shift)
        return c.tree, plan

    def values(self, measure: str, dims: tuple, scope: dict, d0: date, d1: date) -> dict[tuple, float] | str:
        """{member tuple: value} for the dims (() = the total), within scope {dim key: member}."""
        tree, plan = self.plan(measure)
        if tree is None:
            return plan
        keys = tuple(dict.fromkeys(tuple(scope) + dims))
        cube = self.e.cube(keys)
        dates = cube.dates
        out = {}
        groups: dict[tuple, list[int]] = {}
        for i, mem in enumerate(cube.members):
            if all(mem[keys.index(k)] == v for k, v in scope.items()):
                groups.setdefault(tuple(mem[keys.index(k)] for k in dims), []).append(i)
        for member, cols in groups.items():
            vals = {}
            for atom, (base, shift) in plan.items():
                a, b = _shifted(d0, d1, shift)
                if a < dates[0]:
                    return f"快照從 {dates[0]} 開始，算不到 {a}"
                ia, ib = (a - dates[0]).days, (b - dates[0]).days
                vals[atom] = float(np.nansum(cube.data[base][ia:ib + 1][:, cols]))
            v = catalog._value(tree, vals)
            out[member] = None if v != v else v
        return out


def _shifted(d0: date, d1: date, shift: tuple) -> tuple[date, date]:
    kind, n = shift
    if kind == "days":
        return d0 + timedelta(days=n), d1 + timedelta(days=n)
    if kind in ("months", "years"):
        months = n * (12 if kind == "years" else 1)
        return _add_months(d0, months), _add_months(d1, months)
    start = {"MTD": d1.replace(day=1), "QTD": d1.replace(month=(d1.month - 1) // 3 * 3 + 1, day=1),
             "YTD": d1.replace(month=1, day=1)}[kind]
    return start, d1


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return d.replace(year=y, month=m, day=min(d.day, last))


def band(cases: list[dict], value: float) -> dict | None:
    """The first of the author's rule cases the value satisfies."""
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
           "=": lambda a, b: a == b}
    return next((c for c in cases if all(ops[op](value, x) for op, x in c["conds"])), None)


def path_of(report: catalog.Report) -> Path:
    return next(report.pbip.glob("*.Report"))
