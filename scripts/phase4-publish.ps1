# Publish powerbi\RetailSales.pbix to Power BI Report Server, store the Oracle credentials,
# create a daily scheduled refresh and run it once. No admin rights needed (Windows auth).
param(
    [string]$Pbix = (Join-Path (Split-Path $PSScriptRoot -Parent) 'powerbi\RetailSales.pbix'),
    [string]$Portal = 'http://localhost:8081/Reports',
    [string]$At = '06:30'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Import-Module (Join-Path $root 'installers\ReportingServicesTools\ReportingServicesTools.psd1') -Force -WarningAction SilentlyContinue
$envs = @{}
Get-Content (Join-Path $root '.env') | ForEach-Object { if ($_ -match '^(\w+)=(.*)$') { $envs[$matches[1]] = $matches[2] } }
$name = [IO.Path]::GetFileNameWithoutExtension($Pbix)
$item = "/$name"
$api = "$Portal/api/v2.0"

# Report Server only accepts the legacy report format: rebuild RetailSales.pbix from the PBIR project,
# using the model (with data) inside RetailSales_RS.pbix saved by Power BI Desktop for Report Server.
Write-Output '0) convert PBIR report -> legacy Report/Layout'
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'powerbi\to_legacy_pbix.py')
if ($LASTEXITCODE -ne 0) { throw 'to_legacy_pbix.py failed' }
if (-not (Test-Path $Pbix)) { throw "找不到 $Pbix" }

Write-Output "1) upload $Pbix -> $item"
Write-RsRestCatalogItem -Path $Pbix -RsFolder '/' -ReportPortalUri $Portal -Overwrite -RestApiVersion v2.0

Write-Output '2) data source credentials (Oracle, database auth)'
$sources = Get-RsRestItemDataSource -RsItem $item -ReportPortalUri $Portal
foreach ($ds in $sources) {
    Write-Output "   $($ds.DataModelDataSource.Kind) $($ds.ConnectionString)"
    $ds.DataModelDataSource.AuthType = 'UsernamePassword'
    $ds.DataModelDataSource.Username = $envs['ORACLE_USER']
    $ds.DataModelDataSource.Secret = $envs['ORACLE_PASSWORD']
}
Set-RsRestItemDataSource -RsItem $item -RsItemType PowerBIReport -DataSources $sources -ReportPortalUri $Portal

Write-Output "3) daily scheduled refresh at $At"
foreach ($plan in @(Get-RsRestCacheRefreshPlan -RsReport $item -ReportPortalUri $Portal)) {
    if ($plan) { Remove-RsRestCacheRefreshPlan -Id $plan.Id -ReportPortalUri $Portal -Confirm:$false }
}
$start = (Get-Date).Date.AddDays(1).Add([TimeSpan]::Parse($At)).ToString('yyyy-MM-ddTHH:mm:sszzz')
$recurrence = @{ DailyRecurrence = @{ DaysInterval = '1' } }
$plan = New-RsRestCacheRefreshPlan -RsItem $item -ReportPortalUri $Portal -Description '每日 Oracle 資料更新' `
    -StartDateTime $start -Recurrence $recurrence
$plan = Get-RsRestCacheRefreshPlan -RsReport $item -ReportPortalUri $Portal | Select-Object -First 1
Write-Output "   plan $($plan.Id): $($plan.ScheduleDescription)"

Write-Output '4) run the refresh once now'
Start-RsRestCacheRefreshPlan -Id $plan.Id -ReportPortalUri $Portal
$deadline = (Get-Date).AddMinutes(15)
do {
    Start-Sleep -Seconds 10
    $p = Invoke-RestMethod -UseDefaultCredentials "$api/CacheRefreshPlans($($plan.Id))"
    Write-Output "   status: $($p.LastStatus)"
} until (($p.LastStatus -match 'Completed|Failed|Error') -or (Get-Date) -gt $deadline)
$hist = Get-RsRestCacheRefreshPlanHistory -Id $plan.Id -ReportPortalUri $Portal | Select-Object -First 1
Write-Output "   last run: $($hist.StartTime) -> $($hist.EndTime) $($hist.Status) $($hist.Message)"
Write-Output "Report: $Portal/powerbi$item"
