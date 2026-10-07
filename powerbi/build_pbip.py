"""Generate the RetailSales Power BI project (PBIP: TMDL semantic model + PBIR report).

    python powerbi/build_pbip.py

Re-running overwrites the generated definition files. Open powerbi/RetailSales.pbip in
Power BI Desktop, then Refresh and enter the Oracle credentials once.
"""
import json
import shutil
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "RetailSales"
MODEL_DIR = HERE / f"{NAME}.SemanticModel"
REPORT_DIR = HERE / f"{NAME}.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
NS = uuid.UUID("6f1c0d1e-8a1b-4c55-9a7e-2d4b6c8e0f11")


def tag(*parts: str) -> str:
    """Stable lineage tag so regenerated files diff cleanly."""
    return str(uuid.uuid5(NS, "/".join(parts)))


# --------------------------------------------------------------------------------------
# Semantic model definition
# --------------------------------------------------------------------------------------
# column spec: (name, M type, TMDL dataType, options)
INT, DEC, DATE, DT, TXT = ("Int64.Type", "int64"), ("Currency.Type", "decimal"), \
    ("type date", "dateTime"), ("type datetime", "dateTime"), ("type text", "string")

TABLES = {
    "FACT_SALES": {
        "description": "銷售明細（訂單明細層級，一列 = 一筆訂單中的一項商品）",
        "columns": [
            ("ORDER_ID", INT, {"hidden": True, "description": "訂單編號"}),
            ("LINE_NO", INT, {"hidden": True}),
            ("DATE_KEY", INT, {"hidden": True}),
            ("STORE_ID", INT, {"hidden": True}),
            ("CHANNEL_ID", INT, {"hidden": True}),
            ("PRODUCT_ID", INT, {"hidden": True}),
            ("CUSTOMER_ID", INT, {"hidden": True}),
            ("QUANTITY", INT, {"format": "#,##0", "description": "銷售數量"}),
            ("UNIT_PRICE", DEC, {"format": "#,##0", "description": "成交單價（未扣折扣）"}),
            ("DISCOUNT_AMOUNT", DEC, {"format": "#,##0", "description": "整行折扣金額"}),
            ("SALES_AMOUNT", DEC, {"format": "#,##0", "description": "折扣後淨銷售額 = 數量 × 單價 − 折扣"}),
            ("COST_AMOUNT", DEC, {"format": "#,##0", "description": "銷貨成本"}),
        ],
    },
    "DIM_DATE": {
        "description": "日期維度（已標記為日期資料表）",
        "date_table": "FULL_DATE",
        "columns": [
            ("DATE_KEY", INT, {"hidden": True}),
            ("FULL_DATE", DATE, {"format": "yyyy-mm-dd", "key": True, "description": "日期"}),
            ("YEAR_NUM", INT, {"format": "0", "description": "年"}),
            ("QUARTER_NUM", INT, {"format": "0", "description": "季"}),
            ("MONTH_NUM", INT, {"format": "0", "description": "月"}),
            ("YEAR_MONTH", TXT, {"description": "年月，例如 2026-10"}),
            ("ISO_WEEK", INT, {"format": "0", "description": "ISO 週次"}),
            ("DAY_OF_WEEK", INT, {"hidden": True, "description": "星期幾（1=週一）"}),
            ("WEEKDAY_NAME", TXT, {"sort_by": "DAY_OF_WEEK", "description": "星期名稱"}),
            ("IS_WEEKEND", TXT, {"description": "是否為週末 Y/N"}),
            ("HOLIDAY_NAME", TXT, {"description": "節日或促銷檔期名稱"}),
        ],
    },
    "DIM_STORE": {
        "description": "門市。線上訂單依顧客所在區域歸屬到門市",
        "columns": [
            ("STORE_ID", INT, {"hidden": True}),
            ("STORE_CODE", TXT, {"description": "門市代碼"}),
            ("STORE_NAME", TXT, {"description": "門市名稱"}),
            ("CITY", TXT, {"description": "縣市"}),
            ("REGION_CODE", TXT, {"hidden": True}),
            ("REGION_NAME", TXT, {"description": "區域：北區 / 中區 / 南區 / 東區"}),
            ("STORE_TYPE", TXT, {"description": "店型：旗艦店 / 標準店 / 社區店"}),
            ("FLOOR_AREA_PING", INT, {"format": "#,##0", "summarize": "none", "description": "賣場坪數"}),
            ("OPEN_DATE", DATE, {"format": "yyyy-mm-dd", "description": "開幕日"}),
        ],
    },
    "DIM_CHANNEL": {
        "description": "銷售通路",
        "columns": [
            ("CHANNEL_ID", INT, {"hidden": True}),
            ("CHANNEL_CODE", TXT, {"hidden": True}),
            ("CHANNEL_NAME", TXT, {"description": "通路：門市 / 官網 / 電商平台"}),
            ("CHANNEL_TYPE", TXT, {"description": "實體 / 線上"}),
        ],
    },
    "DIM_PRODUCT": {
        "description": "商品",
        "columns": [
            ("PRODUCT_ID", INT, {"hidden": True}),
            ("PRODUCT_CODE", TXT, {"description": "商品代碼"}),
            ("PRODUCT_NAME", TXT, {"description": "商品名稱"}),
            ("CATEGORY", TXT, {"description": "商品大類"}),
            ("BRAND", TXT, {"description": "品牌"}),
            ("LIST_PRICE", DEC, {"format": "#,##0", "summarize": "none", "description": "牌價"}),
            ("STD_COST", DEC, {"format": "#,##0", "summarize": "none", "description": "標準成本（主檔，實際成本以 FACT_SALES.COST_AMOUNT 為準）"}),
        ],
    },
    "DIM_CUSTOMER": {
        "description": "顧客。每個區域有一筆「非會員」虛擬顧客，代表未登入會員的門市消費",
        "columns": [
            ("CUSTOMER_ID", INT, {"hidden": True}),
            ("CUSTOMER_CODE", TXT, {"description": "顧客代碼"}),
            ("SEGMENT", TXT, {"description": "顧客分群：非會員 / 一般會員 / VIP"}),
            ("GENDER", TXT, {"description": "性別"}),
            ("AGE_GROUP", TXT, {"description": "年齡層"}),
            ("HOME_REGION_NAME", TXT, {"description": "會員居住區域"}),
            ("JOIN_DATE", DATE, {"format": "yyyy-mm-dd", "description": "入會日"}),
        ],
    },
    "ETL_BATCH_LOG": {
        "description": "ETL 每日批次紀錄：每個營業日載入的筆數與狀態",
        "columns": [
            ("BUSINESS_DATE_KEY", INT, {"hidden": True}),
            ("RUN_AT", DT, {"format": "yyyy-mm-dd hh:nn", "description": "批次執行時間"}),
            ("ROWS_LOADED", INT, {"format": "#,##0", "description": "ETL 回報的載入筆數"}),
            ("STATUS", TXT, {"description": "批次狀態"}),
            ("MESSAGE", TXT, {"description": "批次訊息"}),
        ],
    },
}

