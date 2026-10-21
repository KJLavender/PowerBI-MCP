# 多份報表、報表作者的意圖

一份 Power BI 報表很少單獨存在：財務部、營運部各做一份，讀同一個資料庫，旁邊再放一份 Excel 目標。
這一階段回答三個問題：

1. **作者想凸顯什麼？** 條件式格式的門檻、目標線、特別上色的成員……這些是作者明確做的設定，可以直接讀出來。
2. **A 報表跟 B 報表有什麼關係？** 讀同一張表嗎？同名的量值是同一件事嗎？不同名的呢？
3. **報表跟 Excel 對得起來嗎？** Excel 的代碼跟資料庫的代碼一致嗎？

```powershell
.\.venv\Scripts\python -m analysis intent store_targets      # 某份報表的作者設定（也可以給 .pbix 或 *.Report 資料夾）
.\.venv\Scripts\python -m analysis catalog                   # 所有報表 → reports/catalog.md
```

問答助理有對應的兩個工具：`report_emphasis`（作者設定，並用最新數字對照作者的門檻）與 `cross_report`
（某個量值 / 欄位在其他報表的對應、兩者之間的關聯路徑、所有報表的總覽）。

## 原則：全部從報表檔案讀，不從名稱猜

| 問題 | 依據 | 不用的方法 |
|---|---|---|
| 作者凸顯什麼 | 報表版面 JSON 裡的格式設定（條件式格式、參考線、資料點顏色、篩選） | 讓模型看截圖猜 |
| 兩張表是不是同一張 | Power Query：伺服器、schema、資料表；Excel 檔案路徑與工作表 | 表名相似度 |
| 兩個量值是不是同一件事 | 把 DAX 展開到實體欄位上的彙總，比對公式是否等價 | 量值名稱、embedding |
| Excel 跟資料庫對不對得起來 | 讀實際資料，逐值比對 | 欄名相似度 |

向量檢索（RAG）適合把「業績」「營收」這類口語對到候選名稱，但「是不是同一件事」必須由程式確認；
數字一律由分析引擎計算。

## 作者的意圖（`analysis/intent.py`）

報表版面（PBIR 的 `visual.json`，或舊版 .pbix 的 `Report/Layout`）裡每個圖表都帶著作者的格式設定：

| 讀出來的 | 例子 | 性質 |
|---|---|---|
| 條件式格式：規則 | 「達成率 < 90% → 紅；90%～100% → 黃；≥ 100% → 綠」 | 作者明確設定，門檻可以拿來判斷 |
| 條件式格式：色階、依欄位值上色、資料橫條 | 「背景色依 Total Sales Var % 漸層：最低紅 → 0 → 最高藍」 | 作者明確設定 |
| 常數線 / 分析線 | 「Y 軸常數線『毛利率底線 45%』= 45%」、平均線、趨勢線、預測、異常偵測 | 作者明確設定 |
| 凸顯單一成員 | 「只把區域 = 東區改成紅色」 | 只改一個成員才算凸顯；改兩個以上通常只是配色 |
| 前 N 名篩選 | 「只顯示依 Revenue Won 排序的前 5 名 Industry」 | 作者明確設定 |
| 卡片比較值、儀表目標 | 「卡片 This Year Sales 旁邊顯示 Last Year Sales」、「gauge 以 Last Year Sales 為目標」 | 作者明確設定 |
| 版面 | 每頁左上角、面積最大的圖表 | **推斷**，標示為提示，不當成作者說的話 |

顏色另外標上色系（紅 / 黃 / 綠…），門檻的數字依量值的格式顯示（達成率是百分比，就寫 90%，不寫 0.9）。

### 用真實報表驗證

