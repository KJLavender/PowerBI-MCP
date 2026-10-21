"""Generate the finance team's store-target report (PBIP): report B next to the retail report A.

    python datasets/store_targets/build_pbip.py [--excel PATH]

Built the way a second team would build it, to test what the catalog can tell about two related reports:
  - the same Oracle tables as the retail report, renamed in the model (銷售 = FACT_SALES, 實收金額 = SALES_AMOUNT),
    with the connection typed inline instead of the retail report's parameters
  - an Excel workbook of monthly targets per store code (prepare.py), joined to the stores
  - measures that agree with the retail report under another name (營收 = 銷售額), under another formula
    (毛利率), and ones that share a name but not a meaning (銷售額 before discount, 訂單數 counting lines)
  - the author's emphasis: red / yellow / green rules on 達成率, a 100 % target line, a 45 % margin floor,
    one region singled out in colour
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "powerbi"))
from build_pbip import (SCHEMA, indent, literal, q, reset_dir, tabs, textbox, visual, write,  # noqa: E402
                        write_json)
from build_pbip import tag as _tag  # noqa: E402

NAME = "StoreTargets"
OUT = HERE / "powerbi"
MODEL_DIR = OUT / f"{NAME}.SemanticModel"
REPORT_DIR = OUT / f"{NAME}.Report"
DEFAULT_EXCEL = HERE / "data" / "store_targets.xlsx"
ORACLE = ("127.0.0.1:1521/FREEPDB1", "SALES_DW")


def tag(*parts: str) -> str:
    return _tag(NAME, *parts)


INT, DEC, DATE, TXT = ("Int64.Type", "int64"), ("Currency.Type", "decimal"), ("type date", "dateTime"), \
    ("type text", "string")

# model column, source column, type, options
TABLES = {
    "銷售": {
        "oracle": "FACT_SALES",
        "description": "銷售明細（Oracle FACT_SALES）",
        "columns": [
            ("訂單編號", "ORDER_ID", INT, {"hidden": True}),
            ("明細序號", "LINE_NO", INT, {"hidden": True}),
            ("DATE_KEY", "DATE_KEY", INT, {"hidden": True}),
            ("門市ID", "STORE_ID", INT, {"hidden": True}),
            ("實收金額", "SALES_AMOUNT", DEC, {"format": "#,##0", "description": "折扣後實收金額"}),
            ("折扣金額", "DISCOUNT_AMOUNT", DEC, {"format": "#,##0"}),
            ("成本金額", "COST_AMOUNT", DEC, {"format": "#,##0"}),
        ],
    },
    "門市": {
        "oracle": "DIM_STORE",
        "description": "門市（Oracle DIM_STORE）",
        "columns": [
            ("門市ID", "STORE_ID", INT, {"hidden": True}),
            ("門市代碼", "STORE_CODE", TXT, {}),
            ("門市名稱", "STORE_NAME", TXT, {}),
            ("區域", "REGION_NAME", TXT, {}),
        ],
    },
    "日期": {
        "oracle": "DIM_DATE",
        "description": "日期（Oracle DIM_DATE）",
        "columns": [
            ("DATE_KEY", "DATE_KEY", INT, {"hidden": True}),
            ("日期", "FULL_DATE", DATE, {"format": "yyyy-mm-dd"}),
            ("年月", "YEAR_MONTH", TXT, {}),
        ],
    },
    "門市月目標": {
        "sheet": "門市月目標",
        "description": "財務部每月門市營收目標（Excel）",
        "columns": [
            ("門市代碼", "門市代碼", TXT, {}),
            ("年月", "年月", TXT, {}),
            ("目標營收", "目標營收", DEC, {"format": "#,##0"}),
        ],
    },
}

# from table, column, to table, column, to-side cardinality
RELATIONSHIPS = [
    ("銷售", "DATE_KEY", "日期", "DATE_KEY", "one"),
    ("銷售", "門市ID", "門市", "門市ID", "one"),
    ("門市月目標", "門市代碼", "門市", "門市代碼", "one"),
    ("門市月目標", "年月", "日期", "年月", "many"),   # month-level targets against daily dates
]

PCT, MONEY = "0.0%;-0.0%;0.0%", "#,##0"
MEASURES = [
    ("銷售", "營收", "SUM ( '銷售'[實收金額] )", MONEY, "折扣後實收金額"),
    ("銷售", "銷售額", "SUM ( '銷售'[實收金額] ) + SUM ( '銷售'[折扣金額] )", MONEY, "折扣前銷售額（財務部定義）"),
    ("銷售", "毛利", "[營收] - SUM ( '銷售'[成本金額] )", MONEY, "營收 − 成本"),
    ("銷售", "毛利率", "DIVIDE ( [營收] - SUM ( '銷售'[成本金額] ), [營收] )", PCT, "毛利 ÷ 營收"),
    ("銷售", "訂單數", "COUNTROWS ( '銷售' )", MONEY, "銷售明細筆數"),
    ("門市月目標", "目標營收", "SUM ( '門市月目標'[目標營收] )", MONEY, "財務部設定的營收目標"),
    ("門市月目標", "達成率", "DIVIDE ( [營收], [目標營收] )", PCT, "營收 ÷ 目標營收"),
    ("門市月目標", "目標差額", "[營收] - [目標營收]", MONEY, "營收 − 目標營收"),
]


def m_partition(spec: dict, excel_param: str) -> str:
    types = ",\n".join(f'{{"{src}", {mt}}}' for _, src, (mt, _), _ in spec["columns"])
    if "oracle" in spec:
        server, schema = ORACLE
        nav = [f'Source = Oracle.Database("{server}", [HierarchicalNavigation = true])',
               f'SchemaNav = Source{{[Schema = "{schema}"]}}[Data]',
               f'TableNav = SchemaNav{{[Name = "{spec["oracle"]}"]}}[Data]',
               f"Typed = Table.TransformColumnTypes(TableNav, {{\n{indent(types, 8)}\n    }})"]
    else:
        nav = [f"Source = Excel.Workbook(File.Contents({excel_param}), null, true)",
               f'SheetNav = Source{{[Item = "{spec["sheet"]}", Kind = "Sheet"]}}[Data]',
               "Promoted = Table.PromoteHeaders(SheetNav, [PromoteAllScalars = true])",
               f"Typed = Table.TransformColumnTypes(Promoted, {{\n{indent(types, 8)}\n    }})"]
    return "let\n" + ",\n".join("    " + s for s in nav) + "\nin\n    Typed"


def table_tmdl(name: str, spec: dict) -> str:
    out = [f"/// {spec['description']}", f"table {q(name)}", f"\tlineageTag: {tag(name)}", ""]
    for t, mname, expr, fmt, desc in MEASURES:
        if t == name:
            out += [f"\t/// {desc}", f"\tmeasure {q(mname)} = {expr}", f"\t\tformatString: {fmt}",
                    f"\t\tlineageTag: {tag(name, 'measure', mname)}", ""]
    for col, src, (mt, dtype), opt in spec["columns"]:
        if opt.get("description"):
            out.append(f"\t/// {opt['description']}")
        out += [f"\tcolumn {q(col)}", f"\t\tdataType: {dtype}"]
        if opt.get("hidden"):
            out.append("\t\tisHidden")
        if opt.get("format"):
            out.append(f"\t\tformatString: {opt['format']}")
        out += [f"\t\tlineageTag: {tag(name, col)}", f"\t\tsummarizeBy: {'sum' if dtype == 'decimal' else 'none'}",
                f"\t\tsourceColumn: {src}", "", "\t\tannotation SummarizationSetBy = Automatic"]
        if mt == "type date":
            out += ["", "\t\tannotation UnderlyingDateTimeDataType = Date"]
        out.append("")
    out += [f"\tpartition {q(name)} = m", "\t\tmode: import", "\t\tsource ="]
    out.append(tabs(m_partition(spec, "TargetWorkbook"), 4))
    out += ["", "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(out)


def build_model(excel: Path) -> None:
    d = MODEL_DIR / "definition"
    reset_dir(d)
    (d / "tables").mkdir()
    write_json(MODEL_DIR / "definition.pbism", {
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {"qnaEnabled": False}})
    write(d / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n\tcompatibilityMode: powerBI\n")
    write(d / "model.tmdl", "\n".join([
        "model Model", "\tculture: zh-TW", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: zh-TW", "\tdataAccessOptions", "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "", *[f"ref table {q(t)}" for t in TABLES], ""]))
    path = str(excel).replace('"', '""')
    write(d / "expressions.tmdl", "\n".join([
        "/// 財務部目標活頁簿的完整路徑",
        f'expression TargetWorkbook = "{path}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {tag('expr', 'TargetWorkbook')}", "", "\tannotation PBI_ResultType = Text", ""]))
    rels = []
    for ft, fc, tt, tc, card in RELATIONSHIPS:
        rels.append(f"relationship {tag('rel', ft, fc, tt)}\n" + (
            "\ttoCardinality: many\n\tcrossFilteringBehavior: bothDirections\n" if card == "many" else "") +
            f"\tfromColumn: {q(ft)}.{q(fc)}\n\ttoColumn: {q(tt)}.{q(tc)}\n")
    write(d / "relationships.tmdl", "\n".join(rels))
    for name, spec in TABLES.items():
        write(d / "tables" / f"{name}.tmdl", table_tmdl(name, spec))


# --------------------------------------------------------------------------------------
# Report (PBIR): formatting objects in the same shape Power BI Desktop writes them
# --------------------------------------------------------------------------------------
def field(kind: str, table: str, name: str) -> dict:
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name, "active": True}


def M(name: str) -> dict:
    return field("Measure", "門市月目標" if name in ("目標營收", "達成率", "目標差額") else "銷售", name)


def C(table: str, name: str) -> dict:
    return field("Column", table, name)


def cmp(kind: int, ref: dict, value: str) -> dict:
    return {"Comparison": {"ComparisonKind": kind, "Left": ref["field"], "Right": {"Literal": {"Value": value}}}}


def rule_color(prop: str, ref: dict, cases: list[tuple[dict, str]]) -> list:
    """Rules-based conditional formatting of one field (values / backColor, fontColor …)."""
    table = ref["queryRef"]
    return [{"properties": {prop: {"solid": {"color": {"expr": {"Conditional": {"Cases": [
        {"Condition": cond, "Value": {"Literal": {"Value": f"'{color}'"}}} for cond, color in cases]}}}}}},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": table}}]


def constant_line(name: str, value: str, color: str) -> list:
    return [{"properties": {"show": literal("true"), "displayName": literal(f"'{name}'"), "value": literal(value),
                            "lineColor": {"solid": {"color": literal(f"'{color}'")}}, "style": literal("'dashed'"),
                            "dataLabelShow": literal("true")}, "selector": {"id": "1"}}]


def member_color(table: str, column: str, member: str, color: str) -> list:
    return [{"properties": {"fill": {"solid": {"color": literal(f"'{color}'")}}},
             "selector": {"data": [{"scopeId": {"Comparison": {"ComparisonKind": 0, "Left": {
                 "Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}},
                 "Right": {"Literal": {"Value": f"'{member}'"}}}}}]}}]


RED, YELLOW, GREEN = "#E81123", "#F2C80F", "#3BB44A"


def pages() -> list[tuple[str, str, list]]:
    rate = M("達成率")
    rate_rules = rule_color("backColor", rate, [
        (cmp(3, rate, "0.9D"), RED),
        ({"And": {"Left": cmp(2, rate, "0.9D"), "Right": cmp(3, rate, "1D")}}, YELLOW),
        (cmp(2, rate, "1D"), GREEN)])
    margin = M("毛利率")
    return [
        ("achievement", "目標達成", [
            textbox("title", "門市目標達成（財務部）"),
            visual("kpi", "cardVisual", 20, 80, 1240, 120, 200,
                   {"Data": [M("營收"), M("目標營收"), M("達成率"), M("目標差額")]}),
            visual("storeTable", "tableEx", 20, 215, 760, 495, 300,
                   {"Values": [C("門市", "門市名稱"), M("營收"), M("目標營收"), M("達成率")]},
                   title="各門市達成率", objects={"values": rate_rules}),
            visual("regionRate", "clusteredColumnChart", 800, 215, 460, 240, 400,
                   {"Category": [C("門市", "區域")], "Y": [rate]}, title="各區域達成率",
                   objects={"y1AxisReferenceLine": constant_line("目標 100%", "1D", RED)}),
            visual("regionSales", "clusteredColumnChart", 800, 470, 460, 240, 500,
                   {"Category": [C("門市", "區域")], "Y": [M("營收")]}, title="各區域營收",
                   objects={"dataPoint": member_color("門市", "區域", "東區", RED)}),
        ]),
        ("margin", "毛利", [
            textbox("title", "毛利"),
            visual("marginTrend", "lineChart", 20, 80, 1240, 300, 200,
                   {"Category": [C("日期", "年月")], "Y": [margin]}, title="每月毛利率",
                   objects={"y1AxisReferenceLine": constant_line("毛利率底線 45%", "0.45D", RED)},
                   sort_by=(C("日期", "年月"), "Ascending")),
            visual("marginTable", "tableEx", 20, 400, 1240, 310, 300,
                   {"Values": [C("門市", "門市名稱"), M("營收"), M("毛利"), margin, M("銷售額"), M("訂單數")]},
                   title="各門市毛利", objects={"values": rule_color("fontColor", margin, [
                       (cmp(3, margin, "0.45D"), RED)])}),
        ]),
    ]


def build_report() -> None:
    d = REPORT_DIR / "definition"
    reset_dir(d)
    write_json(REPORT_DIR / "definition.pbir", {
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    theme_dir = REPORT_DIR / "StaticResources" / "SharedResources" / "BaseThemes"
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "CY24SU10.json").write_bytes(
        (HERE.parent.parent / "powerbi" / "_template" / "CY24SU10.json").read_bytes())
    write_json(d / "version.json", {"$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
                                    "version": "2.0.0"})
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": "SharedResources",
                                          "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97",
                                                                    "page": "1.3.97"}}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources", "items": [
            {"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]}]})
    all_pages = pages()
    write_json(d / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p[0] for p in all_pages], "activePageName": all_pages[0][0]})
    for page_name, display, visuals in all_pages:
        pdir = d / "pages" / page_name
        write_json(pdir / "page.json", {
            "$schema": f"{SCHEMA}/item/report/definition/page/1.4.0/schema.json",
            "name": page_name, "displayName": display, "displayOption": "FitToPage", "height": 720, "width": 1280})
        for vname, body in visuals:
            write_json(pdir / "visuals" / vname / "visual.json", body)
    write_json(OUT / f"{NAME}.pbip", {
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--excel", type=Path, default=DEFAULT_EXCEL)
    args = p.parse_args()
    build_model(args.excel.resolve())
    build_report()
    print(f"Generated {OUT / (NAME + '.pbip')}  (TargetWorkbook = {args.excel.resolve()})")
