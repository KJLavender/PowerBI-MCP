"""Generate the Superstore Power BI project (PBIP) on top of the Excel workbook.

    python datasets/superstore/build_pbip.py [--excel PATH]

This is what an analyst would build in Power BI Desktop from an Excel file: three sheets imported
with Power Query (Orders / People / Returns), two relationships, measures with Chinese names and a
four-page report. The workbook path is a Power Query parameter (ExcelPath), absolute as Power BI requires;
re-run the script after moving the repo. Default: data/GlobalSuperstore.xlsx (the version with the injected
scenarios, see prepare.py).
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "powerbi"))
from build_pbip import (SCHEMA, column, indent, literal, measure, q, reset_dir, tabs,  # noqa: E402
                        textbox, visual, write, write_json)
from build_pbip import tag as _tag  # noqa: E402

NAME = "Superstore"
OUT = HERE / "powerbi"
MODEL_DIR = OUT / f"{NAME}.SemanticModel"
REPORT_DIR = OUT / f"{NAME}.Report"
DEFAULT_EXCEL = HERE / "data" / "GlobalSuperstore.xlsx"


def tag(*parts: str) -> str:
    return _tag(NAME, *parts)


# column spec: (name, M type, TMDL dataType, options) — names exactly as the Excel headers
INT, DEC, DATE, TXT = ("Int64.Type", "int64"), ("type number", "double"), ("type date", "dateTime"), \
    ("type text", "string")

TABLES = {
    "Orders": {
        "sheet": "Orders",
        "description": "訂單明細（Excel 工作表 Orders，一列 = 一筆訂單中的一項商品）",
        "columns": [
            ("Row ID", INT, {"hidden": True}),
            ("Order ID", TXT, {"description": "訂單編號"}),
            ("Order Date", DATE, {"format": "yyyy-mm-dd", "description": "訂單日期"}),
            ("Ship Date", DATE, {"format": "yyyy-mm-dd", "description": "出貨日期"}),
            ("Ship Mode", TXT, {"description": "運送方式"}),
            ("Customer ID", TXT, {"description": "顧客編號"}),
            ("Customer Name", TXT, {"description": "顧客名稱"}),
            ("Segment", TXT, {"description": "顧客分群：Consumer / Corporate / Home Office"}),
            ("Postal Code", INT, {"hidden": True, "summarize": "none"}),
            ("City", TXT, {"description": "城市"}),
            ("State", TXT, {"description": "州 / 省"}),
            ("Country", TXT, {"description": "國家"}),
            ("Region", TXT, {"description": "地區（銷售區）"}),
            ("Market", TXT, {"description": "市場：Asia Pacific / Europe / USCA / LATAM / Africa"}),
            ("Product ID", TXT, {"description": "商品編號"}),
            ("Category", TXT, {"description": "品類"}),
            ("Sub-Category", TXT, {"description": "子品類"}),
            ("Product Name", TXT, {"description": "商品名稱"}),
            ("Sales", DEC, {"format": "#,##0.00", "description": "折扣後銷售額"}),
            ("Quantity", INT, {"format": "#,##0", "description": "數量"}),
            ("Discount", DEC, {"format": "0%", "summarize": "none", "description": "折扣率（0.2 = 打八折）"}),
            ("Profit", DEC, {"format": "#,##0.00", "description": "利潤"}),
            ("Shipping Cost", DEC, {"format": "#,##0.00", "description": "運費"}),
            ("Order Priority", TXT, {"description": "訂單優先度"}),
        ],
        # Power Query step added by the analyst: month for the monthly charts
        "added": [("Order Month", DATE, "Date.StartOfMonth([Order Date])",
                   {"format": "yyyy-mm", "description": "訂單月份（Power Query 新增欄位）"})],
    },
    "People": {
        "sheet": "People",
        "description": "各地區負責人（Excel 工作表 People）",
        "columns": [("Person", TXT, {"description": "負責人"}), ("Region", TXT, {"description": "地區"})],
    },
    "Returns": {
        "sheet": "Returns",
        "description": "退貨訂單（Excel 工作表 Returns）",
        "columns": [("Returned", TXT, {"description": "是否退貨"}), ("Order ID", TXT, {"description": "訂單編號"}),
                    ("Region", TXT, {"hidden": True})],
    },
}

RELATIONSHIPS = [
    ("Orders", "Region", "People", "Region"),
    ("Orders", "Order ID", "Returns", "Order ID"),
]

PCT, MONEY, NUM = "0.0%;-0.0%;0.0%", "#,##0", "#,##0"
MONEY2 = "#,##0.0"
# table, name, DAX, format, folder, description
MEASURES = [
    ("Orders", "銷售額", "SUM ( Orders[Sales] )", MONEY, "基本指標", "折扣後銷售額"),
    ("Orders", "利潤", "SUM ( Orders[Profit] )", MONEY, "基本指標", "利潤"),
    ("Orders", "利潤率", "DIVIDE ( [利潤], [銷售額] )", PCT, "基本指標", "利潤 ÷ 銷售額"),
    ("Orders", "成本", "[銷售額] - [利潤]", MONEY, "基本指標", "銷售額 − 利潤（含運費等所有成本）"),
    ("Orders", "銷售數量", "SUM ( Orders[Quantity] )", NUM, "基本指標", "商品件數"),
    ("Orders", "訂單數", "DISTINCTCOUNT ( Orders[Order ID] )", NUM, "基本指標", "不重複訂單數"),
    ("Orders", "客單價", "DIVIDE ( [銷售額], [訂單數] )", MONEY2, "基本指標", "平均每筆訂單銷售額 = 銷售額 ÷ 訂單數"),
    ("Orders", "折扣前金額", "SUMX ( Orders, Orders[Sales] / ( 1 - Orders[Discount] ) )", MONEY, "折扣",
     "依折扣率還原的原價金額"),
    ("Orders", "折扣金額", "[折扣前金額] - [銷售額]", MONEY, "折扣", "折扣前金額 − 銷售額"),
    ("Orders", "折扣率", "DIVIDE ( [折扣金額], [折扣前金額] )", PCT, "折扣", "折扣金額 ÷ 折扣前金額"),
    ("Orders", "平均標價", "DIVIDE ( [折扣前金額], [銷售數量] )", MONEY2, "基本指標", "折扣前平均每件售價"),
    ("Orders", "運費", "SUM ( Orders[Shipping Cost] )", MONEY, "基本指標", "運費"),
    ("Orders", "退貨訂單數", "CALCULATE ( [訂單數], Returns[Returned] = \"Yes\" )", NUM, "退貨",
     "有退貨紀錄的訂單數"),
    ("Orders", "退貨率", "DIVIDE ( [退貨訂單數], [訂單數] )", PCT, "退貨", "退貨訂單數 ÷ 訂單數"),
    ("Orders", "銷售額 去年同期", "CALCULATE ( [銷售額], SAMEPERIODLASTYEAR ( Orders[Order Date] ) )", MONEY,
     "時間比較", "去年同期銷售額"),
]


def m_partition(name: str, spec: dict) -> str:
    types = ",\n".join(f'{{"{c}", {mt}}}' for c, (mt, _), _ in spec["columns"])
    steps = [
        "Source = Excel.Workbook(File.Contents(ExcelPath), null, true)",
        f'SheetNav = Source{{[Item = "{spec["sheet"]}", Kind = "Sheet"]}}[Data]',
        "Promoted = Table.PromoteHeaders(SheetNav, [PromoteAllScalars = true])",
        f"Typed = Table.TransformColumnTypes(Promoted, {{\n{indent(types, 8)}\n    }})",
    ]
    last = "Typed"
    for col, (mt, _), expr, _ in spec.get("added", []):
        steps.append(f'Added = Table.AddColumn({last}, "{col}", each {expr}, {mt})')
        last = "Added"
    return "let\n" + ",\n".join("    " + s for s in steps) + f"\nin\n    {last}"


def table_tmdl(name: str, spec: dict) -> str:
    out = [f"/// {spec['description']}", f"table {q(name)}", f"\tlineageTag: {tag(name)}", ""]
    for t, mname, expr, fmt, folder, desc in MEASURES:
        if t == name:
            out += [f"\t/// {desc}", f"\tmeasure {q(mname)} = {expr}", f"\t\tformatString: {fmt}",
                    f"\t\tdisplayFolder: {folder}", f"\t\tlineageTag: {tag(name, 'measure', mname)}", ""]
    cols = [(c, t, o) for c, t, o in spec["columns"]] + [(c, t, o) for c, t, _, o in spec.get("added", [])]
    for col, (mt, dtype), opt in cols:
        if opt.get("description"):
            out.append(f"\t/// {opt['description']}")
        out += [f"\tcolumn {q(col)}", f"\t\tdataType: {dtype}"]
        if opt.get("hidden"):
            out.append("\t\tisHidden")
        if opt.get("format"):
            out.append(f"\t\tformatString: {opt['format']}")
        out.append(f"\t\tlineageTag: {tag(name, col)}")
        numeric = dtype in ("int64", "double") and not opt.get("hidden")
        out.append(f"\t\tsummarizeBy: {opt.get('summarize', 'sum' if numeric else 'none')}")
        out.append(f"\t\tsourceColumn: {col}")
        out += ["", "\t\tannotation SummarizationSetBy = Automatic"]
        if mt == "type date":
            out += ["", "\t\tannotation UnderlyingDateTimeDataType = Date"]
        out.append("")
    out += [f"\tpartition {q(name)} = m", "\t\tmode: import", "\t\tsource ="]
    out.append(tabs(m_partition(name, spec), 4))
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
    query_order = json.dumps(["ExcelPath", *TABLES], ensure_ascii=False)
    write(d / "model.tmdl", "\n".join([
        "model Model", "\tculture: zh-TW", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: zh-TW", "\tdataAccessOptions", "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull", "",
        f"annotation PBI_QueryOrder = {query_order}", "", "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in TABLES], ""]))
    path = str(excel).replace('"', '""')
    write(d / "expressions.tmdl", "\n".join([
        "/// Excel 活頁簿的完整路徑",
        f'expression ExcelPath = "{path}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {tag('expr', 'ExcelPath')}", "", "\tannotation PBI_ResultType = Text", ""]))
    write(d / "relationships.tmdl", "\n".join(
        f"relationship {tag('rel', ft, fc)}\n\tfromColumn: {q(ft)}.{q(fc)}\n\ttoColumn: {q(tt)}.{q(tc)}\n"
        for ft, fc, tt, tc in RELATIONSHIPS))
    for name, spec in TABLES.items():
        write(d / "tables" / f"{name}.tmdl", table_tmdl(name, spec))


# --------------------------------------------------------------------------------------
# Report (PBIR)
# --------------------------------------------------------------------------------------
M = lambda n: measure("Orders", n)  # noqa: E731
C = lambda n: column("Orders", n)  # noqa: E731


def date_slicer():
    return visual("dateSlicer", "slicer", 960, 10, 300, 60, 100, {"Values": [C("Order Date")]},
                  objects={"data": [{"properties": {"mode": literal("'Between'")}}]})


def pages() -> list[tuple[str, str, list]]:
    month = C("Order Month")
    return [
        ("overview", "總覽", [
            textbox("title", "Global Superstore 總覽"), date_slicer(),
            visual("kpiCards", "cardVisual", 20, 80, 1240, 120, 200,
                   {"Data": [M("銷售額"), M("利潤"), M("利潤率"), M("訂單數"), M("客單價")]},
                   objects={"value": [{"properties": {"labelDisplayUnits": literal("1D"), "fontSize": literal("24D")},
                                       "selector": {"id": "default"}}]}),
            visual("salesByMarket", "lineChart", 20, 215, 820, 240, 300,
                   {"Category": [month], "Series": [C("Market")], "Y": [M("銷售額")]},
                   title="每月銷售額（依市場）", sort_by=(month, "Ascending")),
            visual("salesByCategory", "barChart", 860, 215, 400, 240, 400,
                   {"Category": [C("Category")], "Y": [M("銷售額")]}, title="各品類銷售額"),
            visual("marginByMarket", "columnChart", 20, 470, 620, 240, 500,
                   {"Category": [C("Market")], "Y": [M("利潤率")]}, title="各市場利潤率"),
            visual("salesByPerson", "barChart", 660, 470, 600, 240, 600,
                   {"Category": [column("People", "Person")], "Y": [M("銷售額")]}, title="各負責人銷售額"),
        ]),
        ("market", "市場與地區", [
            textbox("title", "市場與地區"), date_slicer(),
            visual("geoMatrix", "pivotTable", 20, 80, 760, 630, 200,
                   {"Rows": [C("Market"), C("Region"), C("Country")],
                    "Values": [M("銷售額"), M("利潤率"), M("訂單數"), M("折扣率"), M("運費")]}, title="市場績效"),
            visual("regionOrders", "lineChart", 800, 80, 460, 300, 300,
                   {"Category": [month], "Series": [C("Market")], "Y": [M("訂單數")]},
                   title="每月訂單數（依市場）", sort_by=(month, "Ascending")),
            visual("returnsByMarket", "barChart", 800, 400, 460, 310, 400,
                   {"Category": [C("Market")], "Y": [M("退貨率")]}, title="各市場退貨率"),
        ]),
        ("product", "商品", [
            textbox("title", "商品分析"), date_slicer(),
            visual("categoryMargin", "lineChart", 20, 80, 620, 300, 200,
                   {"Category": [month], "Series": [C("Category")], "Y": [M("利潤率")]},
                   title="各品類每月利潤率", sort_by=(month, "Ascending")),
            visual("categoryQty", "lineChart", 660, 80, 600, 300, 300,
                   {"Category": [month], "Series": [C("Category")], "Y": [M("銷售數量")]},
                   title="各品類每月銷售數量", sort_by=(month, "Ascending")),
            visual("productMatrix", "pivotTable", 20, 400, 1240, 310, 400,
                   {"Rows": [C("Category"), C("Sub-Category")],
                    "Values": [M("銷售額"), M("銷售數量"), M("平均標價"), M("折扣率"), M("利潤率")]},
                   title="品類與子品類"),
        ]),
        ("customer", "顧客與運送", [
            textbox("title", "顧客與運送"), date_slicer(),
            visual("segmentOrders", "columnChart", 20, 80, 1240, 300, 200,
                   {"Category": [month], "Series": [C("Segment")], "Y": [M("訂單數")]},
                   title="每月訂單數（依顧客分群）", sort_by=(month, "Ascending")),
            visual("shipMode", "barChart", 20, 400, 600, 310, 300,
                   {"Category": [C("Ship Mode")], "Y": [M("銷售額"), M("運費")]}, title="運送方式"),
            visual("discountBySegment", "lineChart", 640, 400, 620, 310, 400,
                   {"Category": [month], "Series": [C("Segment")], "Y": [M("折扣率")]},
                   title="每月折扣率（依顧客分群）", sort_by=(month, "Ascending")),
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
    (theme_dir / "CY24SU10.json").write_bytes((HERE.parent.parent / "powerbi" / "_template" / "CY24SU10.json").read_bytes())
    write_json(d / "version.json", {"$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
                                    "version": "2.0.0"})
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": "SharedResources",
                                          "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"}}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources", "items": [
            {"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultFilterActionIsDataFilter": True,
                     "defaultDrillFilterOtherVisuals": True, "allowChangeFilterTypes": True, "useEnhancedTooltips": True}})
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
    print(f"Generated {OUT / (NAME + '.pbip')}  (ExcelPath = {args.excel.resolve()})")
