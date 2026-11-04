# PRISM — Proven Report Insights & Semantic Mapping

[English](README.md) | **繁體中文**

PRISM 讓你用一般的話問 Power BI 報表的問題，例如「為什麼北區的銷售額下降？」「紅色的那根長條是什麼意思？」。你上傳報表檔（`.pbix`）和相關的 Excel，PRISM 直接讀檔案，不需要裝 Power BI。數字由一般程式計算，不是語言模型算的，每個回答在顯示給你之前，都會先和計算結果逐項比對。全部在你自己的電腦上執行。

## 它能做什麼

- 回答「為什麼」。數字來自報表自己的量值（用 DAX 寫的命名公式，DAX 是 Power BI 的公式語言），翻成 SQL 後在本機的資料副本上計算。語言模型只負責把結果寫成句子。
- 每個回答都過 9 項機械式查核：日期、範圍、數字、名稱、數字與名稱的對應、方向、顯著性、沒有證據的原因、重點。不通過的回答會退回給模型重寫；仍不通過，就改顯示分析工具自己的判讀。
- 說明畫面上的東西。作者設的顏色規則、目標線、特別標出的成員，都是從報表檔讀出來的，所以不用截圖，就能問「紅色的字」「毛利率那條虛線」。
- 把報表和 Excel 放在同一頁，也就是報表地圖。它會找出同名但算法不同的量值、名稱不同但意思相同的量值，以及 Excel 與資料庫對不上的代碼。
- 可以轉寄對話。下一個人打開的是同一個報表畫面，可以接著同一段對話問下去。

## 運作方式

