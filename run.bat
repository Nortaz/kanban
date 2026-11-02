@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.12 x64 was not found. Install it from python.org and run this file again.
    pause
    exit /b 1
  )
  call ".venv\Scripts\activate.bat"
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  if errorlevel 1 (
    pause
    exit /b 1
  )
) else (
  call ".venv\Scripts\activate.bat"
)

python app.py
if errorlevel 1 pause
