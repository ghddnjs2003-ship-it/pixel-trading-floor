@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  Pixel Trading Floor - TESTNET/DEMO AUTO MODE (Bybit sandbox only)
echo  50 USDT seed - 3%% loss per trade - leverage 3x - TP/SL attached
echo  Sandbox only: api-testnet.bybit.com or api-demo.bybit.com - no real money.
echo  Demo Trading: put BYBIT_DEMO=true in backend\.env (see TESTNET_AUTO.md).
echo  A live status line prints every minute.
echo  Adaptive watch: 5-min checks while a position is open, volatility spikes,
echo                  or price is within 0.35%% of the swing high/low (BSL/SSL).
echo  Position watch: price every 5 seconds while a position is open.
echo    +1.5R -> sell 50%% as reduce-only and move the stop to break-even (+0.1%%).
echo    Small size below the exchange minimum -> skip the partial, break-even only.
echo    The rest runs to the 4R target or the 96-bar time exit.
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

echo [INFO] Starting the 30-minute bar loop (5s position watch, 1-minute status). First scan runs now.
echo.
python -m backend.testnet_auto --loop --adaptive %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [ERROR] It stopped with code %CODE%. Read the messages above.
echo [INFO] Stopped. Your open position (if any) still has its TP/SL at Bybit.
pause