RELATIONSHIPS = [
    ("FACT_SALES", "DATE_KEY", "DIM_DATE", "DATE_KEY"),
    ("FACT_SALES", "STORE_ID", "DIM_STORE", "STORE_ID"),
    ("FACT_SALES", "CHANNEL_ID", "DIM_CHANNEL", "CHANNEL_ID"),
    ("FACT_SALES", "PRODUCT_ID", "DIM_PRODUCT", "PRODUCT_ID"),
    ("FACT_SALES", "CUSTOMER_ID", "DIM_CUSTOMER", "CUSTOMER_ID"),
    ("ETL_BATCH_LOG", "BUSINESS_DATE_KEY", "DIM_DATE", "DATE_KEY"),
]

PCT, MONEY, NUM, MONEY2 = "0.0%;-0.0%;0.0%", "#,##0", "#,##0", "#,##0.0"
# table, name, expression, format, display folder, description
MEASURES = [
    ("FACT_SALES", "銷售額", "SUM ( FACT_SALES[SALES_AMOUNT] )", MONEY, "基本指標",
     "折扣後淨銷售額"),
    ("FACT_SALES", "成本", "SUM ( FACT_SALES[COST_AMOUNT] )", MONEY, "基本指標", "銷貨成本"),
    ("FACT_SALES", "毛利", "[銷售額] - [成本]", MONEY, "基本指標", "銷售額 − 成本"),
    ("FACT_SALES", "毛利率", "DIVIDE ( [毛利], [銷售額] )", PCT, "基本指標", "毛利 ÷ 銷售額"),
    ("FACT_SALES", "訂單數", "DISTINCTCOUNT ( FACT_SALES[ORDER_ID] )", NUM, "基本指標",
     "不重複訂單數（來客數）"),
    ("FACT_SALES", "銷售數量", "SUM ( FACT_SALES[QUANTITY] )", NUM, "基本指標", "商品銷售件數"),
    ("FACT_SALES", "客單價", "DIVIDE ( [銷售額], [訂單數] )", MONEY2, "基本指標",
     "平均每筆訂單銷售額 = 銷售額 ÷ 訂單數"),
    ("FACT_SALES", "平均成交單價",
     "DIVIDE ( SUMX ( FACT_SALES, FACT_SALES[UNIT_PRICE] * FACT_SALES[QUANTITY] ), [銷售數量] )",
     MONEY2, "基本指標", "折扣前平均每件售價"),
    ("FACT_SALES", "折扣金額", "SUM ( FACT_SALES[DISCOUNT_AMOUNT] )", MONEY, "基本指標", "折扣總額"),
    ("FACT_SALES", "折扣率", "DIVIDE ( [折扣金額], [銷售額] + [折扣金額] )", PCT, "基本指標",
     "折扣金額 ÷ 折扣前金額"),
    ("FACT_SALES", "購買會員數",
     "CALCULATE ( DISTINCTCOUNT ( FACT_SALES[CUSTOMER_ID] ), DIM_CUSTOMER[SEGMENT] <> \"非會員\" )",
     NUM, "基本指標", "有消費的不重複會員數（不含非會員）"),
    ("FACT_SALES", "銷售額 前一日", "CALCULATE ( [銷售額], DATEADD ( DIM_DATE[FULL_DATE], -1, DAY ) )",
     MONEY, "時間比較", "前一天的銷售額"),
    ("FACT_SALES", "銷售額 上週同日", "CALCULATE ( [銷售額], DATEADD ( DIM_DATE[FULL_DATE], -7, DAY ) )",
     MONEY, "時間比較", "7 天前的銷售額（同星期比較）"),
    ("FACT_SALES", "銷售額 週變動%", "DIVIDE ( [銷售額] - [銷售額 上週同日], [銷售額 上週同日] )",
     PCT, "時間比較", "與上週同日相比的變動率"),
    ("FACT_SALES", "銷售額 去年同期",
     "CALCULATE ( [銷售額], SAMEPERIODLASTYEAR ( DIM_DATE[FULL_DATE] ) )", MONEY, "時間比較",
     "去年同期銷售額"),
    ("FACT_SALES", "銷售額 YoY%", "DIVIDE ( [銷售額] - [銷售額 去年同期], [銷售額 去年同期] )",
     PCT, "時間比較", "年增率"),
    ("FACT_SALES", "銷售額 MTD", "TOTALMTD ( [銷售額], DIM_DATE[FULL_DATE] )", MONEY, "時間比較",
     "本月累計銷售額"),
    ("FACT_SALES", "毛利率 上週同日",
     "CALCULATE ( [毛利率], DATEADD ( DIM_DATE[FULL_DATE], -7, DAY ) )", PCT, "時間比較",
     "7 天前的毛利率"),
    ("FACT_SALES", "訂單數 上週同日",
     "CALCULATE ( [訂單數], DATEADD ( DIM_DATE[FULL_DATE], -7, DAY ) )", NUM, "時間比較",
     "7 天前的訂單數"),
    ("ETL_BATCH_LOG", "ETL 載入筆數", "SUM ( ETL_BATCH_LOG[ROWS_LOADED] )", NUM, "資料品質",
     "ETL 批次回報的載入筆數"),
    ("ETL_BATCH_LOG", "實際明細筆數", "COUNTROWS ( FACT_SALES )", NUM, "資料品質",
     "FACT_SALES 實際列數，可與 ETL 載入筆數比對"),
]


