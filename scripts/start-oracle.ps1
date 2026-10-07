# Start the Oracle container in WSL and wait until it is healthy.
$composeDir = wsl -d Ubuntu -- wslpath -a ((Split-Path $PSScriptRoot -Parent) + '\docker')

# docker compose writes progress to stderr; silence it in bash so PowerShell 5.1 doesn't treat it as an error
wsl -d Ubuntu -- bash -c "cd '$composeDir' && docker compose --env-file ../.env up -d >/dev/null 2>&1"
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }
for ($i = 0; $i -lt 40; $i++) {
    $status = wsl -d Ubuntu -- docker inspect -f '{{.State.Health.Status}}' pbi-oracle
    if ($status -eq 'healthy') { Write-Output 'Oracle is healthy.'; exit 0 }
    Start-Sleep -Seconds 5
}
throw "Oracle did not become healthy (last status: $status)"
