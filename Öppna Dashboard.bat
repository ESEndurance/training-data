@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ES Endurance Dashboard  -  lat detta fonster vara oppet
echo ============================================================
echo   ES Endurance Dashboard
echo   Server: http://localhost:8750
echo   Webblasaren oppnas om nagra sekunder...
echo.
echo   LAT DETTA FONSTER VARA OPPET medan du anvander dashboarden.
echo   Stang fonstret for att stoppa servern.
echo ============================================================
start "" /b powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://localhost:8750/training-dashboard.html'"
"C:\Users\Eric\AppData\Local\Programs\Python\Python312\python.exe" -m http.server 8750 --directory "C:\Users\Eric\garmin-ai\min_lopar_app"
