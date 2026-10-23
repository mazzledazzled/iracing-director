@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause & exit /b 1
)
echo Installing the Chatterbox voice. This downloads about 5 GB and can take 10-20 minutes.
echo.
.venv\Scripts\python -m director.setup_voice
pause
