"""What the report's author chose to emphasise, read from the report layout (no data needed).

Power BI keeps the author's formatting next to each visual: conditional-formatting rules and their thresholds,
colour scales, constant / analytics lines, colours given to single members, Top N filters, the comparison
value on a card, a gauge's target. These are explicit choices, so they can be quoted as facts
("the author colours 毛利率 < 45% red"); layout (what sits top-left, what is largest) is only a hint and is
labelled as such.

Reads both report formats: PBIR folders (Report/definition/... in a .pbix, *.Report/definition in a PBIP) and
the legacy Report/Layout of older .pbix files.

    python -m analysis intent path/to/report.pbix | path/to/Name.Report | <dataset name>
"""
import colorsys
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

COMPARISON = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}
SHOW_OP = {"=": "=", ">": ">", ">=": "≥", "<": "<", "<=": "≤"}
AGGREGATION = {0: "總和", 1: "平均", 2: "計數", 3: "最小值", 4: "最大值", 5: "非空計數", 6: "中位數",
               7: "標準差", 8: "變異數"}
PROPERTY = {"backColor": "背景色", "fontColor": "字色", "fill": "填色", "fillColor": "填色", "icon": "圖示",
            "color": "顏色", "lineColor": "線條色", "dataBars": "資料橫條", "webURL": "連結", "strokeColor": "外框色",
            "markerColor": "標記色", "valueFontColor": "數值字色", "detailColor": "明細色", "labelColor": "標籤色"}
# visuals that carry no data (decoration, navigation, filters)
DECORATION = {"textbox", "shape", "basicShape", "image", "actionButton", "pageNavigator", "bookmarkNavigator",
              "slicer", "advancedSlicerVisual", None, ""}


@dataclass
class Emphasis:
    kind: str     # rule | gradient | color_by_field | data_bars | reference_line | analytics | highlight |
    #               member_colors | top_n | comparison | target | layout | hidden_page | bookmarks
    page: str
    visual: str   # visual title, or its type when it has no title
    field: str    # the measure / column the emphasis is about ("" for page-level items)
    text: str     # readable description (Traditional Chinese)
    explicit: bool = True       # an explicit setting (True) or a hint read from the layout (False)
    thresholds: list = field(default_factory=list)  # rules: [{"op", "value", "color", "tone"}] with literal values
    where: str = ""             # page / visual id inside the report, for tracing back


@dataclass
class _Visual:
    page: str
    page_index: int
    page_size: tuple
    name: str
    vtype: str
    title: str
    box: tuple                  # x, y, width, height
    objects: dict
    vc_objects: dict
    fields: dict                # role → [readable field]
    filters: list
    alias: dict = field(default_factory=dict)  # query alias → entity (legacy prototypeQuery)


# ------------------------------------------------------------------------------------------- loading
def _pbir_visuals(files: dict[str, dict]) -> tuple[list[_Visual], list[str], list[str]]:
    """files: path under definition/ → parsed JSON."""
    pages_meta = files.get("pages/pages.json", {})
    order = pages_meta.get("pageOrder") or sorted({p.split("/")[1] for p in files if p.startswith("pages/")
                                                   and p.count("/") >= 2})
    visuals, hidden = [], []
    for i, page in enumerate(order):
        meta = files.get(f"pages/{page}/page.json", {})
        display = meta.get("displayName", page)
        if meta.get("visibility") == "HiddenInViewMode":
            hidden.append(display)
        size = (meta.get("width", 1280), meta.get("height", 720))
        prefix = f"pages/{page}/visuals/"
        for path, v in sorted(files.items()):
            if not (path.startswith(prefix) and path.endswith("/visual.json")):
                continue
            vis = v.get("visual", {})
            pos = v.get("position", {})
            roles = {r: [_field_text(p.get("field", {})) for p in q.get("projections", [])]
                     for r, q in vis.get("query", {}).get("queryState", {}).items()}
            visuals.append(_Visual(display, i, size, v.get("name", path[len(prefix):].split("/")[0]),
                                   vis.get("visualType", ""), _title(vis.get("visualContainerObjects", {})),
                                   (pos.get("x", 0), pos.get("y", 0), pos.get("width", 0), pos.get("height", 0)),
                                   vis.get("objects", {}), vis.get("visualContainerObjects", {}), roles,
                                   v.get("filterConfig", {}).get("filters", [])))
    bookmarks = [b.get("displayName", "") for p, b in files.items()
                 if p.startswith("bookmarks/") and p.endswith(".bookmark.json")]
    return visuals, hidden, [b for b in bookmarks if b]


