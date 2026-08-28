<#
Stop everything: the bot first, then the TTS service.

Order matters here too, the other way round. Stopping TTS first would leave the
bot briefly trying to speak into a service that has gone, which logs a warning
for every reply until it is stopped as well.

    scripts\stop-all.ps1
#>

$ErrorActionPreference = 'Stop'
$Scripts = $PSScriptRoot

Write-Output "=== Athena ==="
& (Join-Path $Scripts 'stop-athena.ps1')

Write-Output "=== TTS ==="
& (Join-Path $Scripts 'stop-tts.ps1')
