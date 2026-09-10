[CmdletBinding()]
param(
    [string] $TaskName = 'T3 Scheduler'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot 'run.ps1'
& (Join-Path $PSScriptRoot 'setup.ps1')
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`" tick"

$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $ProjectRoot
$StartAt = (Get-Date).AddMinutes(1)
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$CurrentPrincipal = New-Object System.Security.Principal.WindowsPrincipal(
    [System.Security.Principal.WindowsIdentity]::GetCurrent()
)
$IsAdministrator = $CurrentPrincipal.IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)
if ($IsAdministrator) {
    $Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType S4U -RunLevel Highest
    $PrincipalMode = 'background (S4U, highest privileges)'
}
else {
    $Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
    $PrincipalMode = 'current-user interactive (runs while signed in)'
}
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Dispatch native T3 Code jobs and coordinate provider quota failover.' -Force | Out-Null
Write-Output "Installed '$TaskName'. First tick: $StartAt"
Write-Output "Principal: $PrincipalMode"
Write-Output "Configuration: $(Join-Path $ProjectRoot 'jobs.toml')"