格式設定的 JSON 結構沒有公開規格，所以用 Microsoft 官方的 10 個範例 .pbix（[powerbi-desktop-samples](https://github.com/microsoft/powerbi-desktop-samples)，
新舊兩種格式都有）驗證：每個檔案裡的 `Conditional`（規則）與 `FillRule`（色階）數量，都和解析器讀出的數量一致。

| 範例 | 格式 | 規則 | 色階 | 參考 / 分析線 | 凸顯 | 成員配色 | 前 N 名 | 比較值 / 目標 |
|---|---|---|---|---|---|---|---|---|
| AdventureWorks Sales | PBIR | 2 | 6 | 1 | 0 | 0 | 0 | 0 |
| Competitive Marketing Analysis | 舊版 Layout | 0 | 2 | 2 | 0 | 0 | 5 | 4 |
| Corporate Spend | PBIR | 0 | 9 | 0 | 0 | 0 | 0 | 14 |
| Employee Hiring and History | 舊版 Layout | 0 | 4 | 2 | 2 | 9 | 0 | 2 |
| Human Resources Sample | 舊版 Layout | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Regional Sales Sample | 舊版 Layout | 6 | 12 | 4 | 0 | 6 | 2 | 3 |
| Revenue Opportunities | PBIR | 0 | 6 | 0 | 0 | 1 | 0 | 0 |
| Sales & Returns Sample | 舊版 Layout | 5 | 0 | 2 | 0 | 0 | 5 | 2 |
| Store Sales | PBIR | 1 | 6 | 2 | 0 | 2 | 0 | 12 |
| Supply Chain Sample | 舊版 Layout | 0 | 5 | 0 | 0 | 6 | 0 | 1 |

讀範例時修掉的問題：

- 色階在 JSON 裡有兩層 `FillRule`（外層帶輸入欄位、內層帶顏色），一開始每個色階被算兩次。
- 「凸顯」原本定義成「只改兩個以內的成員」，但 Full-Time / Part-Time 兩個都上色其實就是全部成員；改成只改一個才算。
- 舊版格式在 `config` 和 `dataTransforms` 各存一份設定，只讀 `config`。
- **既有 bug**：PBIR 格式的 .pbix 若圖表用了隱含量值（例如 `Sum(Sales.Amount)`），`import-pbix` 解析頁面時會出錯
  （AdventureWorks、Store Sales 都會）。已修正：隱含量值視為用到該欄位。

`import-pbix` 匯入舊版 .pbix 時，現在也保留每個圖表的位置、格式設定、角色與書籤；
10 個範例轉成 PBIR 資料夾後再讀，結果和直接讀 .pbix 完全相同。

## 多份報表（`analysis/catalog.py`）

### 血緣：每張表、每個欄位追到實體來源

Power Query 決定一張表讀哪裡：`Oracle.Database("127.0.0.1:1521/FREEPDB1")` → schema `SALES_DW` → 資料表 `FACT_SALES`。
伺服器與 schema 可能直接寫在 M 裡，也可能是參數，兩種都會解析。欄位再經過模型改名
（`實收金額` 的 `sourceColumn` 是 `SALES_AMOUNT`）與 Power Query 改名，追到原始欄位。
所以 B 報表叫「銷售[實收金額]」、A 報表叫「FACT_SALES[SALES_AMOUNT]」，在目錄裡是同一個來源欄位。

### 量值：公式等價才算同義

每個量值展開成「實體欄位上的彙總」組成的運算式：量值引用展開、`CALCULATE` 的欄位篩選與
`DATEADD` / `SAMEPERIODLASTYEAR` / `TOTALMTD` 變成彙總上的條件、`DIVIDE` 變成除法。
兩個量值的運算式代入相同的隨機數值，6 次都相等，才算同義。所以：

- `DIVIDE([毛利], [銷售額])` 和 `DIVIDE([營收] - SUM('銷售'[成本金額]), [營收])` 是同一件事（寫法、名稱都不同）。
- `SUM(SALES_AMOUNT)` 和 `SUM(SALES_AMOUNT) + SUM(DISCOUNT_AMOUNT)` 不是，即使兩個都叫「銷售額」。
- 用到還不支援的 DAX（`IF`、`SWITCH`、迭代以外的表格函數…）的量值會列出原因，**不會被當成相同**。

同名但不同義分兩種：讀同一個資料庫 / 檔案（兩份報表的數字會對不起來，要處理），
或讀不同系統（例如零售的「銷售額」和 Superstore 的「銷售額」，名稱相同但本來就是不同的資料）。

### 代碼：讀實際資料逐值比對

- **模型關聯跨兩個系統**（Excel ↔ Oracle）：兩邊各有哪些值對不上、哪些只差大小寫或空白。
- **同一系統內的關聯**：多方有、一方沒有的值（孤兒）；完全對得上就不列。
- **不同報表、不同系統的文字欄位**：值重疊夠多（至少 3 個、較小一邊的一半以上）時列出，可能可以串接。
  數字 ID 不比（1、2、3 在任何兩張表都會重疊）。

### 關聯路徑

所有東西放進同一張圖（報表、資料表、欄位、來源欄位、量值、圖表、作者設定），`cross_report(A, B)` 找兩者之間的最短路徑。
「同義」「引用」「模型關聯」「共用來源欄位」比「在同一張表」便宜，所以路徑會是有意義的那一條：

```
retail 量值「銷售額」 —同義（公式等價）→ store_targets 量值「營收」
store_targets 量值「營收」 ←引用— store_targets 量值「達成率」
```

## 測試報表：財務部的門市目標達成（`datasets/store_targets`）

為了驗證「兩份有關係的報表」，做了第二份零售報表，刻意放進企業裡常見的情況：

| 放了什麼 | 目錄找到的 |
|---|---|
| 讀同一個 Oracle，但表和欄位都改成中文（銷售 = FACT_SALES、實收金額 = SALES_AMOUNT），連線直接寫在 M 裡（零售報表用參數） | 共用 FACT_SALES、DIM_STORE、DIM_DATE |
| 「營收」= `SUM('銷售'[實收金額])` | 與零售的「銷售額」同義（名稱不同） |
| 「毛利率」= `DIVIDE([營收] - SUM(成本), [營收])` | 與零售的「毛利率」同義（寫法不同） |
| 「銷售額」= 實收 + 折扣（財務部定義） | 同名不同義：多了 `SUM(DISCOUNT_AMOUNT)`，也就是零售的「銷售額」+「折扣金額」 |
| 「訂單數」= `COUNTROWS('銷售')` | 同名不同義；而且其實等於零售的「實際明細筆數」 |
| Excel 門市月目標：漏了新開的 S025、多了還沒開的 S026、一筆代碼打成 `s013 ` | 三個都找到，`s013 ` 標成「只差大小寫或空白」 |
| 達成率紅黃綠規則、100% 目標線、45% 毛利率底線、東區特別上色 | 全部讀出 |

另外在 Global Superstore 原始資料裡找到一個沒有埋入的問題：訂單的 Region 有「Canada」，
但負責人表只有「Eastern Canada」「Western Canada」，所以加拿大的訂單對不到負責人。

```powershell
.\.venv\Scripts\python datasets\store_targets\prepare.py     # 從 Oracle 產生 Excel 目標（含刻意的不一致）
.\.venv\Scripts\python datasets\store_targets\build_pbip.py
.\.venv\Scripts\python -m analysis catalog
```

B 報表混用 Oracle 與 Excel 兩種來源，每日變動分析目前只支援單一來源，所以 B 只用在目錄與問答，沒有每日報告。

## 問答

| 題目 | 工具 | 結果 |
|---|---|---|
| 財務部報表的「營收」跟我們的銷售額是同一個東西嗎？ | `cross_report` | 同一件事（名稱不同） |
| 為什麼財務部報表的銷售額跟我們的對不起來？ | `cross_report` | 財務部的多加了折扣金額 |
| 門市目標 Excel 跟系統的門市對得起來嗎？ | `cross_report` | S025 沒目標、S026 不存在、`s013 ` 大小寫與空白 |
| StoreTargets 的作者想凸顯什麼？紅燈門檻？ | `report_emphasis` | 達成率 < 90% 紅、東區 |

開發時抓到一個**錯誤但通過查核**的回答：模型用報表顯示名稱 `StoreTargets:營收` 查詢，工具只認資料集名稱
`store_targets`，查不到；模型改看總覽，把財務部「銷售額」的定義套到「營收」上，回答「不是同一個東西」。
查核只能確認名稱與數字都出自工具結果，抓不到這種張冠李戴。修正在工具端：報表名稱與資料集名稱都接受；
只給報表名稱時，回傳該報表每個量值對到本報表的哪個量值；不同報表的定義一律寫明是哪份報表的哪個量值，
並盡量用本報表的量值改寫（「= retail 的「銷售額」+「折扣金額」」）。這題已加入題庫。

## 限制

- 等價判斷只涵蓋常見的量值寫法；`IF`、`SWITCH`、`FILTER` 表格、`USERELATIONSHIP` 等會列為無法展開。
- 兩個量值展開後相同，代表**在相同篩選下**數字相同；兩份報表各自的報表 / 頁面篩選不同時，畫面上的數字仍可能不同
  （報表篩選在 `metric_lineage` 列出）。
- 伺服器名稱照字面比對：同一台資料庫用 `localhost` 和 `127.0.0.1` 會被當成兩個系統。
- 讀實際資料比對代碼需要能連到該來源（Oracle 用 `.env` 的連線；其他伺服器會在備註列出讀不到）。
