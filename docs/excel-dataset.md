# 用開源 Excel 做成的 Power BI：Global Superstore 實驗

前面的零售資料是模擬器產生的，每天上千筆訂單，雜訊小、規律好抓。這裡改用一份**公開的真實格式 Excel**，照一般分析師的做法做成 Power BI，再接上同一套分析引擎與問答，驗證兩件事：

1. 換一份不是為這個系統設計的資料，分析還準不準？
2. 問答的回答是否真的有根據，而不是幻覺？

```powershell
$env:PBI_DATASET = "superstore"                     # 之後的 analysis / agent 指令都改用這份報表
.\.venv\Scripts\python datasets\superstore\prepare.py       # 下載活頁簿（SHA-256 驗證）並寫出埋入事件的版本
.\.venv\Scripts\python datasets\superstore\build_pbip.py    # 產生 Power BI 專案（Excel 路徑寫進 ExcelPath 參數）
.\.venv\Scripts\python -m analysis snapshot                 # 從 Excel 建立快照
.\.venv\Scripts\python -m analysis evaluate                 # 用埋入的事件評分
.\.venv\Scripts\python -m agent evaluate                    # 問答評分（datasets/superstore/questions.yaml）
.\.venv\Scripts\python -m agent mutation-test               # 查核器突變測試
.\.venv\Scripts\python -m analysis reconcile                # 與 Power BI Desktop 對帳（先開啟報表）
.\.venv\Scripts\python -m agent.web                         # 問答網頁（沒有 Report Server，只有問答面板）
```

## 資料與報表

- **資料**：Tableau 的 Global Superstore 2016 範例活頁簿，`Orders`（51,290 筆訂單明細，2012–2015 年）、`People`（各地區負責人）、`Returns`（退貨訂單）三個工作表。活頁簿不放進 git，`prepare.py` 會下載並比對 SHA-256。
- **Power BI 專案**（`datasets/superstore/powerbi/Superstore.pbip`）：就是一般人用 Power BI Desktop 匯入 Excel 會做出來的東西：
  - Power Query：`Excel.Workbook(File.Contents(ExcelPath))` → 工作表 → 升級標頭 → 變更型別；`Orders` 另外加了「訂單月份」欄位。
  - 關聯：`Orders[Region] → People[Region]`、`Orders[Order ID] → Returns[Order ID]`。原始資料的 `People` 把 Canada 拆成東西兩區，`Orders` 卻只寫 Canada，所以這條關聯有對不上的列，跟真實資料一樣髒。
  - 15 個中文量值：銷售額、利潤、利潤率、訂單數、客單價、折扣率、平均標價（`SUMX(Orders, Orders[Sales] / (1 - Orders[Discount]))` 還原原價）……；另有 3 個用 `CALCULATE` 的量值（退貨訂單數、退貨率、去年同期），分析引擎翻譯不了，會明確標示「無法分析」。
  - 4 頁報表：總覽、市場與地區、商品、顧客與運送。
- **分析設定**（`datasets/superstore/analysis.yaml`）：要看的維度（市場 > 地區 > 國家、品類 > 子品類、顧客分群、運送方式）、交叉組合、門檻。這是唯一需要人寫的「業務知識」；量值、關聯、資料來源都從 Power BI 專案讀。

分析引擎讀 Excel 的方式就是照 Power BI 模型的 M 查詢：找到 `Excel.Workbook` 與工作表名稱，把工作表載入記憶體中的 DuckDB，再用跟 Oracle 一樣的 SQL 產生快照。關聯一律用 LEFT JOIN，跟 Power BI 一樣：對不到維度的列仍算在總數裡（顯示為「(空白)」）。

## 標準答案：在真實資料上埋事件

開源資料沒有「為什麼變化」的標準答案，所以在**原始活頁簿的複本**裡埋入已知事件（`datasets/superstore/scenarios.yaml`），原始資料本身的雜訊全部保留：

| 情境 | 內容 |
|---|---|
| apac-volume-drop | 2015-06-01 起，Asia Pacific 訂單少 55% |
| technology-price-up | 2015-08-03 起，Technology 漲價 30%、數量少 10% |
| furniture-cost-up | 2015-07-06 起，Furniture 成本上漲 20%，售價不變 |
| latam-discount-promo | 2015-10-05～11-01，LATAM 折扣多 25 個百分點、訂單多 60% |
| corporate-churn | 2015-03-02 起五個月內，Corporate 客戶逐步流失 70% |
| europe-tables-subtle | 2015-09-01 起，Europe × Tables 銷量少 30%（**刻意小於雜訊**） |

分析引擎與問答助理都讀不到這個檔案，只有 `prepare.py` 與評分程式會讀。