1. PRISM 用 [PBIXRay](https://github.com/Hugoberry/pbixray) 解開 `.pbix`，讀出資料表、量值、關聯、Power Query 步驟、匯入的資料，以及每一頁每張圖用了什麼。
2. 依資料推出分析設定（事實表、日期欄、維度），並把每日快照存進 DuckDB（一個小型的本機資料庫）。
3. 本機的小型語言模型（Ollama，預設 `qwen3.5:4b`）透過 MCP Server 呼叫唯讀工具來回答。MCP 是讓模型使用工具的通用標準。工具回傳的數字已經格式化好，模型必須照抄。
4. 查核器把模型的回答和工具結果逐項比對。

## 怎麼執行

聊天需要 [Ollama](https://ollama.com/download)，PRISM 第一次啟動時會自動下載模型。上傳報表和報表地圖不需要 Ollama。

Windows：安裝 [Python 3.11 以上](https://www.python.org/downloads/)，然後雙擊 `PRISM.cmd`。第一次會建立 `.venv` 並安裝套件，要等幾分鐘，之後瀏覽器會打開 <http://localhost:8080>。啟動腳本是 `scripts/start-prism.ps1`，`PRISM.cmd` 會把參數傳給它：

- `-Share`：監聽所有位址，同網路的同事也能開。要在 Windows 防火牆放行 8080 和 8101-8120。
- `-Port 8080` 改入口網站的埠，`-NoBrowser` 不自動開瀏覽器。

用 Docker（Linux，或有 Docker 的 Windows）：下面的指令會同時啟動 PRISM 和 Ollama 容器。

```bash
docker compose up -d        # 然後打開 http://localhost:8080
```

有 NVIDIA 顯卡的話，把 `compose.yaml` 裡 `deploy` 那段取消註解，聊天會快很多。

接著把 `.pbix` 拖到頁面上，有 Excel 就一起加入，按上傳按鈕（頁面上寫「上傳並分析」）。PRISM 會匯入檔案、建立設定和快照、產生第一份報告，並更新地圖。AdventureWorks 範例加它的活頁簿花了 77 秒。完成後，就可以開啟那份報表的聊天。

環境變數：`AGENT_MODEL`（聊天用的 Ollama 模型，預設 `qwen3.5:4b`）、`OLLAMA_HOST`（預設 `http://127.0.0.1:11434`，Docker 版指向 `ollama` 容器）、`PRISM_EXAMPLES`（`1` 或 `0`，顯示或隱藏範例報表）。

三個頁面：

| 頁面 | 用途 |
|---|---|
| 我的報表 | 上傳、處理進度、開啟報表的聊天、加入更多 Excel、刪除 |
| 報表地圖 | 所有報表與 Excel、共用的來源、同義量值、衝突、代碼比對、作者的設定 |
| 運作原理 | 為什麼回答可以信、驗證過的數字、限制 |

詳細說明、HTTP API 和在網路上分享的做法，請看 [docs/prism.md](docs/prism.md)。

## 驗證過什麼

以下數字來自這個 repo 裡的測試報表（`reports/` 和 `datasets/*/reports/`）。

| 驗證項目 | 結果 |
|---|---|
| 分析引擎的數字 vs Power BI 自己用 DAX 算的（零售報表 4,176 個、Superstore 報表 6,140 個） | 10,316 個數字，0 個不一致 |
| 直接讀 `.pbix` 檔的快照 vs 透過 Power BI Desktop 取的快照 | 52,885,918 個值，0 個不一致 |
| 透過 Power BI Desktop 的快照 vs 直接讀 Oracle | 544,515 個值，0 個不一致 |
| 畫面上圖表的數值 vs 對 Oracle 下 SQL | 75 個值，0 個不一致 |
| 零售報表問答（20 題） | 使用者拿到 18 題正確答案 |
| Superstore 報表問答（15 題） | 使用者拿到 14 題正確答案 |
| 通過查核卻答錯的回答 | 以上全部是 0 個 |
| 突變測試（刻意竄改正確回答，看查核攔不攔得下） | 攔下 98/99、103/106、90/94（96% 到 99%） |
| 找到埋入的業務事件，零售報表 | 6 個中找到 6 個 |
| 找到埋入的業務事件，Superstore 報表 | 可偵測的 5 個中找到 4 個 |
| 格式解析器 vs Microsoft 官方 10 個範例 `.pbix` | 數量與檔案一致 |

## 開發用的實驗環境

PRISM 是從一個本機實驗環境長出來的。這個環境模擬公司的架構，並在資料裡埋入已知的業務事件，這樣才能替回答評分。零售報表建在 Oracle 資料庫上，由 ETL 模擬器每天更新，並發佈到 Power BI Report Server，每天 06:30 排程更新。第二份報表用公開的 Global Superstore Excel 活頁簿做成。第三份「門市目標」報表把零售的 Oracle 資料和一份 Excel 目標活頁簿放在一起，活頁簿裡刻意放了不一致的地方，用來測試跨報表的地圖。

`python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？"` 的範例回答：

> 結論：北區銷售額下降，主要受 3C配件影響（佔總變動 84%）；其中「北區 × 3C配件」顯著異常。
> 證據：北區銷售額 -19.1%（本期 381,154，比較基準 471,022），未達顯著門檻（z -3.0）；深入一層，北區 × 3C配件 -46.7%，z -4.7，顯著異常 …
> 限制：資料無法判斷業務原因（如競爭對手、缺貨等），需業務確認。

實驗環境需要 Windows 11、WSL2（Ubuntu）和 WSL 內的 Docker Engine、Python 3.11 以上、Power BI Desktop 和 Ollama。Report Server 的部分另外需要 Power BI Report Server（Developer 版）、Report Server 專用版 Power BI Desktop 和 Oracle ODAC。

```powershell
# Oracle、模擬資料、Power BI 專案
copy .env.example .env                                  # 再設定密碼
.\scripts\start-oracle.ps1
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m simulator init
.\.venv\Scripts\python -m simulator backfill            # 2025-01-01 到昨天，約 270 萬筆
.\.venv\Scripts\python powerbi\build_pbip.py            # 開啟 powerbi\RetailSales.pbip，按重新整理
.\scripts\register-daily-task.ps1                       # 每天 06:00：ETL + 分析

# 每日變動報告
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis report               # 寫出 reports/<日期>.md
.\.venv\Scripts\python -m analysis evaluate             # 用埋入的事件評分

# 用本機模型問為什麼
.\scripts\start-chat.ps1                                # 報表 + 問答面板 http://localhost:8090
.\.venv\Scripts\python -m agent ask "為什麼 2026-09-17 北區的銷售額下降？" -v
.\.venv\Scripts\python -m agent chat
.\.venv\Scripts\python -m agent evaluate

# Report Server（一次性的管理員設定，之後發佈）
.\scripts\phase4-setup-admin.ps1                        # 以系統管理員身分執行
.\scripts\phase4-publish.ps1                            # 上傳、設定帳密、06:30 排程、立即更新

# 驗證
.\.venv\Scripts\python -m agent mutation-test           # 刻意竄改正確的回答，看查核攔不攔得下
.\.venv\Scripts\python -m analysis reconcile            # 與 Power BI Desktop 用 DAX 算的結果對帳（先開啟報表）

# 多份報表與作者意圖
.\.venv\Scripts\python datasets\store_targets\prepare.py
.\.venv\Scripts\python datasets\store_targets\build_pbip.py
.\.venv\Scripts\python -m analysis catalog              # 寫出 reports/catalog.md
.\.venv\Scripts\python -m analysis intent store_targets # 作者設定的門檻、目標線、凸顯

# 不用 Power BI 匯入任何 .pbix（加 --desktop 則改透過 Power BI Desktop 讀）
.\.venv\Scripts\python -m analysis import-pbix 檔案.pbix --name X

# Excel 報表（Global Superstore）：設定 PBI_DATASET 後，上面的指令都適用
$env:PBI_DATASET = "superstore"
.\.venv\Scripts\python datasets\superstore\prepare.py   # 下載活頁簿（驗 SHA-256）並寫出埋入事件的版本
.\.venv\Scripts\python datasets\superstore\build_pbip.py
.\.venv\Scripts\python -m analysis snapshot
.\.venv\Scripts\python -m analysis evaluate
.\.venv\Scripts\python -m agent evaluate
```

`agent/mcp-config.example.json` 示範如何從別的 MCP Client 啟動唯讀的 MCP Server（`python -m analysis.mcp_server`）。

repo 裡的測試資料：`powerbi/RetailSales.pbix`（含 270 萬筆訂單明細的零售報表，採用 Report Server 能接受的舊格式，不含資料來源帳密）、`powerbi/RetailSales.pbip`（同一份報表的 Power BI 專案格式，由 `powerbi/build_pbip.py` 產生），以及 `reports/` 和 `datasets/*/reports/` 底下的評分結果。

## 資料夾

| 路徑 | 內容 |
|---|---|
| `prism/` | 入口網站：上傳、處理工作、每份報表一個問答服務、報表地圖 |
| `analysis/` | 讀模型（TMDL、PBIR、DAX）、快照、變化偵測、報告、MCP Server |
| `agent/` | 本機 Ollama Agent、回答查核、題庫、問答頁面 |
| `simulator/` | Oracle schema、主檔資料、含事件注入的每日資料產生器 |
| `powerbi/` | 專案產生器、轉成 Report Server 用舊格式 `.pbix` 的轉換器、報表檔案 |
| `datasets/` | Superstore 報表、門市目標報表、匯入 `.pbix` 的測試資料 |
| `scripts/` | 啟動、排程、Report Server 用的 PowerShell 腳本 |
| `docker/` | Oracle Database Free 和 SQL Server Developer（Report Server 的目錄資料庫） |
| `docs/` | 各主題的設計說明 |

## 限制

- 分析能說明變化發生在哪裡，但無法給出業務上的原因，除非原因本身就在資料裡。
- 門檻要依每份報表調整。Excel 資料直接沿用原設定時，每天誤報 7.3 則，改用未修改的原始活頁簿重新校準後才降下來。資料量小時，只看得出大事件。
- 查核只能證明回答與分析結果一致，不能證明分析選到的是最好的解釋，所以回答一定附上證據。
- 量值比對只涵蓋常見的 DAX 寫法。`IF`、`SWITCH` 等會列為無法展開，不會被當成相同。混用 Oracle 與 Excel 的報表只用在地圖和問答，沒有每日變動分析。
- PRISM 沒有登入和權限，請在內網使用。拿到轉寄連結的人看得到那段對話。
- 被敏感度標籤加密的 `.pbix`，以及 DirectQuery 報表（檔案裡沒有資料），無法直接讀取。請改用 Power BI Desktop 搭配 `import-pbix --desktop`。
- 如果把 MCP Server 接到使用雲端模型的 Client，工具結果會送出你的電腦。這種情況只適合用模擬資料。
