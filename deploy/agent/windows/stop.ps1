[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'JobCollectionAgent'),
    [string]$TaskName = 'Job Collection Desktop Agent',
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$stopMarker = Join-Path $InstallRoot 'stop.request'
New-Item -ItemType File -Path $stopMarker -Force | Out-Null
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
while ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 1
}
if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
    throw 'Agent cooperative stop timed out; the scheduled task was forcibly stopped'
}
Remove-Item -LiteralPath $stopMarker -Force -ErrorAction SilentlyContinue
Write-Output 'desktop Agent stopped cleanly'
