@echo off
title Horcrux
color 0A
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

echo.
echo  ================================================
echo   Horcrux - Multi-AI Orchestration System
echo   Modes: Auto / Fast / Standard / Full / Parallel / Deep Refactor
echo  ================================================
echo.

REM --- .env load ---
echo  [1/6] Loading .env...
if exist .env (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
    )
    echo        OK
) else (
    echo        .env not found - using defaults
)

REM --- venv ---
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    echo  [2/6] venv activated
) else (
    echo  [2/6] No venv, using system Python
)

REM --- deps ---
echo  [3/6] Checking dependencies...
py -c "import flask" 2>nul || (
    echo        Installing flask...
    py -m pip install flask --quiet
)
py -c "import requests" 2>nul || (
    echo        Installing requests...
    py -m pip install requests --quiet
)
echo        OK

REM --- API key check ---
echo  [4/6] API keys:

where claude >nul 2>&1 && (
    echo        Claude CLI      : OK
) || (
    echo        Claude CLI      : NOT FOUND
)

where codex >nul 2>&1 && (
    echo        Codex CLI       : OK
) || (
    echo        Codex CLI       : not found [fallback]
)

if defined GROQ_API_KEY (
    echo        GROQ_API_KEY    : set
) else (
    echo        GROQ_API_KEY    : not set
)

echo.

REM --- config check ---
echo  [5/6] Config check:
py -c "import json; c=json.load(open('config.json')); print('        OK')" 2>nul || echo        config check skipped

echo.

REM --- start servers ---
echo  [6/6] Starting servers...

start "Horcrux-Flask" cmd /k "cd /d "%~dp0" && py server.py"

ping -n 4 127.0.0.1 > nul

REM MCP는 Claude Desktop이 mcp_server.js를 직접 실행 (start.bat에서 불필요)

start "" "http://localhost:5000"

echo.
echo  ================================================
echo   RUNNING
echo  ------------------------------------------------
echo   Web UI    : http://localhost:5000
echo   Analytics : http://localhost:5000/api/analytics
echo   MCP       : stdio (Claude Desktop)
echo  ------------------------------------------------
echo   Press any key to stop all servers
echo  ================================================
echo.

pause > nul

echo  Shutting down...
taskkill /fi "WINDOWTITLE eq Horcrux-Flask*" /f >nul 2>&1
echo  Done.