def _legacy_visuals(layout: dict) -> tuple[list[_Visual], list[str], list[str]]:
    visuals, hidden = [], []
    for i, sec in enumerate(layout.get("sections", [])):
        display = sec.get("displayName", sec.get("name", f"page{i}"))
        if json.loads(sec.get("config") or "{}").get("visibility") == 1:
            hidden.append(display)
        size = (sec.get("width", 1280), sec.get("height", 720))
        for j, vc in enumerate(sec.get("visualContainers", [])):
            cfg = json.loads(vc.get("config") or "{}")
            sv = cfg.get("singleVisual") or {}
            query = sv.get("prototypeQuery") or {}
            alias = {f["Name"]: f["Entity"] for f in query.get("From", [])}
            by_ref = {s.get("Name"): _field_text(s, alias) for s in query.get("Select", [])}
            roles = {r: [by_ref.get(p.get("queryRef"), p.get("queryRef", "")) for p in ps]
                     for r, ps in (sv.get("projections") or {}).items()}
            visuals.append(_Visual(display, i, size, cfg.get("name", f"v{j}"), sv.get("visualType", ""),
                                   _title(sv.get("vcObjects") or {}),
                                   (vc.get("x", 0), vc.get("y", 0), vc.get("width", 0), vc.get("height", 0)),
                                   sv.get("objects") or {}, sv.get("vcObjects") or {}, roles,
                                   json.loads(vc.get("filters") or "[]"), alias))
    config = json.loads(layout.get("config") or "{}")
    bookmarks = [b.get("displayName", "") for b in config.get("bookmarks", [])]
    for b in config.get("bookmarks", []):  # bookmark groups
        bookmarks += [c.get("displayName", "") for c in b.get("children", [])]
    return visuals, hidden, [b for b in bookmarks if b]


def load(source: Path) -> tuple[list[_Visual], list[str], list[str]]:
    """Visuals, hidden pages and bookmark names of a .pbix, a PBIP *.Report folder or its definition folder."""
    source = Path(source)
    if source.suffix.lower() == ".pbix":
        with zipfile.ZipFile(source) as z:
            names = z.namelist()
            if "Report/Layout" in names:
                return _legacy_visuals(json.loads(z.read("Report/Layout").decode("utf-16-le")))
            files = {n[len("Report/definition/"):]: json.loads(z.read(n).decode("utf-8-sig"))
                     for n in names if n.startswith("Report/definition/") and n.endswith(".json")}
            return _pbir_visuals(files)
    root = source / "definition" if (source / "definition").is_dir() else source
    files = {p.relative_to(root).as_posix(): json.loads(p.read_text(encoding="utf-8-sig"))
             for p in root.rglob("*.json")}
    return _pbir_visuals(files)


# ------------------------------------------------------------------------------------------- expressions
def _field_text(node: dict, alias: dict | None = None) -> str:
    """{"Measure": …} / {"Column": …} / {"Aggregation": …} / {"SelectRef": …} → readable text."""
    alias = alias or {}
    if not isinstance(node, dict):
        return ""
    for kind in ("Measure", "Column", "HierarchyLevel"):
        if kind in node:
            body = node[kind]
            ref = body.get("Expression", {}).get("SourceRef", {})
            table = ref.get("Entity") or alias.get(ref.get("Source"), ref.get("Source", ""))
            if kind == "HierarchyLevel":
                table = body.get("Expression", {}).get("Hierarchy", {}).get("Hierarchy", table)
                return f"{table}.{body.get('Level', '')}"
            return f"{table}[{body.get('Property', '')}]" if kind == "Column" else body.get("Property", "")
    if "Aggregation" in node:
        agg = node["Aggregation"]
        return f"{AGGREGATION.get(agg.get('Function'), '彙總')}({_field_text(agg.get('Expression', {}), alias)})"
    if "SelectRef" in node:
        return node["SelectRef"].get("ExpressionName", "")
    if "ScopedEval" in node:
        return _field_text(node["ScopedEval"].get("Expression", {}), alias)
    return ""


