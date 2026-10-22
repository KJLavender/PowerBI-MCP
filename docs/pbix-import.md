# 直接從 .pbix 匯入報表

前面兩份報表都是 Power BI 專案格式（PBIP：TMDL + PBIR），但公司裡大多數報表只有一個 `.pbix` 檔。這裡讓分析引擎直接吃 `.pbix`，不需要先另存成專案、也不需要手寫分析設定。

```powershell
.\.venv\Scripts\python -m analysis import-pbix 報表.pbix --name sales           # 直接讀檔案，不需要 Power BI
.\.venv\Scripts\python -m analysis import-pbix 報表.pbix --name sales --desktop # 改用 Power BI Desktop 讀
.\.venv\Scripts\python -m analysis import-pbix --title "報表" --name sales       # 已經在 Desktop 開著的報表
$env:PBI_DATASET = "sales"
.\.venv\Scripts\python -m analysis model        # 看讀到了什麼
.\.venv\Scripts\python -m analysis snapshot     # 取資料
.\.venv\Scripts\python -m agent.web             # 問答
```

## 怎麼讀

`.pbix` 是一個 zip 檔，裡面分兩部分：

| 讀什麼 | 預設：直接讀檔案 | `--desktop`：透過 Power BI Desktop |
|---|---|---|
| 報表每一頁、每個圖表用了哪些欄位、篩選、作者的格式設定 | 解壓縮後讀 JSON：`Report/Layout`（舊格式）或 `Report/definition`（PBIR） | 同左 |
| 資料表、欄位、量值（DAX）、關聯、Power Query、參數 | [PBIXRay](https://github.com/Hugoberry/pbixray)（MIT）解壓縮資料模型，讀裡面的中繼資料庫：就是 Desktop 的 `$SYSTEM.TMSCHEMA_*` 檢視背後的那幾張表 | 開啟檔案，問 Desktop 背景跑的 Analysis Services 引擎（DMV，只連 `localhost`） |
| 分析用的資料 | PBIXRay 解出檔案裡存的資料表（Power Query 處理後、Power BI 匯入的那一份），放進 DuckDB，用同一套 SQL 做快照（`source: pbix`） | 引擎用 DAX `SUMMARIZECOLUMNS` 算好的彙總（`source: powerbi`） |

兩條路讀到的是同一份模型，後面的步驟完全共用。直接讀檔案**不需要 Windows、不需要安裝 Power BI**，零售報表從匯入到草稿設定約 15 秒（大部分在解 274 萬列資料），AdventureWorks 範例不到 2 秒；Desktop 要先等它開檔。
檔案讀不了時（例如被 Microsoft Purview 敏感度標籤加密）會自動改用 Desktop。

不論哪一條，**報表接的是什麼資料來源都可以**（SQL Server、SharePoint 清單、網頁 API……），因為讀的是 Power BI 已經匯入的資料；代價是資料只到報表最後一次重新整理。如果模型的資料來源是 Oracle 或 Excel，也可以把設定裡的 `source:` 拿掉，改成照 Power Query 直接讀來源，每天排程。

### 兩條路的結果一樣嗎

用 `powerbi/RetailSales_RS.pbix` 各匯入一次，逐項比對：

| 比對 | 結果 |
|---|---|
| 模型（7 張表、21 個量值、6 條關聯、Power Query、參數） | 相同；唯一差別是直接讀檔案保留了 Power Query 的換行，Desktop 路徑經過 PowerShell 時被壓成一行 |
| 分析設定草稿 | 相同（事實表、日期、13 個維度與階層、指標角色、門檻），只有 `source` 不同 |
| 快照 | **52,885,918 個值，0 個不一致**（line_snap 2,213,277 列、order_snap 478,767 列、643 天） |

PBIXRay 本身另外驗過：零售報表的 21 個量值 DAX 與 repo 裡的定義相同，`FACT_SALES` 2,745,960 列、銷售額合計與 Oracle 同期間相同；Microsoft 的 10 個官方範例 .pbix 都讀得出模型。

再拿沒看過的 Microsoft AdventureWorks Sales 範例從頭跑一次（匯入 → 快照 → 每日報告），不開 Desktop；報告上 2020-06-15 的 Sales 373,022、Cost 376,180，和直接加總檔案裡的明細相同。

這次也修掉了幾個在零售資料上碰不到的問題：

- Desktop 路徑讀到空的說明 / 格式時，模型檔裡寫成 `/// nan`、`formatString: nan`（pandas 的空值是 NaN，而 NaN 在 Python 裡算「有值」）。`datasets/retail_pbix` 已重新產生，它的報告前後相同。
- 貨幣欄位在檔案裡是固定小數，DuckDB 依第一個值把型別猜成 `DECIMAL(3,0)` 後溢位；改成存浮點數。
- 第一個月剛好沒有節日時，日曆的 `holiday` 欄被建成整數，之後的節日名稱存不進去。
- 欄位叫 `Group`、`Date` 這類 SQL 保留字時沒有加引號；自動產生的維度代號也避開保留字。
- `CALCULATE` 篩選了一張從事實表走不到的表（例如計算群組）時，整個翻譯中斷；改成只把那個量值列為無法分析。

## 設定草稿：從資料推斷

`import-pbix` 會依模型與資料寫出 `datasets/<名稱>/analysis.yaml` 草稿，檔頭註明推斷的依據：

| 項目 | 推斷方式 |
|---|---|
| 事實表 | 多對一關聯最多的資料表（同分時看量值數） |
| 日期 | 標記為日期資料表的鍵值欄位，否則事實表上的日期欄位 |
| 維度 | 從事實表沿多對一關聯走得到的文字欄位，排除隱藏欄位與代碼（ID、CODE、編號…），成員數 2～200 |
| 標籤 | 欄位說明的開頭（「區域：北區 / 中區…」→「區域」），沒有說明就用欄位名稱 |
| 階層 | 同一張表上，若 B 的每個值都只對應一個 A，A 就是 B 的上層（門市 → 縣市 → 區域） |
| 品項 | 成員 200～20,000 的欄位（例如商品），只用於同品項售價指數，不掃描 |
| 指標角色 | 依量值名稱（銷售額、銷售數量、訂單數、平均單價、折扣率、毛利率…） |
| 可能為負的指標 | 實際在某一天出現過負值的金額類量值 |
| 門檻 | 依每天明細筆數：300 列以下用小量資料的設定（不比較單日、至少 60 筆才判斷、成員 z ≥ 5） |
| 資料品質單位 | 商品以外、成員最多（≤ 100）的維度（例如門市），某天筆數突然低於平常一半時提示可能是資料缺漏 |

草稿只是起點：要檢查標籤與階層，並且**用沒有事件的歷史資料校準門檻**（見 [excel-dataset.md](excel-dataset.md)）。

## 驗證：拿已知答案的零售報表來試（透過 Desktop 匯入時）

用 repo 裡的 `powerbi/RetailSales_RS.pbix`（零售模擬資料，Oracle 來源），完全不手動調整設定：

- **模型**：讀到 7 張表、21 個量值、6 條關聯、26 個視覺效果；翻譯出的 10 個量值與從 PBIP 讀到的完全相同。
- **草稿設定**：事實表、日期、13 個維度（含門市 → 縣市 → 區域、通路 → 實體/線上 等階層）、指標角色、資料品質單位（門市），標籤都是中文。
- **資料**：從 Power BI 引擎取的快照，和直接讀 Oracle 的快照逐一比對：**544,515 個彙總值，0 個不一致**（2025-01-01 ～ 2026-10-01）。比對過程中抓到一個 bug：第一個月的金額剛好都是整數時，快照欄位被建成整數型別，之後的小數被捨去；改成一律存浮點數後才一致。
- **分析**：直接跑出埋入的事件：飲料漲價、北區 × 3C配件下滑、官網折扣、生鮮成本上升、高雄左營店資料缺漏。

- **資料品質**：自動選出的檢查單位（門市）在 2026-10-03 標出「高雄左營店當日明細 67 筆，僅為近 4 週同星期中位數的 26%，疑似資料缺漏」。
- **問答**：零售的 11 題題庫原封不動拿來用：使用者拿到正確答案 **10/11**，**查核通過卻答錯 0**。沒過的一題是每日摘要：模型自己補上門市所屬的「花蓮縣」，查核認為該數字不屬於這個範圍而退件。

草稿與手寫設定的差別也看得出來：維度較多（同一件事會從商品、品牌各報一次）、「區域」和「會員居住區域」都有「中區」這個成員，報告上會出現兩個「中區」。這些都是檢查草稿時該刪改的地方。

報表裡存的篩選也會一起讀：舊格式 `Report/Layout` 與 PBIR 的報表、頁面、圖表三個層級篩選都會轉進來（報表層級套用到分析，其他層級列在說明中），見 [analysis.md 的支援範圍](analysis.md#支援範圍報表裡的哪些東西讀得懂)。

## 限制

- 資料只到報表最後一次重新整理。
- PBIXRay 是社群依檔案格式反推寫成的（非 Microsoft 官方）；Power BI 改版時可能要等它更新。有 Desktop 的環境可以用 `--desktop` 匯入同一個檔案，或用 `python -m analysis reconcile` 對帳。
- 被敏感度標籤加密的 .pbix 只能透過 Desktop 讀；DirectQuery / 即時連線的報表檔案裡沒有資料，資料要從 Desktop 取（`--desktop`）。
- 草稿設定的維度通常比手寫的多（零售：13 個 vs 6 個），分析會變慢，也比較容易出現「同一件事從好幾個維度各報一次」，建議刪掉不需要的維度。
- 引擎翻譯不了的 DAX（時間智慧函數、`FILTER`/`ALL`）一樣會列為「無法分析」；欄位篩選型的 `CALCULATE` 可以翻譯。
- Power BI 服務（雲端）上別人的報表：需要語意模型的「建置」權限，透過 XMLA 端點才能用同樣的 DMV 與 DAX；只有檢視權限時讀不到模型。
