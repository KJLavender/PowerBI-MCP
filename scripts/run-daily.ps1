# Daily job: make sure Oracle is up, load missing business dates (ETL simulation),
# then snapshot into DuckDB and write the daily change report for yesterday.
$root = Split-Path $PSScriptRoot -Parent
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'daily.log'
$python = Join-Path $root '.venv\Scripts\python.exe'

$env:PYTHONIOENCODING = 'utf-8'
Set-Location $root
& (Join-Path $PSScriptRoot 'start-oracle.ps1') *>> $log
& $python -m simulator daily *>> $log
if ($LASTEXITCODE -ne 0) { throw "simulator daily failed, see $log" }
& $python -m analysis daily *>> $log
if ($LASTEXITCODE -ne 0) { throw "analysis daily failed, see $log" }
