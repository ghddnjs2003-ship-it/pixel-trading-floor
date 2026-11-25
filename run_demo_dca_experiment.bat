@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  EXPERIMENTAL RICH-STYLE 1-4 DCA MODE (Bybit Demo ONLY - NO REAL MONEY)
echo  Rules from the partner config (ctl/g/config.js @ b91184e, dcaRules).
echo  Account: Bybit Demo Trading (api-demo.bybit.com) - mock USDT only.
echo  1st entry: 13%% of available margin, leverage 50x (adjustable 1-50x).
echo  Take profit: margin +10%% from average price (price +0.20%% at 50x).
echo  DCA 2nd/3rd/4th: margin -25%%/-40%%/-60%% with RSI14 / BB(20, 2.5s)
echo                   force line -40%%/-60%%/-80%%, add 15%%/33%%/100%%
echo  Auto stop: after the 4th add, STOP at 90%% of avg-to-liquidation distance.
echo  Entry direction: Gemini (5m + 4h), technical filter if AI is unavailable.
echo  Checks every 30 seconds. A status line prints every minute.
echo  Ctrl+C stops the loop. An open position keeps its TP/SL at Bybit.
echo.
echo  Dry run without orders:  run_demo_dca_experiment.bat --mock
echo  Demo balance needed: ~50 USDT. Symbol: ETHUSDT (default) / SOLUSDT / BTCUSDT.
echo  ** Do NOT run run_testnet_auto.bat or run_demo_ai_experiment.bat at the
echo     same time - one demo account, one engine. **
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

echo [INFO] Starting the 30-second DCA loop (1-minute status). First scan runs now.
echo.
python -m backend.demo_dca_experiment --loop %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [ERROR] It stopped with code %CODE%. Read the messages above.
echo [INFO] Stopped. Your open position (if any) still has its TP/SL at Bybit.
pause
