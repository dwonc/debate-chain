@echo off
REM Horcrux CLI — shortcut for adaptive_orchestrator.py
REM Usage: horcrux "fix typo" / horcrux --mode full "task" / horcrux classify "task"
python "%~dp0adaptive_orchestrator.py" %*
