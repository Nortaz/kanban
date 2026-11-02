@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.12 x64 was not found. Install it from python.org and run this file again.
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller==6.11.1
if errorlevel 1 goto fail

set ICON=
if exist app.ico set ICON=--icon app.ico
pyinstaller --noconfirm --clean --windowed --onedir --name Doska %ICON% --add-data "index.html;." app.py
if errorlevel 1 goto fail

echo.
echo Done: dist\Doska\Doska.exe  (the dist\Doska folder can be copied as a whole)
pause
exit /b 0

:fail
echo.
echo Build failed. Copy the text above and report it with the contents of app.log.
pause
exit /b 1
