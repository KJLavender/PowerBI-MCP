# Register a Windows scheduled task (current user, no admin needed) that runs run-daily.ps1.
param([string]$At = '06:00')

$script = Join-Path $PSScriptRoot 'run-daily.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: if the PC was off at $At, run as soon as it is back on
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName 'PowerBI-Sim Daily ETL' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Load simulated sales into Oracle (PowerBI MCP project)' -Force
