# PRISM：入口網站

PRISM（Proven Report Insights & Semantic Mapping）把這個 repo 的分析引擎、問答與報表目錄包成一個網頁：
使用者上傳自己的 `.pbix` 與 Excel，不需要開終端機、不需要 Power BI。名稱、版本集中在 `prism/__init__.py`。

## 安裝與啟動

| 方式 | 步驟 | 適合 |
|---|---|---|
| Windows | 安裝 Python 3.11 以上 → 雙擊 `PRISM.cmd` | 自己的電腦、部門裡的一台 Windows |
| Docker | `docker compose up -d` | Linux 伺服器、已有 Docker 的環境 |

`PRISM.cmd`（`scripts/start-prism.ps1`）第一次執行時建立 `.venv` 並安裝 `requirements.txt`，之後 `requirements.txt` 有變才重裝；
啟動後打開 <http://localhost:8080>。參數：`-Port 8080`、`-Share`（監聽所有位址，同網路的同事可以開；Windows 防火牆要開 8080 與 8101–8120）、
`-NoBrowser`。紀錄在 `logs/prism.*.log`、每份報表的問答在 `logs/chat-<名稱>.log`。

Docker：`compose.yaml` 有兩個服務：`prism`（`Dockerfile`）與 `ollama`（本機模型）。上傳的檔案、快照、對話、模型分別存在
`workspace`、`data`、`models` 等 volume。有 NVIDIA GPU 時把 `compose.yaml` 裡 `deploy` 那段取消註解，問答會快很多。

### 本機模型

問答用 [Ollama](https://ollama.com) 的 `qwen3.5:4b`（環境變數 `AGENT_MODEL` 可換、`OLLAMA_HOST` 指定位址）。PRISM 啟動時檢查：
Ollama 沒在跑，首頁會說明要安裝；模型不在，就自動下載並在首頁顯示進度。上傳與報表地圖不需要模型。

## 上傳之後發生什麼

上傳 `.pbix`（可同時附上 Excel）後，每一步都顯示在首頁：

| 步驟 | 做什麼 | 對應指令 |
|---|---|---|
| 讀取報表模型與資料 | 用 PBIXRay 解開 .pbix：資料表、DAX、關聯、Power Query、匯入的資料、每頁每張圖與作者的格式設定 | `python -m analysis import-pbix --root workspace` |
| 依資料推出分析設定 | 找事實表、日期、維度候選、指標角色，資料量小時改用小量資料的門檻 | （同上，寫出 `analysis.yaml`） |
| 建立每日快照 | 依模型的關聯把資料攤成 DuckDB 快照 | `python -m analysis snapshot` |
| 產生最新一天的變動報告 | 確認整條分析能跑完 | `python -m analysis report` |
| 加入 Excel | 每個活頁簿的資料表物件（沒有就用有標題列的工作表）做成一份模型 | `python -m analysis import-excel` |
| 更新報表地圖 | 重建所有報表與 Excel 的目錄 | `python -m analysis catalog` |

檔案放在 `workspace/<名稱>/`（`files/` 是原始上傳，`prism.json` 是標題、狀態、報表網址、附加的活頁簿），快照在 `data/<名稱>/`。
`workspace/` 不進 git。刪除報表會一併刪掉它的活頁簿、快照與問答服務。

AdventureWorks 範例（.pbix 加它的資料來源活頁簿）：77 秒跑完；活頁簿的 46 欄全部判定與報表讀的是同一份資料
（其中 2 欄是文字日期 vs 日期，註明「Power Query 做了型別轉換」）。

## 頁面

- **我的報表**：上傳、處理進度、每份報表的「開啟問答」「加入 Excel」「刪除」。第一次開啟問答會啟動那份報表的問答服務
  （`python -m agent.web`，`PBI_DATASET=<名稱>`，埠從 8101 起），之後沿用。有填「已發佈報表的網址」時，問答放在報表旁邊。
- **報表地圖**：所有報表與 Excel 的關係圖與發現：同名但算法不同、名稱不同意思相同、代碼比對（漏的、多的、只差大小寫或空白）、
  共用的資料來源、作者的格式設定、無法比對的量值。兩份報表剛好有相同值的欄位列為「可能可以串接」，不算問題；
  Excel 是報表資料來源的副本時，只顯示逐欄比對，不重複列值重疊。
- **運作原理**：給第一次接觸的人：為什麼回答不會亂講、驗證過的數字、限制。

範例報表（零售、Superstore、財務部門市目標）需要 repo 的 Oracle 模擬器，只在有 `.env` 的開發環境顯示；
別人下載後的 PRISM 從空的開始。`PRISM_EXAMPLES=1`／`0` 可強制顯示／隱藏。

## API

| 方法 | 路徑 | |
|---|---|---|
| GET | `/api/reports` | 所有報表與活頁簿 |
| POST | `/api/reports` | multipart：`pbix`、`excel`（可多個）、`title`、`report_url`；回傳處理工作 |
| POST | `/api/reports/{name}/excel` | 替報表加入活頁簿 |
| GET | `/api/jobs/{id}` | 工作進度（每一步的狀態與輸出） |
| POST | `/api/reports/{name}/open` | 啟動（或沿用）問答服務，回傳網址 |
| DELETE | `/api/reports/{name}` | 刪除上傳的報表 |
| GET / POST | `/api/map`、`/api/map/rebuild` | 報表地圖的資料／重新分析 |
| GET | `/api/model` | 本機模型的狀態 |

## 限制

- 沒有登入與權限：放在內網使用；拿到轉寄連結的人看得到那段對話。
- 被敏感度標籤加密的 .pbix、DirectQuery 報表（檔案裡沒有資料）讀不了，要用 Power BI Desktop 的路徑（`import-pbix --desktop`）。
- 一份報表一個問答服務，Docker 預先開放 20 個埠（8101–8120）；要更多就改 `compose.yaml`。
- 每日變動分析用的是上傳時檔案裡的資料；要更新就重新上傳新的 .pbix。
