@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
echo ============================================
echo   Voyant (GuanYu) - AI Screen Assistant Workbench
echo   Console: http://127.0.0.1:8770
echo   Close this window to STOP.
echo ============================================
"%PY%" bot.py
echo.
echo Program exited. See messages above if something went wrong.
pause
