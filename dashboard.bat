@echo off
rem iRacing Director control panel. Start iRacing (replay) and OBS yourself; do everything else from the page.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause & exit /b 1
)
title iRacing Director dashboard - keep this window open
.venv\Scripts\python -m director dashboard
