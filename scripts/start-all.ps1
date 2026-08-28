<#
Start everything: the TTS service, then the bot.

Order matters. start-tts.ps1 waits until the service answers /health, so the
bot comes up with speech already available - otherwise the first spoken reply
after a restart hits a service still loading its model and is silently dropped
(the text reply still posts, so it looks like speech is broken rather than
warming up).

    scripts\start-all.ps1
#>

$ErrorActionPreference = 'Stop'
$Scripts = $PSScriptRoot

Write-Output "=== TTS ==="
# Bound to every interface on purpose, so the log page is readable from another
# machine on the LAN. A firewall rule alone cannot do that - loopback means
# nothing is listening on the network interface for the firewall to allow.
#
# Note what it opens: /synthesize takes text and speaks it into the Discord
# channel with no authentication. Fine on a LAN you control, and not something
# to expose beyond it.
& (Join-Path $Scripts 'start-tts.ps1') -BindHost 0.0.0.0
if ($LASTEXITCODE -ne 0) {
    Write-Output ""
    Write-Output "TTS did not start. Continuing anyway - the bot runs fine"
    Write-Output "without speech, it just won't talk."
}

Write-Output ""
Write-Output "=== Athena ==="
& (Join-Path $Scripts 'start-athena.ps1')
