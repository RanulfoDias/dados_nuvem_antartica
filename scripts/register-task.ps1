param(
    [string]$TaskName = 'MAESTRO Drive Bridge',
    [switch]$Online
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Config = Join-Path $Root 'config.toml'
if (-not (Test-Path $Python) -or -not (Test-Path $Config)) { throw 'Run install.ps1 and configure config.toml first' }
$Arguments = '-m maestro_bridge --config "' + $Config + '" run'
if (-not $Online) { $Arguments += ' --offline' }
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Read-only MAESTRO acquisition ingestion and persistent Drive outbox'
