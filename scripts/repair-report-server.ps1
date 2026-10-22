# Repair Power BI Report Server after its catalog database stalled, and finish publishing RetailSales:
# restart the service, set the Oracle credentials, recreate the daily 06:30 refresh and run it once.
# Run once as administrator (one UAC prompt):
#   powershell -ExecutionPolicy Bypass -File scripts\repair-report-server.ps1
# The service log lines with errors are copied to data\rs-errors.txt for diagnosis.
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
$out = Join-Path $root 'data\rs-errors.txt'

Write-Output '1) restart the Report Server service'
$svc = Get-Service | Where-Object { $_.Name -match 'PowerBIReportServer' } | Select-Object -First 1
if ($svc) { Restart-Service $svc.Name -Force } else { Write-Output '   service not found' }

Write-Output '2) wait for the portal'
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 10
    $ok = try { (Invoke-WebRequest -UseDefaultCredentials -UseBasicParsing 'http://localhost:8081/Reports/api/v2.0/CatalogItems' -TimeoutSec 60).StatusCode -eq 200 } catch { $false }
} until ($ok -or (Get-Date) -gt $deadline)
Write-Output "   portal up: $ok"

Write-Output '3) credentials, daily refresh plan, one refresh now'
$lines = Get-Content (Join-Path $PSScriptRoot 'phase4-publish.ps1')
$from = ($lines | Select-String -Pattern "^Write-Output '2\) data source").LineNumber
$steps = Join-Path $env:TEMP 'publish-steps-2-4.ps1'
Set-Content -Path $steps -Encoding UTF8 -Value ((($lines[0..15] -join "`n").Replace('$PSScriptRoot', "'$PSScriptRoot'")) + "`n" + ($lines[($from - 1)..($lines.Count - 1)] -join "`n"))
& $steps

Write-Output '4) recent errors in the service logs -> data\rs-errors.txt'
$logs = Get-ChildItem 'C:\Program Files\Microsoft Power BI Report Server\PBIRS\LogFiles' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 4
$logs | ForEach-Object { "== $($_.Name)"; Get-Content $_.FullName -Tail 3000 | Select-String -Pattern 'error|exception|fail' | Select-Object -Last 40 } |
    Out-File -FilePath $out -Encoding utf8
Write-Output "done; errors copied to $out"
Read-Host 'Press Enter to close'
