@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  EXPERIMENTAL AI AUTONOMOUS MODE (Bybit Sandbox ONLY - NO REAL MONEY)
echo  AI reads 15m + 4h charts and decides LONG/SHORT/HOLD/CLOSE by itself.
echo  Account: Bybit Demo Trading (api-demo.bybit.com) - mock USDT only.
echo  50 USDT seed - 3%% risk per trade - leverage 3x - TP/SL attached.
echo  Max 4 entries per day - 10%% daily-loss kill switch - mainnet blocked.
echo  Checks at each 15m bar close +60s. A status line prints every minute.
echo  Ctrl+C stops the loop. Open positions keep their TP/SL at Bybit.
echo  Dry run without orders: run_demo_ai_experiment.bat --mock
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
  echo [INFO] Open backend\.env and fill BYBIT_API_KEY / BYBIT_API_SECRET (Demo Trading key),
  echo        then run this again. Without keys it runs in mock mode (no orders).
  echo.
  pause
  exit /b 1
)

echo [INFO] Starting the 15-minute AI loop (1-minute status). First scan runs now.
echo.
python -m backend.demo_ai_experiment --loop %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [ERROR] It stopped with code %CODE%. Read the messages above.
echo [INFO] Stopped. Your open position (if any) still has its TP/SL at Bybit.
pause
