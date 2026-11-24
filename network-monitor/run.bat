@echo off
REM North Star Monitor — Windows launcher.
REM Double-click, or point a Task Scheduler task at this file to start on boot.
REM Creates the venv and installs dependencies on first run.
cd /d %~dp0
if not exist venv\Scripts\activate.bat (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)
call venv\Scripts\activate.bat
python -c "import uvicorn" 2>nul
if errorlevel 1 (
    echo Installing dependencies (first run only)...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
)
python run.py
if errorlevel 1 pause
