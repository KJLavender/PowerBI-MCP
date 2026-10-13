# PowerBI MCP — 報表理解實驗室

[English](README.md) | **繁體中文**

一個完全在本機／地端執行的實驗環境，打造能**讀懂 Power BI 報表**的 Agent。每天早上，它會說明哪些指標有變化、變化來自哪裡。使用者問「為什麼這個指標下降？」時，它會依據語意模型中真實的量值與關聯回答，而且每個回答在顯示前都會和分析結果逐項比對。

用兩份報表驗證：建在 Oracle 上的模擬零售報表，以及用公開 Excel 活頁簿（Global Superstore）做成、埋入已知業務事件作為標準答案的 Power BI 報表。

全程不出內網：資料來源、分析、MCP Server 與 LLM 都在本機執行。

## 功能

| 階段 | 成果 |
|---|---|
| 1. 模擬企業資料來源 | Oracle 資料庫（WSL 內的 Docker）由 ETL 模擬器每日更新；Power BI 專案以程式產生（TMDL + PBIR） |
| 2. 每日變動分析 | 從 Power BI 專案讀取量值與關聯，快照到 DuckDB，偵測顯著變化，並找出能解釋變化的範圍。**6 個埋入情境全數偵測**（其中 4 個當天抓到） |
| 3. 「為什麼」問答 | 報表旁邊的問答面板，透過共用的篩選列與報表連動（篩選同時套用到報表與提問；回答可把範圍套回報表），可追問、即時顯示查詢進度、可查看證據、對話紀錄存在本機，背後是唯讀 MCP Server + 本機 Ollama 模型。每個回答都會和工具結果逐項比對（見第 6 列）；含陷阱題在內，使用者拿到正確答案：零售 **11/11**、Excel **14/15** |
| 4. Report Server | Power BI Report Server 每天 06:30 從 Oracle 排程更新，即典型的地端架構 |
| 5. Excel 報表 | 同一套引擎與問答，用在以公開 Global Superstore 活頁簿做成的 Power BI 報表（一天約 18 筆訂單）：照模型的 Power Query 讀 Excel，門檻用未修改的原始活頁簿校準（每天背景變化 0.2 則），**可偵測的 5 個埋入事件抓到 4 個** |
| 6. 查核 | 每個回答都經過 9 項機械式查核（日期、範圍、數字、名稱、對應、方向、顯著性、無據原因、重點）。不通過就退回重寫，仍不通過時改顯示分析工具自己的判讀。**沒有任何錯誤回答通過查核**；突變測試刻意竄改正確回答，查核攔下 **94–97%**；分析引擎的數字與 Power BI 自己用 DAX 算出的結果對帳（**9,876 個數字，0 個不一致**） |

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

# 驗證
.\.venv\Scripts\python -m agent mutation-test         # 刻意竄改通過的回答，看查核擋不擋得下
.\.venv\Scripts\python -m analysis reconcile          # 與 Power BI Desktop 自己用 DAX 算的數字對帳（先開啟報表）

# Excel 報表（Global Superstore）：上面所有指令設定 PBI_DATASET 後都適用
$env:PBI_DATASET = "superstore"
.\.venv\Scripts\python datasets\superstore\prepare.py   # 下載（SHA-256 驗證）並寫出埋入事件的版本
.\.venv\Scripts\python datasets\superstore\build_pbip.py
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis evaluate
.\.venv\Scripts\python -m agent evaluate
```

## 測試資料

- `powerbi/RetailSales.pbix`：含匯入資料（270 萬筆訂單明細）的報表，採用傳統報表格式，可直接上傳到 Power BI Report Server。不含資料來源帳密。
- `powerbi/RetailSales.pbip`：同一份報表的 Power BI 專案格式（TMDL + PBIR），由 `powerbi/build_pbip.py` 產生。
- `reports/`：每日報告範例、Phase 2 與 Phase 3 的評分結果、突變測試，以及與 Power BI 的對帳結果。
- `datasets/superstore/`：Excel 報表（Power BI 專案、分析設定、埋入事件、問答題庫）與它的結果（`reports/`）。活頁簿本身由 `prepare.py` 下載。

## Repo 結構

| 路徑 | 內容 |
|---|---|
| `simulator/` | Oracle schema、主檔資料、含情境注入的每日資料產生器 |
| `powerbi/` | Power BI 專案產生器、Report Server 用的傳統 `.pbix` 轉換器、報表檔案 |
| `analysis/` | 語意層（解析 TMDL/PBIR/DAX）、快照、變化分析引擎、報告、MCP Server |
| `agent/` | 本機 Ollama Agent、數字查核、問答題庫 |
| `scripts/` | 啟動容器、排程、Report Server 安裝與發佈 |
| `docker/` | Oracle Database Free + SQL Server Developer（Report Server 目錄資料庫） |
| `datasets/` | 用同一套引擎分析的其他報表（Excel 上的 Global Superstore） |
| `docs/` | 各階段的設計說明 |

## 文件

- [docs/powerbi-setup.md](docs/powerbi-setup.md)：Power BI 專案、模型與報表頁面
- [docs/analysis.md](docs/analysis.md)：分析引擎如何讀取模型、偵測變化、選出能解釋變化的範圍
- [docs/agent.md](docs/agent.md)：MCP 工具、數字查核、問答評分
- [docs/report-server.md](docs/report-server.md)：Report Server 安裝，以及 PBIR 格式的陷阱
- [docs/excel-dataset.md](docs/excel-dataset.md)：Excel 報表：小量資料的校準、偵測結果、對帳

## 限制

- 分析能說明變化**發生在哪裡**，但無法說明業務上的原因，除非那個原因本身也在資料裡。
- 門檻要依每份報表校準：Excel 資料直接沿用原設定時每天誤報 7.3 則，改用未修改的原始活頁簿校準後才降下來。資料量小時，只能看出大事件。
- 查核能證明回答與分析結果一致，不能證明分析選到的是最好的解釋；所以回答一定附上證據。
- 若把 MCP Server 接到使用雲端模型的 MCP Client，工具結果會送出本機，只適合用在模擬資料。
