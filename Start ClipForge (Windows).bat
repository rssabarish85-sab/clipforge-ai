@echo off
title ClipForge AI
cd /d "%~dp0backend"
echo == ClipForge AI ==

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing ffmpeg - this happens only once...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo ffmpeg installed. Close this window and double-click the start file again.
  pause
  exit /b
)

if not exist bin\deno.exe (
  where deno >nul 2>nul
  if errorlevel 1 (
    echo Downloading deno - needed for YouTube links...
    if not exist bin mkdir bin
    powershell -NoProfile -Command "Invoke-WebRequest https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip -OutFile bin\deno.zip; Expand-Archive -Force bin\deno.zip bin; Remove-Item bin\deno.zip"
  )
)

if not exist venv (
  py -3 -m venv venv 2>nul || python -m venv venv
)
call venv\Scripts\activate.bat
echo Checking Python packages - first run takes a few minutes...
pip install -q -r requirements.txt
pip install -q -U "yt-dlp[default]"

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>nul

start "" cmd /c "timeout /t 6 >nul & start http://localhost:8000"
echo Server running at http://localhost:8000  - close this window to stop
python -m uvicorn main:app --port 8000
pause
