<#
Re-authorise Spotify after the scope list changes.

Spotipy caches the token with the scopes it was granted. Widening SCOPES makes
that cache insufficient, and the next start needs an interactive browser round
trip - which the scheduled task cannot do, because it runs hidden with nothing
to click. It hangs instead of failing.

Run this by hand, with the bot stopped:

    powershell -File scripts\reauth-spotify.ps1

The work is in reauth_spotify.py rather than a here-string. An earlier version
piped the program to `python -`, which reads from STDIN - passing the text as
an argument instead left python waiting for someone to type a program.
#>

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = '$env:LOCALAPPDATA\..\Local\Python\pythoncore-3.14-64\python.exe'

$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'bot\.py' }
if ($running) {
    Write-Host "Athena is still running (pid $($running.ProcessId))." -ForegroundColor Yellow
    Write-Host "Stop it first:  powershell -File scripts\stop-athena.ps1"
    exit 1
}

if (-not (Test-Path $Python)) {
    Write-Host "Interpreter missing: $Python" -ForegroundColor Red
    exit 1
}

Set-Location $Root
& $Python -X utf8 (Join-Path $PSScriptRoot 'reauth_spotify.py')
exit $LASTEXITCODE
