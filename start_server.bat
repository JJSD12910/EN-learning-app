@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo Creating .venv with Python 3.13...
  py -3.13 -m venv .venv --system-site-packages
)

echo Using interpreter: %VENV_PY%
"%VENV_PY%" -c "import flask, sqlalchemy"
if errorlevel 1 (
  echo Dependency check failed.
  exit /b 1
)

"%VENV_PY%" backend\quiz_server.py
