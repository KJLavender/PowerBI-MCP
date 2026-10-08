# PowerBI MCP — 報表理解實驗室

[English](README.md) | **繁體中文**

一個完全在本機／地端執行的實驗環境，打造能**讀懂 Power BI 報表**的 Agent。每天早上，它會說明哪些指標有變化、變化來自哪裡。使用者問「為什麼這個指標下降？」時，它會依據語意模型中真實的量值與關聯回答，並自動查核回答裡的每一個數字。

全程不出內網：資料來源、分析、MCP Server 與 LLM 都在本機執行。

## 功能

| 階段 | 成果 |
|---|---|
| 1. 模擬企業資料來源 | Oracle 資料庫（WSL 內的 Docker）由 ETL 模擬器每日更新；Power BI 專案以程式產生（TMDL + PBIR） |
| 2. 每日變動分析 | 從 Power BI 專案讀取量值與關聯，快照到 DuckDB，偵測顯著變化，並找出能解釋變化的範圍。**6 個埋入情境全數偵測**（其中 4 個當天抓到） |
| 3. 「為什麼」問答 | 報表旁邊的問答面板，透過共用的篩選列與報表連動（篩選同時套用到報表與提問；回答可把範圍套回報表），可追問、即時顯示查詢進度、可查看證據，背後是唯讀 MCP Server + 本機 Ollama 模型。回答中的每個數字都必須出現在工具結果中；「顯著」之類的判斷，必須有實際做過顯著性判斷的工具作為依據。**11 題問答測試全數通過**，含陷阱題 |
| 4. Report Server | Power BI Report Server 每天 06:30 從 Oracle 排程更新，即典型的地端架構 |

範例（`python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？"`）：

> **結論**：北區銷售額下降，主要受 3C配件影響（佔總變動 84%）；其中「北區 × 3C配件」顯著異常。
> **證據**：北區銷售額 -19.1%（本期 381,154，比較基準 471,022），未達顯著門檻（z -3.0）；深入一層，北區 × 3C配件 -46.7%，z -4.7，顯著異常 …
> **限制**：資料無法判斷業務原因（如競爭對手、缺貨等），需業務確認。

## 架構

```
06:00  ETL 模擬器 ──▶ Oracle（SALES_DW）──▶ DuckDB 每日快照 ──▶ 分析引擎 ──▶ reports/<日期>.md
06:30                Oracle ──▶ Power BI Report Server 排程更新 ──▶ 報表入口網站

使用者提問 ──▶ Agent（本機）──▶ Ollama 模型
                  │  ▲  工具結果（數字已格式化）
                  ▼  │
           MCP Server（唯讀，stdio）──▶ Power BI 模型（TMDL/PBIR）＋ 快照 ＋ 分析引擎

scenarios/scenarios.yaml — 埋入的業務事件（標準答案），只有評分程式會讀取
```

## 環境需求

- Windows 11，含 WSL2（Ubuntu），且 WSL 內有 Docker Engine
- Python 3.11 以上、Power BI Desktop
- Phase 3：[Ollama](https://ollama.com) 與支援工具呼叫的模型（預設 `qwen3.5:4b`）
- Phase 4：Power BI Report Server（Developer 版）、Report Server 專用版 Power BI Desktop、Oracle ODAC

## 快速開始

```powershell
# Phase 1 — Oracle + 模擬資料 + Power BI 專案
copy .env.example .env                                  # 再設定密碼
.\scripts\start-oracle.ps1
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m simulator init
.\.venv\Scripts\python -m simulator backfill            # 2025-01-01 ～ 昨天，約 270 萬筆
.\.venv\Scripts\python powerbi\build_pbip.py            # 開啟 powerbi\RetailSales.pbip，按重新整理
.\scripts\register-daily-task.ps1                       # 每天 06:00：ETL + 分析

# Phase 2 — 每日變動報告
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis report               # reports/<日期>.md
.\.venv\Scripts\python -m analysis evaluate             # 用埋入的情境評分

# Phase 3 — 用本機模型問為什麼
.\scripts\start-chat.ps1                                # 報表 + 問答面板 http://localhost:8090（或雙擊 報表問答助理.cmd）
.\.venv\Scripts\python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？" -v
.\.venv\Scripts\python -m agent chat
.\.venv\Scripts\python -m agent evaluate

# Phase 4 — Report Server（一次性的管理員設定，之後發佈）
.\scripts\phase4-setup-admin.ps1                        # 以系統管理員身分執行
.\scripts\phase4-publish.ps1                            # 上傳、設定帳密、06:30 排程、立即更新
```

## 測試資料

- `powerbi/RetailSales.pbix`：含匯入資料（270 萬筆訂單明細）的報表，採用傳統報表格式，可直接上傳到 Power BI Report Server。不含資料來源帳密。
- `powerbi/RetailSales.pbip`：同一份報表的 Power BI 專案格式（TMDL + PBIR），由 `powerbi/build_pbip.py` 產生。
- `reports/`：每日報告範例，以及 Phase 2、Phase 3 的評分結果。

## Repo 結構

| 路徑 | 內容 |
|---|---|
| `simulator/` | Oracle schema、主檔資料、含情境注入的每日資料產生器 |
| `powerbi/` | Power BI 專案產生器、Report Server 用的傳統 `.pbix` 轉換器、報表檔案 |
| `analysis/` | 語意層（解析 TMDL/PBIR/DAX）、快照、變化分析引擎、報告、MCP Server |
| `agent/` | 本機 Ollama Agent、數字查核、問答題庫 |
| `scripts/` | 啟動容器、排程、Report Server 安裝與發佈 |
| `docker/` | Oracle Database Free + SQL Server Developer（Report Server 目錄資料庫） |
| `docs/` | 各階段的設計說明 |

## 文件

- [docs/powerbi-setup.md](docs/powerbi-setup.md)：Power BI 專案、模型與報表頁面
- [docs/analysis.md](docs/analysis.md)：分析引擎如何讀取模型、偵測變化、選出能解釋變化的範圍
- [docs/agent.md](docs/agent.md)：MCP 工具、數字查核、問答評分
- [docs/report-server.md](docs/report-server.md)：Report Server 安裝，以及 PBIR 格式的陷阱

## 限制

- 分析能說明變化**發生在哪裡**，但無法說明業務上的原因，除非那個原因本身也在資料裡。
- 門檻是依這份模擬資料調整的；換成真實資料前，請用過去實際發生的事件重新校準。
- 若把 MCP Server 接到使用雲端模型的 MCP Client，工具結果會送出本機，只適合用在模擬資料。
