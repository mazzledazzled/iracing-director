@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause & exit /b 1
)
if "%~1"=="" (
  echo Usage: run.bat ^<step^>
  echo   dashboard    control panel in your browser ^(or double-click dashboard.bat^)
  echo   all          scan + plan + capture + commentary + voice + render
  echo   remake       everything except the scan ^(re-uses the last scanned race^)
  echo   scan         analyse the replay open in iRacing
  echo   plan         pick the highlights ^(edit config.toml and re-run to tweak^)
  echo   review       check, fix and add clips in your browser ^(also runs inside all/remake^)
  echo   capture      record the clips with OBS   ^(--clips 3,5 to redo some^)
  echo   commentary   write the script with Ollama ^(edit output\...\script.json after^)
  echo   voice        turn the script into speech
  echo   render       build the final video
  echo   short        "A race in 30": vertical TikTok video of one driver's race ^(--driver 28, --reuse, --seconds 60^)
  echo   fake         full test with a made-up race, no iRacing needed
  pause & exit /b 0
)
.venv\Scripts\python -m director %*
