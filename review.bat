@echo off
REM Opens the review + performance dashboard in your browser (http://localhost:8765).
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m clipfarm stats
python -m clipfarm review
