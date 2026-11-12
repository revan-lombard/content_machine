@echo off
REM Opens a command window in this folder, ready for "python -m clipfarm ..." commands.
cd /d "%~dp0"
cmd /k "call .venv\Scripts\activate.bat && echo Ready. Try: python -m clipfarm -h"
