# 報表目錄：多份報表之間的關係

> 由 `python -m analysis catalog` 產生。資料來源、量值定義與關聯由報表檔案（TMDL、Power Query、版面）讀出；代碼對應讀實際資料逐值比對。量值「同義」= 展開成實體欄位上的彙總後公式等價，與名稱、寫法無關。

## 報表

| 報表 | 資料集 | 資料表 | 量值 | 讀取的來源 |
|---|---|---|---|---|
| RetailSales | `retail` | 7 | 21 | Oracle SALES_DW |
| retail_pbix | `retail_pbix` | 7 | 21 | Oracle SALES_DW |
| StoreTargets | `store_targets` | 4 | 8 | Excel store_targets.xlsx、Oracle SALES_DW |
| Superstore | `superstore` | 3 | 15 | Excel GlobalSuperstore.xlsx |

## 共用的資料來源

同一張實體資料表被多份報表讀取（不論在報表裡叫什麼名字）。

| 來源 | 各報表裡的名稱 |
|---|---|
| Oracle SALES_DW.DIM_CHANNEL | `retail`：DIM_CHANNEL、`retail_pbix`：DIM_CHANNEL |
| Oracle SALES_DW.DIM_CUSTOMER | `retail`：DIM_CUSTOMER、`retail_pbix`：DIM_CUSTOMER |
| Oracle SALES_DW.DIM_DATE | `retail`：DIM_DATE、`retail_pbix`：DIM_DATE、`store_targets`：日期 |
| Oracle SALES_DW.DIM_PRODUCT | `retail`：DIM_PRODUCT、`retail_pbix`：DIM_PRODUCT |
| Oracle SALES_DW.DIM_STORE | `retail`：DIM_STORE、`retail_pbix`：DIM_STORE、`store_targets`：門市 |
| Oracle SALES_DW.ETL_BATCH_LOG | `retail`：ETL_BATCH_LOG、`retail_pbix`：ETL_BATCH_LOG |
| Oracle SALES_DW.FACT_SALES | `retail`：FACT_SALES、`retail_pbix`：FACT_SALES、`store_targets`：銷售 |

## 同義量值（公式等價）

**名稱不同、意思相同**（最容易被忽略）：

- `retail`「實際明細筆數」、`retail_pbix`「實際明細筆數」、`store_targets`「訂單數」：COUNTROWS(FACT_SALES)
- `retail`「銷售額」、`retail_pbix`「銷售額」、`store_targets`「營收」：SUM(FACT_SALES.SALES_AMOUNT)

名稱與意思都相同：「ETL 載入筆數」（`retail`、`retail_pbix`）；「成本」（`retail`、`retail_pbix`）；「毛利」（`retail`、`retail_pbix`、`store_targets`）；「毛利率」（`retail`、`retail_pbix`、`store_targets`）；「訂單數」（`retail`、`retail_pbix`）；「銷售數量」（`retail`、`retail_pbix`）；「客單價」（`retail`、`retail_pbix`）；「平均成交單價」（`retail`、`retail_pbix`）；「折扣金額」（`retail`、`retail_pbix`）；「折扣率」（`retail`、`retail_pbix`）；「購買會員數」（`retail`、`retail_pbix`）；「銷售額 前一日」（`retail`、`retail_pbix`）；「銷售額 上週同日」（`retail`、`retail_pbix`）；「銷售額 週變動%」（`retail`、`retail_pbix`）；「銷售額 去年同期」（`retail`、`retail_pbix`）；「銷售額 YoY%」（`retail`、`retail_pbix`）；「銷售額 MTD」（`retail`、`retail_pbix`）；「毛利率 上週同日」（`retail`、`retail_pbix`）；「訂單數 上週同日」（`retail`、`retail_pbix`）

## 同名但定義不同

讀同一個資料庫 / 檔案，名稱相同但算法不同：兩份報表的數字會對不起來。

### 「銷售額」

- `retail`「銷售額」、`retail_pbix`「銷售額」：SUM(FACT_SALES.SALES_AMOUNT)
- `store_targets`「銷售額」：SUM(FACT_SALES.SALES_AMOUNT) + SUM(FACT_SALES.DISCOUNT_AMOUNT)
- 差別：`store_targets` 用到 SUM(FACT_SALES.DISCOUNT_AMOUNT)（另一邊沒有）

