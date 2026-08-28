<#
Stop the text-to-speech service.

Same shape as stop-athena.ps1 and for the same reason: killing the PowerShell
wrapper leaves the cmd.exe/python chain beneath it running as orphans, and an
orphaned cmd keeps the run's log file open, which silently blocks the next
start from writing to it.

Matched on tts_server.py specifically, so this never touches the bot.
#>

$ErrorActionPreference = 'Stop'
$killed = @()

$pythons = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'tts_server\.py' }

foreach ($py in $pythons) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($py.ParentProcessId)" -ErrorAction SilentlyContinue
    $killed += "python $($py.ProcessId)"
    Stop-Process -Id $py.ProcessId -Force -ErrorAction SilentlyContinue

    if ($parent -and $parent.Name -eq 'cmd.exe') {
        $killed += "cmd $($parent.ProcessId)"
        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if ($killed) { "stopped: $($killed -join ', ')" } else { "nothing running" }