def q(name: str) -> str:
    """Quote a TMDL object name when needed."""
    return name if name.replace("_", "").isalnum() and name.isascii() else "'" + name.replace("'", "''") + "'"


def m_partition(table: str, columns) -> str:
    types = ",\n".join(f'{{"{c}", {mt}}}' for c, (mt, _), _ in columns)
    # step names must not collide with navigation fields (Schema, Name, Data) or query names
    return (
        "let\n"
        "    Source = Oracle.Database(OracleServer, [HierarchicalNavigation = true]),\n"
        "    SchemaNav = Source{[Schema = OracleSchema]}[Data],\n"
        f'    TableNav = SchemaNav{{[Name = "{table}"]}}[Data],\n'
        f"    Typed = Table.TransformColumnTypes(TableNav, {{\n{indent(types, 8)}\n    }})\n"
        "in\n"
        "    Typed"
    )


def indent(text: str, n: int) -> str:
    return "\n".join(" " * n + line if line else line for line in text.splitlines())


def tabs(text: str, n: int) -> str:
    return "\n".join("\t" * n + line if line else line for line in text.splitlines())


def table_tmdl(name: str, spec: dict) -> str:
    out = [f"/// {spec['description']}", f"table {q(name)}", f"\tlineageTag: {tag(name)}"]
    if spec.get("date_table"):
        out.append("\tdataCategory: Time")
    out.append("")
    for t, mname, expr, fmt, folder, desc in MEASURES:
        if t != name:
            continue
        out += [f"\t/// {desc}", f"\tmeasure {q(mname)} = {expr}", f"\t\tformatString: {fmt}",
                f"\t\tdisplayFolder: {folder}", f"\t\tlineageTag: {tag(name, 'measure', mname)}", ""]
    for col, (_mt, dtype), opt in spec["columns"]:
        if opt.get("description"):
            out.append(f"\t/// {opt['description']}")
        out += [f"\tcolumn {q(col)}", f"\t\tdataType: {dtype}"]
        if opt.get("key"):
            out.append("\t\tisKey")
        if opt.get("hidden"):
            out.append("\t\tisHidden")
        if opt.get("format"):
            out.append(f"\t\tformatString: {opt['format']}")
        out.append(f"\t\tlineageTag: {tag(name, col)}")
        numeric_fact = dtype in ("int64", "decimal") and name in ("FACT_SALES", "ETL_BATCH_LOG") \
            and not opt.get("hidden")
        out.append(f"\t\tsummarizeBy: {opt.get('summarize', 'sum' if numeric_fact else 'none')}")
        out.append(f"\t\tsourceColumn: {col}")
        if opt.get("sort_by"):
            out.append(f"\t\tsortByColumn: {opt['sort_by']}")
        out.append("")
        out.append("\t\tannotation SummarizationSetBy = Automatic")
        if _mt == "type date":
            out += ["", "\t\tannotation UnderlyingDateTimeDataType = Date"]
        out.append("")
    out += [f"\tpartition {q(name)} = m", "\t\tmode: import", "\t\tsource ="]
    out.append(tabs(m_partition(name, spec["columns"]), 4))
    out += ["", "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(out)


def reset_dir(d: Path) -> None:
    """Empty a generated folder; keep the folder itself so a shell/editor holding it can't block us."""
    d.mkdir(parents=True, exist_ok=True)
    for child in d.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def build_model() -> None:
    d = MODEL_DIR / "definition"
    reset_dir(d)
    (d / "tables").mkdir()
    write_json(MODEL_DIR / "definition.pbism", {
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2",
        "settings": {"qnaEnabled": False},
    })
    write(d / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n\tcompatibilityMode: powerBI\n")
    query_order = json.dumps(["OracleServer", "OracleSchema", *TABLES], ensure_ascii=False)
    write(d / "model.tmdl", "\n".join([
        "model Model",
        "\tculture: zh-TW",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: zh-TW",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
        f"annotation PBI_QueryOrder = {query_order}",
        "",
        "annotation __PBI_TimeIntelligenceEnabled = 0",
        "",
        *[f"ref table {q(t)}" for t in TABLES],
        "",
    ]))
    write(d / "expressions.tmdl", "\n".join([
        "/// Oracle 連線字串（host:port/service）",
        'expression OracleServer = "127.0.0.1:1521/FREEPDB1" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {tag('expr', 'OracleServer')}",
        "",
        "\tannotation PBI_ResultType = Text",
        "",
        "/// Oracle schema 名稱",
        'expression OracleSchema = "SALES_DW" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {tag('expr', 'OracleSchema')}",
        "",
        "\tannotation PBI_ResultType = Text",
        "",
    ]))
    write(d / "relationships.tmdl", "\n".join(
        f"relationship {tag('rel', ft, fc)}\n\tfromColumn: {ft}.{fc}\n\ttoColumn: {tt}.{tc}\n"
        for ft, fc, tt, tc in RELATIONSHIPS))
    for name, spec in TABLES.items():
        write(d / "tables" / f"{name}.tmdl", table_tmdl(name, spec))


# --------------------------------------------------------------------------------------
# Report definition (PBIR)
# --------------------------------------------------------------------------------------
VISUAL_SCHEMA = f"{SCHEMA}/item/report/definition/visualContainer/2.1.0/schema.json"


def measure(table: str, name: str) -> dict:
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name}


def column(table: str, name: str) -> dict:
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name, "active": True}


