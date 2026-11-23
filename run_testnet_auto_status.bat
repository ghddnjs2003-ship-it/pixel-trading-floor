@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  Pixel Trading Floor - TESTNET AUTO MODE - STATUS
echo  Shows the env picked by backend\.env (testnet or demo).
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo         Install Python 3.10 or newer and check "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

python -m backend.testnet_auto --status %*
echo.
pause
