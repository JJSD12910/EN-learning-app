$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv with Python 3.13..."
    py -3.13 -m venv .venv --system-site-packages
}

Write-Host "Using interpreter: $venvPython"
& $venvPython -c "import flask, sqlalchemy"
& $venvPython backend\quiz_server.py