def _title(vc_objects: dict) -> str:
    for t in vc_objects.get("title", []) or []:
        value = _literal(t.get("properties", {}).get("text", {}).get("expr", {}))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _literal(node: dict):
    """Literal value: '…' text, 1D / 2L numbers, true / false; None when not a literal."""
    v = node.get("Literal", {}).get("Value") if isinstance(node, dict) else None
    if v is None:
        return None
    if v.startswith("'"):
        return v[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+(\.\d+)?(E[-+]?\d+)?[DLM]?", v):
        return float(v.rstrip("DLM")) if any(c in v for c in ".E") or v.endswith(("D", "M")) else int(v.rstrip("L"))
    return {"true": True, "false": False, "null": None}.get(v, v)


def _color(node) -> str:
    """Colour of a fill / color property: '#RRGGBB', 主題色 N, or the field that decides it."""
    if not isinstance(node, dict):
        return ""
    node = node.get("solid", {}).get("color", node)
    node = node.get("color", node)  # gradient stops: {"color": {"Literal": …}}
    expr = node.get("expr", node)
    lit = _literal(expr)
    if lit in ("minColor", "midColor", "maxColor"):
        return "預設色"
    if isinstance(lit, str):
        return lit
    if "ThemeDataColor" in expr:
        t = expr["ThemeDataColor"]
        return f"主題色 {t.get('ColorId')}" + (f"（{t.get('Percent'):+.0%}）" if t.get("Percent") else "")
    return ""


def tone(color: str) -> str:
    """紅 / 橘 / 黃 / 綠 / 藍 / 紫 / 灰 for a #RRGGBB colour; '' otherwise (theme colours depend on the theme)."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color or "")
    if not m:
        return ""
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < 0.08:
        return "黑"
    if l > 0.95:
        return "白"
    if s < 0.18:
        return "灰"
    h *= 360
    for limit, name in ((15, "紅"), (40, "橘"), (65, "黃"), (165, "綠"), (255, "藍"), (325, "紫"), (360, "紅")):
        if h < limit:
            return name
    return ""


def _color_text(color: str) -> str:
    t = tone(color)
    return f"{color}（{t}）" if t else color


_PERCENT: set[str] = set()   # measures formatted as percentages (set by extract), shown as 90% instead of 0.9


def fmt(value, field: str = "") -> str:
    """3.0 → 3, 0.25 → 0.25 (25% for a percentage measure), text as is."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and field and \
            re.sub(r"^\w+\((.*)\)$", r"\1", field) in _PERCENT:
        return f"{value:.1%}".replace(".0%", "%")
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() else f"{value:,.6g}"
    return "空白" if value is None else str(value)


def _condition(node: dict, alias: dict) -> tuple[str, list]:
    """Condition of a rule → readable text and [(field, op, value)] for literal comparisons."""
    if "And" in node:
        lt, lc = _condition(node["And"]["Left"], alias)
        rt, rc = _condition(node["And"]["Right"], alias)
        return f"{lt} 且 {rt}", lc + rc
    if "Or" in node:
        lt, _ = _condition(node["Or"]["Left"], alias)
        rt, _ = _condition(node["Or"]["Right"], alias)
        return f"{lt} 或 {rt}", []
    if "Not" in node:
        t, _ = _condition(node["Not"]["Expression"], alias)
        return f"非（{t}）", []
    if "Comparison" in node:
        cmp = node["Comparison"]
        left = _field_text(cmp.get("Left", {}), alias)
        op = COMPARISON.get(cmp.get("ComparisonKind"), "?")
        right = cmp.get("Right", {})
        if "RangePercent" in right:
            return f"{left} {SHOW_OP.get(op, op)} 畫面上範圍的 {right['RangePercent'].get('Percent', 0):.0%} 處", []
        value = _literal(right)
        if value is None and "Literal" not in right:
            return f"{left} {SHOW_OP.get(op, op)} {_field_text(right, alias) or '（動態值）'}", []
        if value is None:
            return f"{left} {'為' if op == '=' else SHOW_OP.get(op, op)}空白", []
        return f"{left} {SHOW_OP.get(op, op)} {fmt(value, left)}", [(left, op, value)]
    if "In" in node:
        exprs = node["In"].get("Expressions", [])
        values = [_literal(v[0]) for v in node["In"].get("Values", [])]
        return f"{_field_text(exprs[0], alias) if exprs else '?'} 屬於 {'、'.join(fmt(v) for v in values)}", []
    return "（無法解讀的條件）", []


# ------------------------------------------------------------------------------------------- extraction
def _walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, path + (k,))
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, path)


