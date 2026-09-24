[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'JobCollectionAgent'),
    [string]$TaskName = 'Job Collection Desktop Agent'
)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'stop.ps1') -InstallRoot $InstallRoot -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "scheduled task removed; retained $InstallRoot for explicit credential/data recovery"
