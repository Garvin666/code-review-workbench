@echo off
REM ==================================================
REM Guardian Workbench one-click launcher (ASCII-safe)
REM ==================================================
REM Uses the system python on PATH. Add it to PATH or
REM run with: py -3 guardian_server.py ...
REM Defaults: --demo (with internal demo site on :8800)

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [guardian] python not found on PATH.
  echo            Example using managed Python:
  echo            C:\Users\26717\.workbuddy\binaries\python\versions\3.13.12\python.exe guardian_server.py --demo
  pause
  exit /b 1
)

REM Auto open browser after 2s in background
start "" /min cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8700/"

python guardian_server.py --demo
endlocal