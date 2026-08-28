<#
Launcher for the text-to-speech service.

Runs its own Python 3.10 environment, NOT the bot's 3.14 - every GPU TTS path
fails on 3.14 for want of wheels, and keeping a second CUDA stack out of the
bot's process protects faster-whisper. See tts/tts_server.py.

Wrapped in cmd.exe for the same reason as start-athena.ps1: Python logs to
stderr, and Windows PowerShell 5.1 wraps every native stderr line in a
terminating NativeCommandError under ErrorActionPreference 'Stop' - the first
line the service logged would kill the wrapper.

Each run gets its own log file, so an orphan holding a handle on the previous
one cannot stop a new run from starting and recording why.
#>

param(
    # 127.0.0.1 keeps it on this machine. Use 0.0.0.0 to read the log page from
    # another PC - a firewall rule alone will NOT do it, because loopback means
    # nothing is listening on the network interface for the firewall to allow.
    #
    # /synthesize has no authentication and speaks whatever it is sent into the
    # Discord channel, so only do this on a LAN you trust.
    [string]$BindHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $PSScriptRoot
$TtsDir = Join-Path $Root 'tts'
$Python = Join-Path $TtsDir '.venv310\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$Keep   = 10

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log   = Join-Path $LogDir "tts-$stamp.log"

Get-ChildItem $LogDir -Filter 'tts-*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $Python)) {
    Write-Output "FATAL: TTS interpreter missing: $Python"
    Write-Output "Create it with:"
    Write-Output "  `"$env:LOCALAPPDATA\..\Local\Programs\Python\Python310\python.exe`" -m venv `"$TtsDir\.venv310`""
    Write-Output "  `"$Python`" -m pip install -r `"$TtsDir\requirements.txt`" --extra-index-url https://download.pytorch.org/whl/cu126"
    exit 1
}

# Already up? Starting a second one just fails on the port and confuses things.
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'tts_server\.py' }
if ($running) {
    Write-Output "TTS already running (pid $($running.ProcessId -join ', '))"
    exit 0
}

Set-Location $TtsDir
"$(Get-Date -Format s)  ---- starting TTS (wrapper pid $PID) ----" | Add-Content $Log

# --preload loads the model now rather than on the first request, so the first
# thing she says isn't delayed by several seconds of model load.
$ErrorActionPreference = 'Continue'
Start-Process -FilePath $env:ComSpec `
    -ArgumentList "/c `"`"$Python`" tts_server.py --preload --host $BindHost >> `"$Log`" 2>&1`"" `
    -WindowStyle Hidden

Write-Output "TTS starting - log: $Log"

# Wait for it to answer, so a caller can rely on it being up when this returns.
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8085/health' -TimeoutSec 3
        if ($health.ready) {
            Write-Output "TTS ready - voice=$($health.voice) device=$($health.device)"
            if ($BindHost -eq '0.0.0.0') {
            # Prefer a real adapter. Hyper-V and WSL create virtual ones
            # (192.168.x.1, 172.x) that sort first and are NOT reachable from
            # another machine, so picking the first address gives an address
            # that looks right and does not work.
            $ip = (Get-NetIPAddress -AddressFamily IPv4 |
                   Where-Object {
                       $_.IPAddress -notlike '127.*' -and
                       $_.IPAddress -notlike '169.254.*' -and
                       $_.InterfaceAlias -notlike '*vEthernet*' -and
                       $_.InterfaceAlias -notlike '*WSL*' -and
                       $_.InterfaceAlias -notlike '*Loopback*'
                   } | Select-Object -First 1).IPAddress
            Write-Output "Logs in a browser: http://$($ip):8085/logs  (also /health)"
        } else {
            Write-Output "Logs in a browser: http://127.0.0.1:8085/logs"
            Write-Output "  (this machine only - rerun with -BindHost 0.0.0.0 to reach it from another PC)"
        }
            exit 0
        }
    } catch { }
}
Write-Output "TTS did not become ready within 90s - check $Log"
exit 1
