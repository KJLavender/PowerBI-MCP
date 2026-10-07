# Power BI 報表（PBIP）

報表與語意模型都由 `powerbi/build_pbip.py` 產生，不需要在 GUI 手動建立。

```
powerbi/
├─ build_pbip.py                    ← 唯一的定義來源：資料表、關聯、量值、報表頁面
├─ RetailSales.pbip                 ← 用 Power BI Desktop 開這個
├─ RetailSales.SemanticModel/definition/   ← TMDL：資料表、關聯、量值與說明（純文字）
└─ RetailSales.Report/definition/          ← PBIR：頁面、視覺效果、欄位綁定（JSON）
```

## 第一次開啟

1. 確認 Oracle 已啟動：`.\scripts\start-oracle.ps1`
2. 開啟 `powerbi\RetailSales.pbip`
3. 常用 → **重新整理**，出現 Oracle 認證視窗時：
   - 左側選 **資料庫**
   - 使用者 `SALES_DW`，密碼見 `.env` 的 `ORACLE_PASSWORD`
   - 帳密只需輸入一次，Power BI 會記住
4. 載入約 270 萬筆，完成後按 **儲存**

> 連線位址請用 `127.0.0.1`，不要用 `localhost`：Windows 會先嘗試 IPv6 的 `::1`，
> 但 WSL 不轉發 IPv6，結果會出現 `ORA-50000: Connection request timed out`。

## 修改模型或報表

1. **關閉 Power BI Desktop**
2. 修改 `build_pbip.py`（例如新增量值、頁面或視覺效果）
3. 執行 `.\.venv\Scripts\python powerbi\build_pbip.py`
4. 重新開啟 `.pbip` 並重新整理

`build_pbip.py` 會清空並重寫兩個 `definition/` 資料夾，在 Power BI 裡手動做的修改會被蓋掉。若要保留 GUI 上的修改，
請把它們同步回 `build_pbip.py`。

## 模型內容

| 項目 | 內容 |
|---|---|
| 資料表 | `FACT_SALES` + 5 個維度表 + `ETL_BATCH_LOG`，從 Oracle `SALES_DW` 匯入 |
| 關聯 | 6 條多對一、單向篩選（見 `RELATIONSHIPS`） |
| 日期表 | `DIM_DATE` 已標記為日期資料表（`FULL_DATE`），已關閉自動日期/時間 |
| 量值 | 21 個，分為「基本指標」「時間比較」「資料品質」三個資料夾，每個都有中文說明 |
| 參數 | `OracleServer`、`OracleSchema`（轉換資料 → 管理參數 可修改） |

每個資料表、欄位、量值都有 `///` 說明，這些說明會存進 TMDL，之後的分析層與 Agent 會用來理解指標的業務意義。

## 報表頁面

| 頁面 | 內容 |
|---|---|
| 總覽 | KPI 卡片（銷售額、毛利率、訂單數、客單價）、每日銷售額 vs 上週同日、各區域 / 品類 / 通路 |
| 區域與門市 | 門市績效矩陣（含週變動%）、各區域每日趨勢、店型比較 |
| 商品 | 各品類每月毛利率與銷售數量、品類 → 商品矩陣 |
| 通路與顧客 | 各通路每日訂單數與折扣率、各顧客分群每月訂單數 |
| 資料品質 | 每日 ETL 載入筆數、ETL 批次明細（ETL 回報筆數 vs 實際明細筆數） |

## 每日更新

- **資料端**：Windows 工作排程器的「PowerBI-Sim Daily ETL」每天 06:00 把前一天的資料寫進 Oracle
- **報表端**：Power BI Desktop 沒有排程更新，開啟後按重新整理即可。要完全模擬「報表自動更新」需要 Power BI Report Server（Phase 4）
