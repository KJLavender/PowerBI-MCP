#Requires -RunAsAdministrator
# Install Power BI Desktop optimized for Report Server (needed to save .pbix files the server accepts).
# The bundle can return 0 even when the MSI fails (e.g. another Power BI Desktop is running), so the
# result is judged by the uninstall registry entry, not the exit code.
$root = Split-Path $PSScriptRoot -Parent
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$result = Join-Path $logs 'pbidesktop-rs-result.txt'
Get-Process PBIDesktop, msmdsrv -ErrorAction SilentlyContinue | Stop-Process -Force
$exe = Join-Path $root 'installers\PBIDesktopSetupRS_x64.exe'
$p = Start-Process $exe -Wait -PassThru -ArgumentList @('-quiet', '-norestart', 'ACCEPT_EULA=1', '-log', "`"$(Join-Path $logs 'pbidesktop-rs-install.log')`"")
$installed = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
    Where-Object { $_.DisplayName -like 'Microsoft Power BI Desktop*' } | Select-Object -First 1
if ($installed) {
    "installed: $($installed.DisplayName) $($installed.DisplayVersion) (bundle exit $($p.ExitCode))" | Tee-Object -FilePath $result
} else {
    "FAILED: not installed (bundle exit $($p.ExitCode)); see $logs\pbidesktop-rs-install_000_ProductMSI.log" | Tee-Object -FilePath $result
    exit 1
}
