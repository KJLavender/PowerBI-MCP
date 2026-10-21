"""Read-only MCP server over the Power BI semantic model and the daily snapshots.

    python -m analysis.mcp_server          (stdio transport)

Runs entirely on this machine; it never contacts external services.
"""
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import tools

INSTRUCTIONS = f"""\
{tools.ctx().config.get("description", "Power BI 報表")}的分析工具（唯讀）。指標與關聯取自 Power BI 模型。
- 不確定指標、維度或成員名稱時，先呼叫 describe_model 或 list_members。
- 回答「為什麼某指標變化」一定要呼叫 explain_change，並只引用工具回傳的數字與文字。
- filters 用維度名稱對應成員，例如 {{"維度名稱": "成員"}}；不給代表整體。
- date 為 YYYY-MM-DD，省略代表最新一天。window：{" / ".join(tools.ctx().config["windows"])}。
- 結果中以底線開頭的欄位（_facts、_key）是給回答查核程式用的結構化資料。
"""

server = MCPServer("powerbi-analysis", instructions=INSTRUCTIONS, version="0.3.0")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _run(fn, *args) -> str:
    try:
        return json.dumps(fn(*args), ensure_ascii=False)
    except tools.ToolError as e:
        return json.dumps({"錯誤": str(e)}, ensure_ascii=False)


@server.tool(annotations=READ_ONLY)
def describe_model() -> str:
    """列出 Power BI 模型中的指標（含 DAX、說明、拆解關係）、維度（含關聯路徑）、資料期間與時間窗定義。"""
    return _run(tools.describe_model)


@server.tool(annotations=READ_ONLY)
def list_members(dimension: str, contains: str | None = None) -> str:
    """列出某維度的成員名稱（維度見 describe_model），可用 contains 篩選。"""
    return _run(tools.list_members, dimension, contains)


@server.tool(annotations=READ_ONLY)
def daily_report(date: str | None = None) -> str:
    """取得某營業日的每日變動報告：資料品質、關鍵指標、顯著變化（含解釋了哪些範圍、是否為新發現）。"""
    return _run(tools.daily_report, date)


@server.tool(annotations=READ_ONLY)
def metric_change(metric: str | None = None, filters: dict[str, str] | None = None,
                  date: str | None = None, window: str = "day") -> str:
    """查詢指標在某範圍、某日、某時間窗的數值與變化（本期、比較基準、變化率、z 值、是否顯著）。metric 省略則回傳報表上所有指標。"""
    return _run(tools.metric_change, metric, filters, date, window)


@server.tool(annotations=READ_ONLY)
def explain_change(metric: str, filters: dict[str, str] | None = None,
                   date: str | None = None, window: str = "day") -> str:
    """解釋指標為什麼變化：同範圍其他指標（量/價/折扣/成本）、模型拆解關係、沿各維度的貢獻拆解、
    是否為更大範圍的現象、節日/新店/資料品質等脈絡。回答「為什麼」的問題時必須使用。"""
    return _run(tools.explain_change, metric, filters, date, window)


@server.tool(annotations=READ_ONLY)
def metric_trend(metric: str, filters: dict[str, str] | None = None, start: str | None = None,
                 end: str | None = None, grain: str = "week") -> str:
    """指標的時間序列（grain: day 或 week），用來看趨勢、何時開始變化。預設最近 12 週。"""
    return _run(tools.metric_trend, metric, filters, start, end, grain)


@server.tool(annotations=READ_ONLY)
def data_quality(date: str | None = None) -> str:
    """某營業日的資料品質：ETL 狀態與載入筆數（若資料來源有）、明細筆數、資料量異常的單位。"""
    return _run(tools.data_quality, date)


@server.tool(annotations=READ_ONLY)
def metric_lineage(metric: str) -> str:
    """指標的來源：DAX 定義、引用的量值、實際計算的欄位、來源資料表、報表中使用的位置。"""
    return _run(tools.metric_lineage, metric)


@server.tool(annotations=READ_ONLY)
def report_emphasis(report: str | None = None) -> str:
    """報表作者刻意凸顯的設定：條件式格式的門檻與顏色、目標線、特別上色的成員、前 N 名、卡片比較值；
    並用最新數字對照作者的門檻。問「報表想強調什麼」「紅燈門檻是多少」時使用。report 省略 = 目前的報表。"""
    return _run(tools.report_emphasis, report)


@server.tool(annotations=READ_ONLY)
def cross_report(name: str = "", other: str | None = None) -> str:
    """跨報表：某量值 / 欄位 / 資料表在其他報表的對應——意思相同的量值（不論名稱）、同名但定義不同、
    共用的來源與代碼對不上的地方、作者的設定；給 other 時回傳兩者的關聯路徑。name 可寫 報表:名稱 或 表[欄位]；
    name 省略 = 所有報表的關係總覽。問「別的報表」「為什麼兩份報表數字不一樣」「跟 Excel 對得起來嗎」時使用。"""
    return _run(tools.cross_report, name, other)


def main(**kwargs: Any) -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
