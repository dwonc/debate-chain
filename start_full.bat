@echo off
chcp 65001 > nul
title Debate Chain v8
color 0A

cd /d D:\Custom_AI-Agent_Project\debate-chain

echo.
echo  ================================================
echo   Debate Chain v8  -  Enhanced Edition
echo   Flask + MCP + Dashboard
echo  ================================================
echo.

REM --- .env load ---
if exist .env (
    echo  [1/4] Loading .env...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
    echo        OK
) else (
    echo  [WARN] .env not found
)

REM --- venv ---
echo  [2/4] Checking venv...
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    echo        .venv activated
) else (
    echo        No .venv, using system Python
)

REM --- deps ---
echo  [3/4] Checking dependencies...
py -c "import flask" 2>nul || py -m pip install flask --quiet
py -c "import sklearn" 2>nul || py -m pip install scikit-learn --quiet
py -c "import google.generativeai" 2>nul || py -m pip install google-generativeai --quiet
echo        OK

REM --- launch Flask ---
echo  [4/4] Starting servers...
start "Debate Chain - Flask" cmd /k "cd /d D:\Custom_AI-Agent_Project\debate-chain && py server_patch.py"

REM --- wait then launch MCP ---
ping -n 4 127.0.0.1 > nul
start "Debate Chain - MCP" cmd /k "cd /d D:\Custom_AI-Agent_Project\debate-chain && py mcp_server.py"

REM --- wait then open browser ---
ping -n 4 127.0.0.1 > nul
start "" "http://localhost:5000/dashboard"

echo.
echo  Flask    : http://localhost:5000
echo  Dashboard: http://localhost:5000/dashboard
echo  MCP      : running in separate window
echo.
echo  Close each window to stop that server.
pause
