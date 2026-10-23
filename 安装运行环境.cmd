@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3.12 scripts\install_windows.py --local-ocr
goto installed
:use_python
python scripts\install_windows.py --local-ocr
:installed
if errorlevel 1 (
  echo Installation failed. Python 3.12 and package-index network access are required.
  pause
  exit /b 1
)
echo Installation finished. You may now open the workspace launcher.
pause
