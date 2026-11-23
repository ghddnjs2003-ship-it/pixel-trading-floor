@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  Pixel Trading Floor - TESTNET AUTO MODE (Bybit testnet only)
echo  50 USDT seed - 3%% loss per trade - leverage 3x - TP/SL attached
echo  Ctrl+C stops the loop. Open positions keep their TP/SL.
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

if not exist "backend\.env" (
  echo [INFO] backend\.env is missing - copying backend\.env.example.
  copy "backend\.env.example" "backend\.env" >nul
  echo [INFO] Open backend\.env and fill BYBIT_API_KEY / BYBIT_API_SECRET, then run this again.
  echo.
  pause
  exit /b 1
)

echo [INFO] Starting the 30-minute bar loop. First scan runs now.
echo.
python -m backend.testnet_auto --loop %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [ERROR] It stopped with code %CODE%. Read the messages above.
echo [INFO] Stopped. Your open position (if any) still has its TP/SL at Bybit.
pause
