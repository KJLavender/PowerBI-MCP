# Start the local chat page (if not already running) and optionally open it in the browser.
param([int]$Port = 8090, [switch]$NoBrowser)
$root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$Port"
function Test-Up { try { Invoke-RestMethod "$url/api/info" -TimeoutSec 3 | Out-Null; $true } catch { $false } }

if (-not (Test-Up)) {
    $logs = Join-Path $root 'logs'
    New-Item -ItemType Directory -Force $logs | Out-Null
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process (Join-Path $root '.venv\Scripts\python.exe') -ArgumentList '-m', 'agent.web', '--port', $Port `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'chat.out.log') -RedirectStandardError (Join-Path $logs 'chat.err.log')
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Seconds 2 }
}
if (Test-Up) { "report + chat: http://localhost:$Port  (chat only: http://localhost:$Port/chat)" }
else { throw "chat server did not start, see logs\chat.err.log" }
# open via localhost: same host name as the embedded Report Server report, so its sign-in cookies work
if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