## 第一個發現：小量資料，原本的判斷方式會大量誤報

這份資料一天平均只有 **18 筆訂單**（零售模擬資料是上千筆）。直接套用原本的設定，在**完全沒有埋事件的原始資料**上，每天會報出 **7.3 則「顯著變化」**——幾乎全是雜訊。逐一診斷後做了這些調整（都是設定，零售資料的結果不受影響，已用回歸測試確認）：

| 問題 | 調整 |
|---|---|
| 小範圍（例如某個小國家一週只有幾筆）的變化率動輒 ±100% | `min_rows: 60`：本期與比較期間各至少 60 筆明細才判斷；不足時判讀為「樣本太少，無法判斷」，**不下結論** |
| 利潤可能是負數或接近 0，變化率會出現 +14172% 這種數字 | `roles.signed: [利潤]`：不以變化率判斷，改看利潤率 |
| 用「單一成員自己去年的變化」做季節校正，去年那個值本身就是雜訊 | `seasonal: total`：成員改用整體去年同期的變化做季節校正；歷史基準拉長為一年 |
| 量價型態把 ±5% 的雜訊說成「量減」「價升」，甚至推論「可能是漲價影響買氣」 | `pattern_min_z: 2`、`price_index_move: 0.08`：只有至少「偏大」的變化才列入型態 |
| 成員門檻 | 在原始資料上校準：`z_alert_member` 4 → 5 |

## 偵測結果

| | 原始資料（沒有事件） | 埋入事件後 |
|---|---|---|
| 每天的背景變化 | 0.2 則 | 0.4 則 |
| 「偵測到」不存在的情境 | 0 次 | — |
| 可偵測情境的偵出 | — | **4/5** |

| 情境 | 偵測到 | 延遲 | 證據 |
|---|---|---|---|
| technology-price-up | 2015-08-29 | 26 天 | 近 28 日 Technology 利潤率 +21.3 個百分點 |
| furniture-cost-up | 2015-08-30 | 55 天 | 與去年同期比，Furniture 利潤率 -16.2 個百分點 |
| latam-discount-promo | 2015-10-08 | 3 天 | 近 7 日 LATAM 利潤率 -51.8 個百分點 |
| corporate-churn | 2015-06-13 | 103 天（逐步流失） | 與去年同期比，Corporate 訂單數 -40.0% |
| apac-volume-drop | **未偵測** | — | 年減約 51%，但 z 只到 -4.4～-4.8，未達門檻 5 |
| europe-tables-subtle | 未偵測（預期） | — | 每月只有個位數筆明細，判讀為「樣本太少」 |

門檻是取捨：改成 4.5 時亞太仍未偵測，原始資料上卻開始出現「偵測到不存在情境」的巧合，所以維持 5。**這份資料能分辨的最小事件大約是：一個市場的訂單量少一半以上、或折扣率多二十幾個百分點**；更小的變化任何方法都分不出來，系統會老實說「未達顯著」或「樣本太少」。

## 與 Power BI 對帳

`python -m analysis reconcile` 連到 Power BI Desktop 背景的本機 Analysis Services 引擎（`scripts/pbi-dax.ps1`，用 Power BI Desktop 內附的 ADOMD.NET），用 DAX 重算一次，跟分析引擎逐一比對：

- 抽樣 6 個日期 × 1／7／28 日 × 整體、各維度前 3 大成員、交叉組合 × 12 個量值。
- **Superstore：5,700 個數字，0 個不一致**；零售報表：4,176 個數字，0 個不一致。

這一層驗證的是 LLM 管不到的部分：DAX 翻譯（包含 `SUMX` 逐列運算、`DISTINCTCOUNT`、`DIVIDE` 巢狀量值）、關聯、快照，都和報表上看到的數字一致。Power BI 專案剛開啟、還沒有資料時，也可以用 TMSL 的 `refresh` 指令經由同一個連接埠重新整理，不需要點畫面。

## 問答評分

見 [docs/agent.md](agent.md) 的「評分結果」：15 題中使用者拿到正確答案 14 題，查核通過卻答錯 0 題，突變測試攔下 94%。

## 限制

- **一天 18 筆訂單的資料，只能分析大事件**：單日比較完全不可靠（設定裡沒有 `day` 時間窗，問「某一天」會改用近 7 日並註明）；近 7 日也只有市場層級勉強可用。
- 分析設定（維度、門檻）仍需要人依資料特性決定；門檻要用「沒有事件的歷史資料」校準，而不是直接沿用。
- 埋入的事件是人為的；真實業務事件的型態可能更複雜（例如同時發生、逐步發生），應再用公司過去實際發生的事件建立題庫。
