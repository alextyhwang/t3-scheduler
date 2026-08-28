$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VirtualPython)) {
    & python -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
}

& $VirtualPython -m pip install --disable-pip-version-check --quiet --editable $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Failed to install T3 Scheduler dependencies.' }

$LocalJobs = Join-Path $ProjectRoot 'jobs.toml'
$ExampleJobs = Join-Path $ProjectRoot 'jobs.example.toml'
if (-not (Test-Path -LiteralPath $LocalJobs)) {
    Copy-Item -LiteralPath $ExampleJobs -Destination $LocalJobs
    Write-Output "Created private local configuration: $LocalJobs"
}
Write-Output "T3 Scheduler environment is ready: $VirtualPython"
