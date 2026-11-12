@echo off
REM Records who's streaming GTA right now. Run it a few times a day (or schedule it) - the longer
REM it runs, the better its picks. Results: review.bat -> Discover tab.
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m clipfarm discover
pause
