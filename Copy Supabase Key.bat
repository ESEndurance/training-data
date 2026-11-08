@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\Eric\AppData\Local\Programs\Python\Python312\python.exe" -c "import json;print(json.load(open('token/supabase.json'))['service_key'],end='')" | clip
echo.
echo ============================================================
echo   Supabase-nyckel kopierad till URKLIPP.
echo   Klistra in den som GitHub-secret: SUPABASE_SERVICE_KEY
echo ============================================================
echo.
pause
