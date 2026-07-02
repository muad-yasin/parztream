@echo off
setlocal

rem Run from the project root regardless of where this script is invoked from.
cd /d "%~dp0..\.."

if exist "C:\ProgramData\parztream\parztream.env.bat" (
    call "C:\ProgramData\parztream\parztream.env.bat"
) else (
    echo WARNING: C:\ProgramData\parztream\parztream.env.bat not found -- running with no configured media dirs/auth.
)

call .venv\Scripts\activate.bat
rem Single worker only -- see the comment in deploy\systemd\parztream.service
uvicorn app.main:app --host 0.0.0.0 --port 8000
