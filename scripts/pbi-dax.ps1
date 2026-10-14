# Query the Analysis Services engine that Power BI Desktop runs in the background (localhost only).
#   pbi-dax.ps1 -List                       running Power BI Desktop engines: port, workspace, open file
#   pbi-dax.ps1 -Port 58712 < queries.json  run DAX queries (JSON array of strings), print JSON results;
#                                           an entry starting with "{" is run as a TMSL command (e.g. refresh)
#   pbi-dax.ps1 -Port 58712 -Out DIR < q    write each result to DIR\<n>.tsv instead (large results)
# Uses the ADOMD.NET client that ships with Power BI Desktop; nothing is sent outside the machine.
param([switch]$List, [int]$Port, [string]$Out)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

if ($List) {
    $items = New-Object System.Collections.ArrayList  # not $out: PowerShell names are case-insensitive ($Out)
    foreach ($p in Get-CimInstance Win32_Process -Filter "Name='msmdsrv.exe'") {
        if ($p.CommandLine -notmatch "AnalysisServicesWorkspace") { continue }
        $ports = Get-NetTCPConnection -State Listen -OwningProcess $p.ProcessId -ErrorAction SilentlyContinue |
            Where-Object LocalAddress -eq "127.0.0.1" | Select-Object -ExpandProperty LocalPort
        $parent = Get-Process -Id $p.ParentProcessId -ErrorAction SilentlyContinue
        $title = if ($parent) { [string]$parent.MainWindowTitle } else { "" }
        [void]$items.Add('{"port":' + [int]($ports | Select-Object -First 1) + ',"exe":' +
            (ConvertTo-Json ([string]$p.ExecutablePath)) + ',"title":' + (ConvertTo-Json $title) + '}')
    }
    "[" + ($items.ToArray() -join ",") + "]"
    exit
}

$candidates = @(
    "C:\Program Files\Microsoft Power BI Desktop RS\bin\Microsoft.PowerBI.AdomdClient.dll",
    "C:\Program Files\Microsoft Power BI Desktop\bin\Microsoft.PowerBI.AdomdClient.dll"
) + @(Get-ChildItem "C:\Program Files\WindowsApps\Microsoft.MicrosoftPowerBIDesktop_*\bin\Microsoft.PowerBI.AdomdClient.dll" -ErrorAction SilentlyContinue | ForEach-Object FullName)
$dll = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dll) { throw "Microsoft.PowerBI.AdomdClient.dll not found (is Power BI Desktop installed?)" }
Add-Type -Path $dll

$queries = [Console]::In.ReadToEnd() | ConvertFrom-Json
$conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$Port")
$conn.Open()
if ($Out) {
    # tab-separated files, header row first; tabs/newlines inside values become spaces
    $n = 0
    $status = foreach ($q in $queries) {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $q; $cmd.CommandTimeout = 0
        $path = Join-Path $Out "$n.tsv"; $n++
        try {
            $r = $cmd.ExecuteReader()
            $w = New-Object System.IO.StreamWriter($path, $false, (New-Object System.Text.UTF8Encoding($false)))
            $w.WriteLine((0..($r.FieldCount - 1) | ForEach-Object { $r.GetName($_) }) -join "`t")
            $inv = [Globalization.CultureInfo]::InvariantCulture
            while ($r.Read()) {
                $cells = for ($i = 0; $i -lt $r.FieldCount; $i++) {
                    $v = $r.GetValue($i)
                    if ($v -is [DBNull]) { "" }
                    elseif ($v -is [DateTime]) { $v.ToString("yyyy-MM-dd", $inv) }
                    else { ([Convert]::ToString($v, $inv)) -replace "[`t`r`n]", " " }
                }
                $w.WriteLine($cells -join "`t")
            }
            $w.Close(); $r.Close()
            , @{ file = $path }
        } catch { , @{ error = $_.Exception.Message } }
    }
    $conn.Close()
    ConvertTo-Json @($status) -Compress
    exit
}
$results = foreach ($q in $queries) {
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = $q
    try {
        if ($q.TrimStart().StartsWith("{")) {  # TMSL command (e.g. refresh), not a DAX query
            [void]$cmd.ExecuteNonQuery()
            , @{ rows = @() }
            continue
        }
        $r = $cmd.ExecuteReader()
        $rows = New-Object System.Collections.ArrayList
        while ($r.Read()) {
            $row = @()
            for ($i = 0; $i -lt $r.FieldCount; $i++) { $v = $r.GetValue($i); $row += , $(if ($v -is [DBNull]) { $null } else { [Convert]::ToString($v, [Globalization.CultureInfo]::InvariantCulture) }) }
            [void]$rows.Add($row)
        }
        $r.Close()
        , @{ rows = $rows }
    } catch { , @{ error = $_.Exception.Message } }
}
$conn.Close()
ConvertTo-Json @($results) -Depth 6 -Compress
