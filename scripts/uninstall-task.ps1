[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $TaskName = 'T3 Scheduler'
)

$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
} else {
    Write-Output "Scheduled task '$TaskName' is not installed."
}
