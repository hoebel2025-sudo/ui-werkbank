@echo off
setlocal
cd /d "%~dp0"
echo UI-Werkbank: http://127.0.0.1:8119
echo Zum Beenden Strg+C in diesem Fenster druecken.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -B werkbank.py start
) else (
    python -B werkbank.py start
)
if errorlevel 1 pause
