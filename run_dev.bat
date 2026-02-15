@echo off
setlocal
cd /d "%~dp0"

echo [DEV] Running tfmr_min_scanner_gui.py...
python tfmr_min_scanner_gui.py

if errorlevel 1 (
  echo.
  echo [DEV] Run failed.
  pause
)

endlocal
