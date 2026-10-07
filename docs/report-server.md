# Phase 4：Power BI Report Server 排程更新

完整模擬企業的地端情境：**Oracle 每天更新 → Report Server 每天自動排程更新報表**。

```
06:00  Windows 排程「PowerBI-Sim Daily ETL」
       ├─ 模擬 ETL：前一天的資料寫進 Oracle
       └─ Phase 2：快照到 DuckDB，產生 reports/<日期>.md
06:30  Report Server 排程更新（SQL Server Agent 觸發）
       └─ 透過 Oracle ODAC 重新匯入 → 報表更新

登入時  Windows 排程「PowerBI-Sim Start Services」
       └─ 啟動 WSL 裡的 Oracle、SQL Server 容器，並檢查 Report Server（結果寫在 logs/services.log）
```

| 元件 | 版本／位置 |
|---|---|
| Power BI Report Server | 1.27（2026 年 9 月），Developer 版，入口 http://localhost:8081/Reports |
| 報表 | http://localhost:8081/Reports/powerbi/RetailSales |
| 目錄資料庫 | SQL Server 2022 Developer（WSL Docker，`pbi-mssql`，含 SQL Server Agent），帳號 `rsuser` |
| Oracle 驅動 | ODAC 23.26.3 非託管版 ODP.NET，`C:\oracle\odac` |
| Power BI Desktop for Report Server | 2.158.1177.0（`C:\Program Files\Microsoft Power BI Desktop RS`） |

## 安裝（一次性）

`installers/` 不在 repo 內，請先把以下檔案放進去：

| 檔案 | 來源 |
|---|---|
| `Microsoft Power BI Report Server_*.exe` | `winget download --id Microsoft.PowerBIReportServer -d installers` |
| `ODAC_x64.zip` | Oracle 官網 [ODAC Xcopy for Windows x64](https://www.oracle.com/database/technologies/net-downloads.html)（23.x） |
| `PBIDesktopSetupRS_x64.exe` | [Power BI Desktop for Report Server](https://www.microsoft.com/download/details.aspx?id=106036)（版本需與 Report Server 對應） |
| `ReportingServicesTools/` | 從 PowerShell Gallery 下載 `https://www.powershellgallery.com/api/v2/package/ReportingServicesTools` 並解壓縮 |

1. `docker compose up -d`（含 `mssql` 服務）
2. 以系統管理員身分執行 `scripts\phase4-setup-admin.ps1`：
   - 安裝 Report Server
   - 安裝 ODAC
   - 用 WMI 產生建庫 SQL，在容器內執行
   - 設定資料庫連線與網址
3. 以系統管理員身分執行 `scripts\phase4-desktop-admin.ps1`，安裝 Report Server 專用版 Desktop

## 發佈／更新報表

```powershell
.\scripts\phase4-publish.ps1
```

腳本會依序執行：

0. 把 PBIR 報表轉成傳統格式
1. 上傳
2. 設定 Oracle 帳密
3. 建立每天 06:30 的排程
4. 立即更新一次，並等待結果

## 踩過的坑（都已處理）

| 問題 | 原因與處理 |
|---|---|
| 上傳 `.pbix` 回傳 422「There was an error uploading your .pbix file」 | **Report Server 不支援 PBIR 報表格式**，只認傳統的 `Report/Layout`。一般版與專用版 Desktop（2026 年 9 月）都會沿用 PBIR，而且沒有開關可以轉回。處理方式是由 `powerbi/to_legacy_pbix.py` 把 PBIR 頁面與視覺效果轉成傳統 Layout，再和專用版 Desktop 存的資料模型重新打包 |
| 專用版 Desktop 無法開啟 `.pbip` | 改開一般版另存的 `.pbix`（兩者版本號相同），再由專用版另存 |
| SQL Server Express 不能用 | 排程更新需要 SQL Server Agent，Express 沒有，所以改用 Developer 版 |
| `Set-RsDatabase` 需要 SqlServer PowerShell 模組 | 改成直接呼叫 WMI 產生 SQL，再透過容器內的 `sqlcmd` 執行 |
| 安裝專用版 Desktop 時回報成功但其實沒裝 | 一般版 Desktop 還開著，MSI 中止，但外層安裝程式仍回傳 0。腳本改成先關閉 Desktop，並用登錄檔確認是否真的安裝 |
| 重開機後 Report Server 連不到資料庫 | 目錄資料庫在 WSL 裡，要有人啟動 WSL 才會跑起來，所以加了登入時啟動容器的排程 |

## 限制

- Report Server 依賴 WSL 裡的 SQL Server，**必須有使用者登入**，容器才會啟動。正式環境的 Report Server 應該使用常駐的 SQL Server。
- `to_legacy_pbix.py` 只轉換這個專案會用到的視覺效果屬性（欄位綁定、排序、格式、標題）。如果之後在 PBIR 加了書籤、篩選器等功能，需要一併擴充轉換器。
