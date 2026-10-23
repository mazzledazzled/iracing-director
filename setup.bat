@echo off
setlocal
cd /d "%~dp0"
echo.
echo === iRacing Replay Director setup ===
echo.

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install. Then run setup.bat again.
  pause & exit /b 1
)

for /f "delims=" %%v in ('%PY% --version') do echo Using %%v
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)
echo Installing Python packages...
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q || (echo pip install failed - see the red lines above & pause & exit /b 1)

echo Installing the Kokoro voice...
.venv\Scripts\python -m pip install kokoro-onnx -q
if errorlevel 1 (
  rem kokoro-onnx says "Python 3.13 or older" but runs fine on newer Python: install it without that check
  .venv\Scripts\python -m pip install onnxruntime phonemizer espeakng-loader -q
  .venv\Scripts\python -m pip install kokoro-onnx --no-deps --ignore-requires-python -q
)
.venv\Scripts\python -c "import kokoro_onnx" 2>nul
if errorlevel 1 (
  echo NOTE: the Kokoro voice couldn't be installed. Race in 30 doesn't need it; the highlights
  echo       commentary will use Chatterbox ^(setup_chatterbox.bat^) or the built-in Windows voice.
)

echo Downloading the broadcast fonts...
.venv\Scripts\python -c "from director.graphics import ensure_fonts; ensure_fonts()"

echo Downloading the commentator voice model (one time, about 340 MB)...
.venv\Scripts\python -c "from director.tts import ensure_kokoro_models; ensure_kokoro_models()"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg not on PATH - trying to install it with winget...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo If that failed, the bundled ffmpeg from imageio-ffmpeg will be used instead.
)

where ollama >nul 2>nul
if errorlevel 1 (
  echo.
  echo Ollama is not installed. Installing with winget...
  winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
  echo After it installs, open a NEW terminal and run:  ollama pull llama3.1:8b
) else (
  echo Pulling the commentary model ^(one time, about 4.7 GB^)...
  ollama pull llama3.1:8b
)

echo.
echo Setup done. Try it without iRacing first:   run.bat fake
echo Then open a replay in iRacing, start OBS, and run:   run.bat all
pause