def _formatting(v: _Visual, label: str) -> list[Emphasis]:
    out = []
    for obj_name, entries in {**v.objects, **v.vc_objects}.items():
        for entry in entries if isinstance(entries, list) else []:
            target = (entry.get("selector") or {}).get("metadata", "")
            for prop, value in (entry.get("properties") or {}).items():
                prop_text = PROPERTY.get(prop, prop)
                if prop == "dataBars" or (obj_name == "values" and prop.startswith("dataBar")):
                    if prop == "dataBars":
                        out.append(Emphasis("data_bars", v.page, label, _metadata_text(target),
                                            f"「{_metadata_text(target)}」以資料橫條顯示大小", where=v.name))
                    continue
                for _, node in _walk(value):
                    if "Conditional" in node:
                        out.append(_rule(v, label, node["Conditional"], target, prop_text))
                    elif "FillRule" in node and "Input" in node["FillRule"]:  # not the nested gradient spec
                        out.append(_gradient(v, label, node["FillRule"], target, prop_text))
                    elif set(node) == {"expr"} and any(k in node["expr"] for k in ("Measure", "Column", "Aggregation")) \
                            and prop in ("color", "fill", "fillColor", "backColor", "fontColor"):
                        src = _field_text(node["expr"], v.alias)
                        out.append(Emphasis("color_by_field", v.page, label, _metadata_text(target) or src,
                                            f"{_target_text(target)}{prop_text}由「{src}」的值決定", where=v.name))
    return out


def _metadata_text(meta: str) -> str:
    """Selector metadata ('Sales.Profit %', 'Sum(Sales.Amount)') → field name as shown in the visual."""
    m = re.fullmatch(r"(\w+)\((.+)\)", meta or "")
    inner = m.group(2) if m else meta or ""
    name = inner.split(".", 1)[1] if "." in inner else inner
    return f"{m.group(1)}({name})" if m else name


def _target_text(meta: str) -> str:
    return f"「{_metadata_text(meta)}」的" if meta else ""


def _rule(v: _Visual, label: str, cond: dict, target: str, prop_text: str) -> Emphasis:
    parts, thresholds, fields = [], [], []
    for case in cond.get("Cases", []):
        text, cmps = _condition(case.get("Condition", {}), v.alias)
        val = case.get("Value", {})
        result = _literal(val)
        result = _color_text(result) if isinstance(result, str) and result.startswith("#") else \
            (result if result is not None else _field_text(val, v.alias))
        parts.append(f"{text} → {result}")
        color = _literal(val) if isinstance(_literal(val), str) else ""
        for f_, op, value in cmps:
            fields.append(f_)
            thresholds.append({"field": f_, "op": op, "value": value, "result": color, "tone": tone(color)})
    if "DefaultValue" in cond:
        d = _literal(cond["DefaultValue"])
        parts.append(f"其他 → {_color_text(d) if isinstance(d, str) else d}")
    field_ = _metadata_text(target) or (fields[0] if fields else "")
    return Emphasis("rule", v.page, label, field_, f"{_target_text(target)}{prop_text}規則：" + "；".join(parts),
                    thresholds=thresholds, where=v.name)


def _gradient(v: _Visual, label: str, rule: dict, target: str, prop_text: str) -> Emphasis:
    src = _field_text(rule.get("Input", {}), v.alias)
    spec = rule.get("FillRule", {})
    grad = spec.get("linearGradient3") or spec.get("linearGradient2") or {}
    stops = []
    for stop in ("min", "mid", "max"):
        if stop in grad:
            value = _literal(grad[stop].get("value", {}))
            stops.append(f"{ {'min': '最低', 'mid': '中間', 'max': '最高'}[stop]}"
                         f"{f'（{value}）' if value is not None else ''} {_color_text(_color(grad[stop]))}")
    return Emphasis("gradient", v.page, label, _metadata_text(target) or src,
                    f"{_target_text(target)}{prop_text}依「{src}」漸層：" + " → ".join(stops), where=v.name)


