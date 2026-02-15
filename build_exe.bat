@echo off
setlocal
cd /d "%~dp0"

echo [BUILD] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo [BUILD] PyInstaller not found. Installing...
  python -m pip install --upgrade pyinstaller
  if errorlevel 1 goto :fail
)

echo [BUILD] Building TFMR.exe...
python -m PyInstaller --noconfirm --clean --windowed --onedir --name TFMR tfmr_min_scanner_gui.py
if errorlevel 1 goto :fail

echo.
echo [BUILD] Done.
echo [BUILD] Output: %CD%\dist\TFMR\TFMR.exe
echo [BUILD] Deploy folder: %CD%\dist\TFMR
pause
endlocal
exit /b 0

:fail
echo.
echo [BUILD] Failed. Check the errors above.
pause
endlocal
exit /b 1
