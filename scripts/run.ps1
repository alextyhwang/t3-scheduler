[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SchedulerArguments
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VirtualPython)) {
    throw 'T3 Scheduler is not set up. Run setup.cmd first.'
}
& $VirtualPython -m t3_scheduler --config (Join-Path $ProjectRoot 'jobs.toml') @SchedulerArguments
exit $LASTEXITCODE