def _lines(v: _Visual, label: str) -> list[Emphasis]:
    out = []
    for obj_name, entries in v.objects.items():
        if not isinstance(entries, list):
            continue
        analytics = obj_name in ("trend", "forecast", "anomalyDetection", "ratioLine")
        if not (analytics or obj_name.endswith("ReferenceLine") or obj_name == "referenceLine"):
            continue
        for e in entries:
            p = e.get("properties", {})
            if _literal(p.get("show", {}).get("expr", {})) is not True:
                continue
            name = _literal(p.get("displayName", {}).get("expr", {})) or obj_name
            series = _metadata_text((e.get("selector") or {}).get("metadata", ""))
            if analytics:
                kind = {"trend": "趨勢線", "forecast": "預測", "anomalyDetection": "異常偵測",
                        "ratioLine": "比率線"}[obj_name]
                params = {x.get("Name"): _literal(x) for x in p.get("transform", {}).get("parameters", [])}
                extra = "、".join(f"{k} {fmt(v_)}" for k, v_ in params.items())
                out.append(Emphasis("analytics", v.page, label, series,
                                    f"{kind}「{name}」" + (f"（{extra}）" if extra else ""), where=v.name))
                continue
            expr = p.get("value", {}).get("expr", {})
            value = _literal(expr)
            axis = {"y1AxisReferenceLine": "Y 軸", "y2AxisReferenceLine": "副 Y 軸",
                    "xAxisReferenceLine": "X 軸"}.get(obj_name, "")
            if value is not None:
                shown = next(iter(v.fields.get("Y", [])), series)
                text, kind = f"{axis}常數線「{name}」= {fmt(value, shown)}", "reference_line"
            elif expr:
                text, kind = f"{axis}分析線「{name}」= {_field_text(expr, v.alias)}", "analytics"
            else:
                text, kind = f"{axis}常數線「{name}」（未設定值）", "reference_line"
            out.append(Emphasis(kind, v.page, label, series, text,
                                thresholds=[{"field": series, "op": "line", "value": value}] if value is not None
                                else [], where=v.name))
    return out


def _member_colors(v: _Visual, label: str) -> list[Emphasis]:
    members = []
    for e in v.objects.get("dataPoint", []) or []:
        color = _color((e.get("properties") or {}).get("fill", {}))
        for d in (e.get("selector") or {}).get("data", []) or []:
            cmp = d.get("scopeId", {}).get("Comparison")
            if cmp and color:
                members.append((_field_text(cmp.get("Left", {}), v.alias), _literal(cmp.get("Right", {})), color))
    out = []
    for col in dict.fromkeys(c for c, _, _ in members):
        mine = list(dict.fromkeys((m, c) for c_, m, c in members if c_ == col))
        listed = "、".join(f"{fmt(m)}（{_color_text(c)}）" for m, c in mine[:8]) + ("…" if len(mine) > 8 else "")
        if len(mine) == 1:  # one member singled out; two or more is usually a colour scheme for all members
            out.append(Emphasis("highlight", v.page, label, col, f"只把 {col} = {listed} 改成特定顏色"
                                f"（只改一個成員，通常是要凸顯它）", where=v.name))
        else:
            out.append(Emphasis("member_colors", v.page, label, col, f"為 {len(mine)} 個 {col} 成員指定顏色：{listed}",
                                where=v.name))
    return out


def _top_n(v: _Visual, label: str) -> list[Emphasis]:
    out = []
    for f in v.filters:
        if f.get("type") != "TopN":
            continue
        flt = f.get("filter", {})
        alias = {x.get("Name"): x.get("Entity") for x in flt.get("From", []) if x.get("Entity")}
        sub = next((x["Expression"]["Subquery"]["Query"] for x in flt.get("From", [])
                    if "Subquery" in x.get("Expression", {})), {})
        sub_alias = {x.get("Name"): x.get("Entity") for x in sub.get("From", [])}
        order = (sub.get("OrderBy") or [{}])[0]
        by = _field_text(order.get("Expression", {}), sub_alias)
        what = _field_text(f.get("expression", {}), alias)
        n = sub.get("Top")
        side = "前" if order.get("Direction", 2) == 2 else "後"
        out.append(Emphasis("top_n", v.page, label, what, f"只顯示依「{by}」排序的{side} {n} 名 {what}", where=v.name))
    return out


def _roles(v: _Visual, label: str) -> list[Emphasis]:
    out = []
    shown = [f for fs in v.fields.values() for f in fs]
    for e in v.objects.get("referenceLabel", []) or []:
        expr = (e.get("properties") or {}).get("value", {}).get("expr", {})
        cmp = _field_text(expr, v.alias)
        if cmp:
            main = _metadata_text((e.get("selector") or {}).get("metadata", "")) or (shown[0] if shown else "")
            out.append(Emphasis("comparison", v.page, label, main, f"卡片「{main}」旁邊同時顯示「{cmp}」",
                                where=v.name))
    for role, text in (("TargetValue", "目標值"), ("Goal", "目標"), ("MinValue", "最小值"), ("MaxValue", "最大值"),
                       ("Target", "要解釋的目標")):
        for f in v.fields.get(role, []):
            main = next(iter(v.fields.get("Y", []) or v.fields.get("Indicator", []) or v.fields.get("Values", [])
                             or [""]), "")
            out.append(Emphasis("target", v.page, label, main or f,
                                f"{v.vtype} 以「{f}」作為{text}" + (f"（主要數值：{main}）" if main else ""),
                                where=v.name))
    return out


