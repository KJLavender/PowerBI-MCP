# Start the WSL containers the simulation depends on (Oracle source, SQL Server catalog for Report Server),
# then check Report Server. Registered as a logon task: after a reboot Report Server (a Windows service)
# needs SQL Server, which lives in WSL and only starts once someone runs WSL.
$root = Split-Path $PSScriptRoot -Parent
$log = Join-Path $root 'logs\services.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Tee-Object -FilePath $log -Append }

$composeDir = wsl -d Ubuntu -- wslpath -a ($root + '\docker')
wsl -d Ubuntu -- bash -c "cd '$composeDir' && docker compose --env-file ../.env up -d >/dev/null 2>&1"
Log "docker compose up: exit $LASTEXITCODE"
$status = ''
for ($i = 0; $i -lt 40; $i++) {
    $status = wsl -d Ubuntu -- docker inspect -f '{{.State.Health.Status}}' pbi-oracle
    if ($status -eq 'healthy') { break }
    Start-Sleep -Seconds 5
}
$mssql = wsl -d Ubuntu -- docker inspect -f '{{.State.Status}}' pbi-mssql
Log "oracle: $status, mssql: $mssql"

$svc = Get-Service PowerBIReportServer -ErrorAction SilentlyContinue
Log "Report Server service: $($svc.Status)"
$api = 'http://localhost:8081/Reports/api/v2.0'
for ($i = 0; $i -lt 12; $i++) {
    try {
        $plans = (Invoke-RestMethod -UseDefaultCredentials "$api/PowerBIReports(Path='/RetailSales')/CacheRefreshPlans" -TimeoutSec 60).value
        Log ("Report Server OK; refresh plans: " + (($plans | ForEach-Object { "$($_.CatalogItemPath) [$($_.ScheduleDescription)] last=$($_.LastStatus) at $($_.LastRunTime)" }) -join '; '))
        break
    } catch {
        if ($i -eq 11) { Log "Report Server not answering: $($_.Exception.Message)" }
        Start-Sleep -Seconds 10
    }
}
