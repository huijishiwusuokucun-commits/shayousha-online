@echo off
chcp 932 >nul
title Shayousha Online App (local test)
cd /d "%~dp0"

py --version >nul 2>&1
if errorlevel 1 goto NOPYTHON

py -c "import streamlit, pandas, jpholiday, openpyxl" >nul 2>&1
if errorlevel 1 (
    echo [Setup] Installing required libraries... please wait...
    py -m pip install -r requirements.txt
)

echo.
echo  This is the ONLINE version, running locally for a test.
echo  Open in a browser:  http://localhost:8511
echo  * Keep this window open. Close it to stop.
echo.
py -m streamlit run app.py --server.port 8511
pause
exit /b 0

:NOPYTHON
echo [ERROR] Python not found. Install Python (Microsoft Store) first.
pause