def _layout(visuals: list[_Visual], hidden: list[str]) -> list[Emphasis]:
    """Per visible page: the data visual read first (top-left) and the largest one. A hint, not a stated intent."""
    out = []
    for page in dict.fromkeys(v.page for v in visuals if v.page not in hidden):
        data = [v for v in visuals if v.page == page and v.vtype not in DECORATION and v.box[2] * v.box[3] > 0]
        if not data:
            continue
        w, h = data[0].page_size
        first = min(data, key=lambda v: (round(v.box[1] / 40), v.box[0]))
        largest = max(data, key=lambda v: v.box[2] * v.box[3])
        share = largest.box[2] * largest.box[3] / max(w * h, 1)
        text = f"第一眼位置（左上）的圖表：{_label(first)}（{_fields_text(first)}）"
        if largest is not first:
            text += f"；面積最大：{_label(largest)}（{_fields_text(largest)}，占頁面 {share:.0%}）"
        else:
            text += f"，也是面積最大的（占頁面 {share:.0%}）"
        out.append(Emphasis("layout", page, "", "", text, explicit=False))
    return out


def _label(v: _Visual) -> str:
    if v.title:
        return v.title
    return "自訂視覺效果" if re.search(r"[0-9A-F]{8}|^PBI_CV_", v.vtype or "") else v.vtype


def _fields_text(v: _Visual) -> str:
    fs = list(dict.fromkeys(f for fs in v.fields.values() for f in fs if f))
    return "、".join(fs[:4]) + ("…" if len(fs) > 4 else "")


def extract(source: Path, percent: set[str] | None = None) -> list[Emphasis]:
    """percent: measures with a percentage format string, so thresholds read 90% rather than 0.9."""
    _PERCENT.clear()
    _PERCENT.update(percent or ())
    visuals, hidden, bookmarks = load(source)
    out = []
    for v in visuals:
        label = _label(v)
        out += _formatting(v, label) + _lines(v, label) + _member_colors(v, label) + _top_n(v, label) + \
            _roles(v, label)
    out += _layout(visuals, hidden)
    out += [Emphasis("hidden_page", p, "", "", "隱藏頁（通常是鑽研 / 工具提示頁，只在點選時出現）") for p in hidden]
    if bookmarks:
        out.append(Emphasis("bookmarks", "", "", "", f"書籤 {len(bookmarks)} 個：{'、'.join(bookmarks[:12])}"
                            + ("…" if len(bookmarks) > 12 else "")))
    return out


def thresholds(items: list[Emphasis]) -> list[dict]:
    """Literal thresholds the author set (rules and constant lines), for checking values against them."""
    out = []
    for e in items:
        for t in e.thresholds:
            if t.get("value") is not None and isinstance(t["value"], (int, float)):
                out.append({**t, "page": e.page, "visual": e.visual, "kind": e.kind, "text": e.text})
    return out


KIND_TITLE = {
    "rule": "條件式格式（規則與門檻）", "gradient": "色階", "color_by_field": "依欄位值上色", "data_bars": "資料橫條",
    "reference_line": "常數線 / 目標線", "analytics": "分析線（平均、最小、趨勢、預測、異常偵測）",
    "highlight": "凸顯特定成員", "member_colors": "成員配色", "top_n": "前 N 名篩選", "comparison": "卡片比較值",
    "target": "目標值", "layout": "版面（推斷，非作者明確設定）", "hidden_page": "隱藏頁", "bookmarks": "書籤",
}


def to_markdown(items: list[Emphasis], title: str) -> str:
    lines = [f"# 報表作者的設定與凸顯：{title}", "",
             "> 由報表版面讀出，不需要資料。「版面」一節是依位置與大小推斷的提示，其餘都是作者明確做的設定。", ""]
    for kind, head in KIND_TITLE.items():
        group = [e for e in items if e.kind == kind]
        if not group:
            continue
        lines += [f"## {head}（{len(group)}）", ""]
        for e in group:
            where = "／".join(x for x in (e.page, e.visual) if x)
            lines.append(f"- {f'**{where}**：' if where else ''}{e.text}")
        lines.append("")
    return "\n".join(lines)


def to_json(items: list[Emphasis]) -> list[dict]:
    return [asdict(e) for e in items]
