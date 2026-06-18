@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [prem2api] Starting...
echo   Admin UI:  http://127.0.0.1:3000/admin
echo   API:       http://127.0.0.1:3000/v1
echo.
python prem2api.py %*
pause
