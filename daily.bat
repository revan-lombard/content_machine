@echo off
REM Usage: daily.bat                 (uses the newest session in sessions\)
REM        daily.bat path\to\file.beats.json
REM        daily.bat path\to\recording.mp4     (no tags: auto-detected moments only)
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" for /f "delims=" %%f in ('dir /b /o-d sessions\*.beats.json 2^>nul') do if not defined TARGET set "TARGET=sessions\%%f"
if "%TARGET%"=="" (echo No session found. Run log.bat first, or pass a video file. & pause & exit /b)
python -m clipfarm daily "%TARGET%"
pause
