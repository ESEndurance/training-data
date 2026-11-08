@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\Eric\AppData\Local\Programs\Python\Python312\python.exe" garmin_sync.py exporttoken | clip
echo.
echo ============================================================
echo   Garmin-token kopierad till URKLIPP.
echo   Klistra in den som GitHub-secret: GARMIN_TOKEN_B64
echo ============================================================
echo.
pause
