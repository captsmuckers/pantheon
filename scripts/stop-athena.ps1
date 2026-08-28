<#
Stop Athena properly.

Stop-ScheduledTask is NOT enough on its own. It kills the PowerShell wrapper
and leaves the chain below it - cmd.exe, python bot.py, and the mpv it spawned -
running as orphans. Worse, the orphaned cmd keeps holding the run's log file
open, which is exactly what silently blocked a later start.

This walks the chain the other way: find python running bot.py, take its cmd
parent with it, then close mpv.

ORDER MATTERS. Killing mpv first makes the player's watchdog immediately
relaunch it - observed live, producing an orphaned athena-mpv-<pid>-2 seconds
after the stop. Python dies first so nothing is left to resurrect anything.

The mpv sweep matches the IPC pipe name (athena-mpv-*), which the bot sets, so an
mpv the user opened themselves is left alone. It deliberately does NOT require
the owning python to still exist, or orphans from an earlier crash would linger.
#>

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

$killed = @()

# 1. The owner first, with the cmd it was launched from.
$pythons = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'bot\.py' }

foreach ($py in $pythons) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($py.ParentProcessId)" -ErrorAction SilentlyContinue
    $killed += "python $($py.ProcessId)"
    Stop-Process -Id $py.ProcessId -Force -ErrorAction SilentlyContinue

    if ($parent -and $parent.Name -eq 'cmd.exe') {
        $killed += "cmd $($parent.ProcessId)"
        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 2. Give the watchdog no chance to have already relaunched, then sweep every
#    mpv the bot owns - including ones orphaned by an earlier crash.
if ($pythons) { Start-Sleep -Milliseconds 1500 }

Get-CimInstance Win32_Process -Filter "Name LIKE 'mpv%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match '(athena|nyx)-mpv-' } |
    ForEach-Object {
        $killed += "mpv $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# And the task itself, so Scheduler does not think it is still running.
# Both names during the Nyx -> Athena changeover: the task may still be
# registered under the old one, and the mpv sweep above matches both pipe
# prefixes for the same reason. An mpv started before the rename is still an
# orphan that has to be cleaned up.
foreach ($task in 'Athena media bot', 'Nyx media bot') {
    try { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue } catch { }
}

if ($killed) { "stopped: $($killed -join ', ')" } else { "nothing running" }
