@echo off
title Horcrux
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

if exist .env (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
    )
)

echo.
echo  Horcrux - Starting Flask server...
echo  http://localhost:5000
echo.

py server.py

pause
