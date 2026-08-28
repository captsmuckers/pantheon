<#
Launcher for the scheduled task.

Wraps python bot.py rather than the task calling the interpreter directly,
because bot.py logs to the console only: an unwrapped scheduled run would
discard everything it says, including the import-time failures that happen
before logging is even configured.

Two hazards this has already been bitten by, both worth keeping in mind:

  * Python's logging writes to STDERR. Windows PowerShell 5.1 wraps every
    native stderr line in a NativeCommandError, which under ErrorActionPreference
    'Stop' is terminating - the first line the bot logged killed the wrapper.
    So redirection happens inside cmd.exe, where PowerShell never sees it.

  * A single shared log file is held open by cmd for the life of the run. If a
    previous run is still alive (orphaned, say), the new wrapper cannot write to
    it and dies before logging why. Each run therefore gets its OWN file.

Runs in the interactive session on purpose. The bot drives mpv, Spotify and
Win32 window handles, none of which exist in session 0, so this must never be
converted to a "run whether user is logged on or not" task.

Stopping the task does NOT stop the bot - Task Scheduler kills this wrapper and
leaves cmd/python/mpv orphaned. Use scripts/stop-athena.ps1.
#>

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $PSScriptRoot

# A checkout's own .venv wins over the global interpreter. Voice needs numpy,
# sounddevice and faster-whisper, which are deliberately NOT installed globally
# so that production keeps the dependency set it was tested against - launching
# through the global interpreter there just logs "numpy/sounddevice missing"
# and starts without voice, which looks like the feature is broken.
$Venv   = Join-Path $Root '.venv\Scripts\python.exe'
$Python = if (Test-Path $Venv) { $Venv } else {
    '$env:LOCALAPPDATA\..\Local\Python\pythoncore-3.14-64\python.exe'
}

$LogDir = Join-Path $Root 'logs'
$Keep   = 10

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# One file per run. No two runs ever contend for the same handle.
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log   = Join-Path $LogDir "athena-$stamp.log"

Get-ChildItem $LogDir -Filter 'athena-*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $Python)) {
    "$(Get-Date -Format s)  FATAL  interpreter missing: $Python" | Add-Content $Log
    exit 1
}

Set-Location $Root
"$(Get-Date -Format s)  ---- starting Athena (wrapper pid $PID) ----" | Add-Content $Log

$ErrorActionPreference = 'Continue'
& $env:ComSpec /c "`"$Python`" -X utf8 bot.py >> `"$Log`" 2>&1"

$code = $LASTEXITCODE
"$(Get-Date -Format s)  ---- Athena exited with code $code ----" | Add-Content $Log
exit $code
