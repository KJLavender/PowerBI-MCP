# Start PRISM (first run: create the Python environment and install the packages) and open it in the browser.
#   -Share      let colleagues on the network open it (listens on all addresses instead of this computer only)
#   -Port 8080  the portal's port; each report's chat gets its own port from 8101
param([int]$Port = 8080, [switch]$Share, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root '.venv\Scripts\python.exe'
$logs = Join-Path $root 'logs'
$url = "http://localhost:$Port"
function Test-Up { try { Invoke-RestMethod "http://127.0.0.1:$Port/api/brand" -TimeoutSec 3 | Out-Null; $true } catch { $false } }

if (-not (Test-Up)) {
    # Python 3.11 or newer
    if (-not (Test-Path $venv)) {
        $python = $null
        foreach ($c in 'py -3', 'python') {
            $exe, $arg = $c -split ' '
            try {
                $v = & $exe @arg -c 'import sys; print(sys.version_info >= (3, 11))' 2>$null
                if ($v -eq 'True') { $python = $exe, $arg; break }
            } catch {}
        }
        if (-not $python) { throw 'PRISM needs Python 3.11 or newer: https://www.python.org/downloads/ (check "Add python.exe to PATH")' }
        Write-Host 'First run: creating the Python environment (.venv)...'
        $exe, $arg = $python
        & $exe @arg -m venv (Join-Path $root '.venv')
    }
    # install / update the packages when requirements.txt changed
    $stamp = Join-Path $root '.venv\requirements.installed'
    $req = Join-Path $root 'requirements.txt'
    if (-not (Test-Path $stamp) -or (Get-Item $req).LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
        Write-Host 'Installing packages (a few minutes the first time)...'
        & $venv -m pip install --disable-pip-version-check -q -r $req
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
        Set-Content $stamp (Get-Date -Format o)
    }

    New-Item -ItemType Directory -Force $logs | Out-Null
    $env:PYTHONIOENCODING = 'utf-8'
    $bind = if ($Share) { '0.0.0.0' } else { '127.0.0.1' }
    Start-Process $venv -ArgumentList '-m', 'prism', '--host', $bind, '--port', $Port `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'prism.out.log') -RedirectStandardError (Join-Path $logs 'prism.err.log')
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Seconds 1 }
    if (-not (Test-Up)) { throw 'PRISM did not start, see logs\prism.err.log' }
}
Write-Host "PRISM: $url"
if ($Share) { Write-Host "Colleagues: http://$([System.Net.Dns]::GetHostName()):$Port (allow the ports 8080 and 8101-8120 in Windows Firewall)" }
if (-not $NoBrowser) { Start-Process $url }
