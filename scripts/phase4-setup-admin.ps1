#Requires -RunAsAdministrator
# Phase 4 one-time setup (needs admin): Power BI Report Server (Developer edition) + Oracle ODAC
# + report server configuration against the SQL Server container. Safe to re-run: each step checks first.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
Start-Transcript -Path (Join-Path $logs 'phase4-setup.log') -Force | Out-Null
$port = 8081
function Step($msg) { Write-Output "`n=== $msg ===" }

try {
    $envs = @{}
    Get-Content (Join-Path $root '.env') | ForEach-Object { if ($_ -match '^(\w+)=(.*)$') { $envs[$matches[1]] = $matches[2] } }

    # 1) Power BI Report Server ------------------------------------------------------------
    Step 'Power BI Report Server'
    if (Get-Service PowerBIReportServer -ErrorAction SilentlyContinue) {
        Write-Output 'already installed'
    } else {
        $exe = Get-ChildItem (Join-Path $root 'installers') -Filter 'Microsoft Power BI Report Server*.exe' | Select-Object -First 1
        $p = Start-Process $exe.FullName -Wait -PassThru -ArgumentList @(
            '/quiet', '/norestart', '/IAcceptLicenseTerms', '/Edition=Dev', '/log', "`"$(Join-Path $logs 'pbirs-install.log')`"")
        Write-Output "installer exit code: $($p.ExitCode)"
        if (-not (Get-Service PowerBIReportServer -ErrorAction SilentlyContinue)) { throw 'Report Server service not found after install' }
    }

    # 2) Oracle ODAC (unmanaged ODP.NET, used by Report Server for Power BI report refresh) ----
    Step 'Oracle ODAC (unmanaged ODP.NET)'
    $odacHome = 'C:\oracle\odac'
    if (Test-Path (Join-Path $odacHome 'odp.net\bin\4\Oracle.DataAccess.dll')) {
        Write-Output 'already installed'
    } else {
        $src = 'C:\oracle\odac_src'
        Expand-Archive (Join-Path $root 'installers\ODAC_x64.zip') -DestinationPath $src -Force
        Push-Location $src
        cmd /c "install.bat odp.net4 $odacHome odac true true" | Out-String | Write-Output
        Pop-Location
    }
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    foreach ($dir in @($odacHome, (Join-Path $odacHome 'instantclient'))) {
        if ((Test-Path $dir) -and ($machinePath -notlike "*$dir*")) { $machinePath = "$dir;$machinePath" }
    }
    [Environment]::SetEnvironmentVariable('Path', $machinePath, 'Machine')

    # 3) Configure the report server ------------------------------------------------------
    Step "Configure report server (catalog database on SQL Server container, URLs on port $port)"
    # ReportingServicesTools is loaded from the project folder (no PowerShellGet / NuGet needed).
    # Database steps are done here via WMI + sqlcmd inside the SQL Server container, the same way
    # Set-RsDatabase does them, so the SqlServer PowerShell module isn't required either.
    Import-Module (Join-Path $root 'installers\ReportingServicesTools\ReportingServicesTools.psd1') -Force
    $ns = Get-CimInstance -Namespace 'root\Microsoft\SqlServer\ReportServer' -ClassName __NAMESPACE | Select-Object -First 1 -ExpandProperty Name
    $ver = Get-CimInstance -Namespace "root\Microsoft\SqlServer\ReportServer\$ns" -ClassName __NAMESPACE | Select-Object -First 1 -ExpandProperty Name
    $instance = $ns -replace '^RS_', ''
    $rsVersion = switch ($ver) { 'v13' { 'SQLServer2016' } 'v14' { 'SQLServer2017' } default { 'SQLServer2019' } }
    Write-Output "WMI instance: $instance $ver -> $rsVersion"
    $common = @{ ReportServerInstance = $instance; ReportServerVersion = $rsVersion }
    $cfg = Get-CimInstance -Namespace "root\Microsoft\SqlServer\ReportServer\$ns\$ver\Admin" -ClassName MSReportServer_ConfigurationSetting

    function Invoke-ContainerSql([string]$sql, [string]$name) {
        $file = Join-Path $logs "$name.sql"
        [IO.File]::WriteAllText($file, $sql, (New-Object Text.UTF8Encoding $false))
        $wslFile = '/mnt/c' + ($file.Substring(2) -replace '\\', '/')
        wsl -d Ubuntu -- docker cp "$wslFile" "pbi-mssql:/tmp/$name.sql" | Out-Null
        $out = wsl -d Ubuntu -- docker exec pbi-mssql /opt/mssql-tools18/bin/sqlcmd -C -S 127.0.0.1 -U sa -P $envs['MSSQL_SA_PASSWORD'] -b -i "/tmp/$name.sql" 2>&1
        if ($LASTEXITCODE -ne 0) { throw "sqlcmd failed for $name : $out" }
        Write-Output "$name.sql executed"
    }

    if (-not $cfg.DatabaseName) {
        $r = Invoke-CimMethod -InputObject $cfg -MethodName GenerateDatabaseCreationScript `
            -Arguments @{ DatabaseName = 'ReportServer'; Lcid = 1033; IsSharePointMode = $false }
        if ($r.HRESULT -ne 0) { throw "GenerateDatabaseCreationScript failed: $($r.HRESULT)" }
        Invoke-ContainerSql $r.Script 'rs-create-db'
        $r = Invoke-CimMethod -InputObject $cfg -MethodName GenerateDatabaseRightsScript `
            -Arguments @{ UserName = 'rsuser'; DatabaseName = 'ReportServer'; IsRemote = $true; IsWindowsUser = $false }
        if ($r.HRESULT -ne 0) { throw "GenerateDatabaseRightsScript failed: $($r.HRESULT)" }
        Invoke-ContainerSql $r.Script 'rs-db-rights'
        # CredentialsType: 0 = Windows, 1 = SQL Server, 2 = service account
        $r = Invoke-CimMethod -InputObject $cfg -MethodName SetDatabaseConnection -Arguments @{
            Server = '127.0.0.1,1433'; DatabaseName = 'ReportServer'; CredentialsType = 1
            UserName = 'rsuser'; Password = $envs['RS_DB_PASSWORD'] }
        if ($r.HRESULT -ne 0) { throw "SetDatabaseConnection failed: $($r.HRESULT)" }
        Write-Output 'database connection set'
    } else {
        Write-Output "database already configured: $($cfg.DatabaseServerName)/$($cfg.DatabaseName)"
    }

    $urls = (Invoke-CimMethod -InputObject $cfg -MethodName ListReservedUrls).UrlString
    if (-not ($urls -match ":$port")) {
        Set-PbiRsUrlReservation @common -ReportServerVirtualDirectory ReportServer -PortalVirtualDirectory Reports -ListeningPort $port
    }
    Write-Output "reserved URLs: $(((Invoke-CimMethod -InputObject $cfg -MethodName ListReservedUrls).UrlString) -join ', ')"
    Restart-Service PowerBIReportServer
    Start-Sleep -Seconds 15
    $cfg = Get-CimInstance -Namespace "root\Microsoft\SqlServer\ReportServer\$ns\$ver\Admin" -ClassName MSReportServer_ConfigurationSetting
    if (-not $cfg.IsInitialized) {
        Initialize-Rs @common
    }
    Write-Output "initialized: $((Get-CimInstance -Namespace "root\Microsoft\SqlServer\ReportServer\$ns\$ver\Admin" -ClassName MSReportServer_ConfigurationSetting).IsInitialized)"
    Step 'Done'
    Write-Output "Web portal: http://localhost:$port/Reports"
}
catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    Write-Output $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