M = lambda n: measure("FACT_SALES", n)  # noqa: E731
literal = lambda v: {"expr": {"Literal": {"Value": v}}}  # noqa: E731


def visual(name, vtype, x, y, w, h, z, roles=None, title=None, sort_by=None, objects=None):
    v = {"visualType": vtype}
    if roles:
        v["query"] = {"queryState": {r: {"projections": p} for r, p in roles.items()}}
        if sort_by:
            field, direction = sort_by
            v["query"]["sortDefinition"] = {
                "sort": [{"field": field["field"], "direction": direction}], "isDefaultSort": True}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {
            "show": literal("true"), "text": literal(f"'{title}'")}}]}
    v["drillFilterOtherVisuals"] = True
    return name, {"$schema": VISUAL_SCHEMA, "name": name,
                  "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z},
                  "visual": v}


def textbox(name, text, x=20, y=10, w=900, h=50):
    return name, {"$schema": VISUAL_SCHEMA, "name": name,
                  "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0},
                  "visual": {"visualType": "textbox", "objects": {"general": [{"properties": {
                      "paragraphs": [{"textRuns": [{"value": text, "textStyle": {
                          "fontFamily": "Segoe (Bold)", "fontSize": "20pt", "color": "#252423"}}]}]}}]},
                      "drillFilterOtherVisuals": True}}


