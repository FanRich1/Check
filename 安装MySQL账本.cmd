@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0" || goto failed
if not exist ".venv\Scripts\python.exe" (
  echo Please run the Python environment installer first.
  goto failed
)
".venv\Scripts\uv.exe" sync --frozen --extra local-ocr --link-mode copy
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m scripts.local_mysql prepare
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m shipping_billing.storage.cli --env-file .env init
if errorlevel 1 goto failed
echo MySQL ledger is ready. Restart the workspace to enable it.
pause
exit /b 0
:failed
echo MySQL setup did not complete. Check the message and outputs/mysql logs.
pause
exit /b 1
