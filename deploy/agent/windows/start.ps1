[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'JobCollectionAgent'),
    [string]$TaskName = 'Job Collection Desktop Agent'
)

$ErrorActionPreference = 'Stop'
Remove-Item -LiteralPath (Join-Path $InstallRoot 'stop.request') -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Write-Output 'desktop Agent start requested'
