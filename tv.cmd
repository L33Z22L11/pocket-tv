@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -c "import textual, av, serial, PIL" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements-tui.txt
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe host\tui.py %*