def date_slicer(name="dateSlicer", x=960, y=10, w=300, h=60):
    return visual(name, "slicer", x, y, w, h, 100, {"Values": [column("DIM_DATE", "FULL_DATE")]},
                  objects={"data": [{"properties": {"mode": literal("'Between'")}}]})


def pages() -> list[tuple[str, str, list]]:
    ym = column("DIM_DATE", "YEAR_MONTH")
    return [
        ("overview", "總覽", [
            textbox("title", "零售銷售總覽"),
            date_slicer(),
            visual("kpiCards", "cardVisual", 20, 80, 1240, 120, 200,
                   {"Data": [M("銷售額"), M("毛利率"), M("訂單數"), M("客單價")]},
                   # show full numbers (measure format strings) instead of auto units like "1 百萬"
                   objects={"value": [{"properties": {"labelDisplayUnits": literal("1D"),
                                                      "fontSize": literal("24D")},
                                       "selector": {"id": "default"}}]}),
            visual("dailySales", "lineChart", 20, 215, 820, 240, 300,
                   {"Category": [column("DIM_DATE", "FULL_DATE")],
                    "Y": [M("銷售額"), M("銷售額 上週同日")]}, title="每日銷售額 vs 上週同日"),
            visual("salesByRegion", "barChart", 860, 215, 400, 240, 400,
                   {"Category": [column("DIM_STORE", "REGION_NAME")], "Y": [M("銷售額")]},
                   title="各區域銷售額"),
            visual("salesByChannel", "columnChart", 20, 470, 600, 240, 500,
                   {"Category": [ym], "Series": [column("DIM_CHANNEL", "CHANNEL_NAME")],
                    "Y": [M("銷售額")]}, title="每月銷售額（依通路）", sort_by=(ym, "Ascending")),
            visual("salesByCategory", "barChart", 640, 470, 620, 240, 600,
                   {"Category": [column("DIM_PRODUCT", "CATEGORY")], "Y": [M("銷售額")]},
                   title="各品類銷售額"),
        ]),
        ("region", "區域與門市", [
            textbox("title", "區域與門市"),
            date_slicer(),
            visual("storeMatrix", "pivotTable", 20, 80, 760, 630, 200,
                   {"Rows": [column("DIM_STORE", "REGION_NAME"), column("DIM_STORE", "STORE_NAME")],
                    "Values": [M("銷售額"), M("銷售額 週變動%"), M("訂單數"), M("客單價"), M("毛利率")]},
                   title="門市績效"),
            visual("regionTrend", "lineChart", 800, 80, 460, 300, 300,
                   {"Category": [column("DIM_DATE", "FULL_DATE")],
                    "Series": [column("DIM_STORE", "REGION_NAME")], "Y": [M("銷售額")]},
                   title="各區域每日銷售額"),
            visual("storeTypeSales", "barChart", 800, 400, 460, 310, 400,
                   {"Category": [column("DIM_STORE", "STORE_TYPE")], "Y": [M("銷售額"), M("客單價")]},
                   title="店型比較"),
        ]),
        ("product", "商品", [
            textbox("title", "商品分析"),
            date_slicer(),
            visual("categoryMargin", "lineChart", 20, 80, 620, 300, 200,
                   {"Category": [ym], "Series": [column("DIM_PRODUCT", "CATEGORY")], "Y": [M("毛利率")]},
                   title="各品類每月毛利率", sort_by=(ym, "Ascending")),
            visual("categoryQty", "lineChart", 660, 80, 600, 300, 300,
                   {"Category": [ym], "Series": [column("DIM_PRODUCT", "CATEGORY")],
                    "Y": [M("銷售數量")]}, title="各品類每月銷售數量", sort_by=(ym, "Ascending")),
            visual("productMatrix", "pivotTable", 20, 400, 1240, 310, 400,
                   {"Rows": [column("DIM_PRODUCT", "CATEGORY"), column("DIM_PRODUCT", "PRODUCT_NAME")],
                    "Values": [M("銷售額"), M("銷售數量"), M("平均成交單價"), M("折扣率"), M("毛利率")]},
                   title="品類與商品"),
        ]),
        ("channel", "通路與顧客", [
            textbox("title", "通路與顧客"),
            date_slicer(),
            visual("channelDaily", "lineChart", 20, 80, 1240, 280, 200,
                   {"Category": [column("DIM_DATE", "FULL_DATE")],
                    "Series": [column("DIM_CHANNEL", "CHANNEL_NAME")], "Y": [M("訂單數")]},
                   title="各通路每日訂單數"),
            visual("channelDiscount", "lineChart", 20, 380, 600, 330, 300,
                   {"Category": [column("DIM_DATE", "FULL_DATE")],
                    "Series": [column("DIM_CHANNEL", "CHANNEL_NAME")], "Y": [M("折扣率")]},
                   title="各通路每日折扣率"),
            visual("segmentOrders", "columnChart", 640, 380, 620, 330, 400,
                   {"Category": [ym], "Series": [column("DIM_CUSTOMER", "SEGMENT")], "Y": [M("訂單數")]},
                   title="每月訂單數（依顧客分群）", sort_by=(ym, "Ascending")),
        ]),
        ("dataQuality", "資料品質", [
            textbox("title", "資料品質"),
            date_slicer(),
            visual("etlRows", "lineChart", 20, 80, 1240, 300, 200,
                   {"Category": [column("DIM_DATE", "FULL_DATE")],
                    "Y": [measure("ETL_BATCH_LOG", "ETL 載入筆數")]}, title="每日 ETL 載入筆數"),
            visual("etlTable", "tableEx", 20, 400, 1240, 310, 300,
                   {"Values": [column("DIM_DATE", "FULL_DATE"), column("ETL_BATCH_LOG", "STATUS"),
                               column("ETL_BATCH_LOG", "RUN_AT"),
                               measure("ETL_BATCH_LOG", "ETL 載入筆數"),
                               measure("ETL_BATCH_LOG", "實際明細筆數")]},
                   title="ETL 批次明細"),
        ]),
    ]