### 「訂單數」

- `retail`「訂單數」、`retail_pbix`「訂單數」：DISTINCTCOUNT(FACT_SALES.ORDER_ID)
- `store_targets`「訂單數」：COUNTROWS(FACT_SALES)
- 差別：`retail` 用到 DISTINCTCOUNT(FACT_SALES.ORDER_ID)；`store_targets` 用到 COUNTROWS(FACT_SALES)（另一邊沒有）

讀不同系統的同名量值（不同的業務資料，名稱相同不代表同一件事）：「銷售額」（`retail`「銷售額」、`retail_pbix`「銷售額」／`superstore`「銷售額」）、「成本」（`retail`「成本」、`retail_pbix`「成本」／`superstore`「成本」）、「訂單數」（`retail`「訂單數」、`retail_pbix`「訂單數」／`superstore`「訂單數」）、「銷售數量」（`retail`「銷售數量」、`retail_pbix`「銷售數量」／`superstore`「銷售數量」）、「客單價」（`retail`「客單價」、`retail_pbix`「客單價」／`superstore`「客單價」）、「折扣金額」（`retail`「折扣金額」、`retail_pbix`「折扣金額」／`superstore`「折扣金額」）、「折扣率」（`retail`「折扣率」、`retail_pbix`「折扣率」／`superstore`「折扣率」）、「銷售額 去年同期」（`retail`「銷售額 去年同期」、`retail_pbix`「銷售額 去年同期」／`superstore`「銷售額 去年同期」）

## 代碼對應（讀實際資料比對）

### store_targets:門市月目標[門市代碼] ↔ store_targets:門市[門市代碼]

- 類型：模型關聯跨兩個系統
- 來源：store_targets.xlsx／門市月目標.門市代碼（26 個值） ↔ DIM_STORE.STORE_CODE（25 個值），相同 24 個
- 只差大小寫或空白：「s013 」≈「S013」
- 只在 store_targets.xlsx／門市月目標.門市代碼：「S026」
- 只在 DIM_STORE.STORE_CODE：「S025」

### store_targets:門市月目標[年月] ↔ store_targets:日期[年月]

- 類型：模型關聯跨兩個系統
- 來源：store_targets.xlsx／門市月目標.年月（12 個值） ↔ DIM_DATE.YEAR_MONTH（48 個值），相同 12 個
- 只在 DIM_DATE.YEAR_MONTH：「2024-01」、「2024-02」、「2024-03」、「2024-04」、「2024-05」、「2024-06」、「2024-07」、「2024-08」、「2024-09」、「2024-10」、「2024-11」、「2024-12」…（共 36 個）

### superstore:Orders[Region] ↔ superstore:People[Region]

- 類型：關聯的多方有對不到的值（孤兒）
- 來源：GlobalSuperstore.xlsx／Orders.Region（23 個值） ↔ GlobalSuperstore.xlsx／People.Region（24 個值），相同 22 個
- 只在 GlobalSuperstore.xlsx／Orders.Region：「Canada」

## 無法展開的量值

用到尚未支援的 DAX，無法判斷與其他報表的量值是否同義（不會被當成相同）。

（沒有）

## 報表作者的凸顯（摘要）

**StoreTargets**（`store_targets`）

- 目標達成／各區域達成率：Y 軸常數線「目標 100%」= 100%
- 目標達成／各區域營收：只把 門市[區域] = 東區（#E81123（紅）） 改成特定顏色（只改一個成員，通常是要凸顯它）
- 目標達成／各門市達成率：「達成率」的背景色規則：達成率 < 90% → #E81123（紅）；達成率 ≥ 90% 且 達成率 < 100% → #F2C80F（黃）；達成率 ≥ 100% → #3BB44A（綠）
- 毛利／各門市毛利：「毛利率」的字色規則：毛利率 < 45% → #E81123（紅）
- 毛利／每月毛利率：Y 軸常數線「毛利率底線 45%」= 45%
