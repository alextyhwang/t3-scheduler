$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VirtualPython)) {
    throw 'T3 Scheduler is not set up. Run setup.cmd first.'
}
& $VirtualPython -m unittest discover -s (Join-Path $ProjectRoot 'tests') -v
exit $LASTEXITCODE
