@echo off
REM Guided setup: keys, streamers, sign-ins and scheduling. Safe to run again any time.
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m clipfarm wizard
pause