def build_report() -> None:
    d = REPORT_DIR / "definition"
    reset_dir(d)
    write_json(REPORT_DIR / "definition.pbir", {
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}},
    })
    theme_dir = REPORT_DIR / "StaticResources" / "SharedResources" / "BaseThemes"
    theme_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "_template" / "CY24SU10.json", theme_dir / "CY24SU10.json")
    write_json(d / "version.json", {
        "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0"})
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10",
            "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"},
            "type": "SharedResources"}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources", "items": [
            {"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultFilterActionIsDataFilter": True,
                     "defaultDrillFilterOtherVisuals": True, "allowChangeFilterTypes": True,
                     "useEnhancedTooltips": True},
    })
    all_pages = pages()
    write_json(d / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p[0] for p in all_pages], "activePageName": all_pages[0][0]})
    for page_name, display, visuals in all_pages:
        pdir = d / "pages" / page_name
        write_json(pdir / "page.json", {
            "$schema": f"{SCHEMA}/item/report/definition/page/1.4.0/schema.json",
            "name": page_name, "displayName": display, "displayOption": "FitToPage",
            "height": 720, "width": 1280})
        for vname, body in visuals:
            write_json(pdir / "visuals" / vname / "visual.json", body)
    write_json(HERE / f"{NAME}.pbip", {
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, obj) -> None:
    write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    build_model()
    build_report()
    print(f"Generated {HERE / (NAME + '.pbip')}")
