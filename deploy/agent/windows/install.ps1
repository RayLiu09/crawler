[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'JobCollectionAgent'),
    [string]$TaskName = 'Job Collection Desktop Agent'
)

$ErrorActionPreference = 'Stop'

function Test-AbsoluteWindowsPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    # IsPathFullyQualified is unavailable in Windows PowerShell 5.1/.NET Framework.
    return ($Path -match '^[A-Za-z]:[\\/]') -or ($Path -match '^\\\\')
}

if (-not (Test-AbsoluteWindowsPath $ExecutablePath) -or
    -not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw 'ExecutablePath must resolve to an existing absolute file'
}
if (-not (Test-AbsoluteWindowsPath $ConfigPath) -or
    -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw 'ConfigPath must resolve to an existing absolute file'
}
$ExecutablePath = [IO.Path]::GetFullPath($ExecutablePath)
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)

$configText = Get-Content -LiteralPath $ConfigPath -Raw
if ($configText -match '(?m)^\s*AGENT_DEVICE_SECRET\s*=') {
    throw 'plaintext AGENT_DEVICE_SECRET is forbidden for managed installation'
}
if ($configText -notmatch '(?m)^\s*AGENT_DEVICE_SECRET_KEYRING_SERVICE\s*=' -or
    $configText -notmatch '(?m)^\s*AGENT_DEVICE_SECRET_KEYRING_USERNAME\s*=') {
    throw 'managed installation requires both keyring locator fields'
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userId = $identity.Name
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
$agentConfig = Join-Path $InstallRoot 'agent.env'
$launcher = Join-Path $InstallRoot 'launcher.ps1'
$stopMarker = Join-Path $InstallRoot 'stop.request'
$runbook = Join-Path $InstallRoot 'RUNBOOK.md'

$managedConfig = ($configText -split '\r?\n' | Where-Object { $_ -notmatch '^\s*SHUTDOWN_MARKER_PATH\s*=' }) -join "`n"
Set-Content -LiteralPath $agentConfig -Value "$managedConfig`nSHUTDOWN_MARKER_PATH=$stopMarker" -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $scriptRoot '..\RUNBOOK.md') -Destination $runbook -Force
$launcherTemplate = Get-Content -LiteralPath (Join-Path $scriptRoot 'launcher.ps1.in') -Raw
$launcherText = $launcherTemplate.Replace('@CONFIG_FILE@', $agentConfig.Replace("'", "''"))
$launcherText = $launcherText.Replace('@EXECUTABLE@', $ExecutablePath.Replace("'", "''"))
Set-Content -LiteralPath $launcher -Value $launcherText -Encoding UTF8

& icacls.exe $InstallRoot /inheritance:r /grant:r "${userId}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to apply the installation directory ACL' }

$actionArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcher`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArguments -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Remove-Item -LiteralPath $stopMarker -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Write-Output 'desktop Agent installed for the current interactive user'
