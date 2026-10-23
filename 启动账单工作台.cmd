@echo off
setlocal
title Shipping Billing Workspace
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0" || goto startup_failed
echo Starting workspace. Please keep this window open.
echo Address: http://127.0.0.1:8765
echo.
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Follow docs/web-workspace.md to install.
  pause
  exit /b 1
)
if not exist "outputs\web" mkdir "outputs\web"
".venv\Scripts\python.exe" -u -m shipping_billing.web.server --project-root "%~dp0." --env-file ".env" --open-browser 2>"outputs\web\startup-error.log"
if errorlevel 1 goto startup_failed
endlocal
exit /b 0

:startup_failed
echo.
echo Workspace failed to start. Error details:
if exist "%~dp0outputs\web\startup-error.log" type "%~dp0outputs\web\startup-error.log"
echo.
echo Please keep this window open to check the error above.
pause
exit /b 1
