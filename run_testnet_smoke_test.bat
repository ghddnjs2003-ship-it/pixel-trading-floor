@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  Pixel Trading Floor - SANDBOX SMOKE TEST (one order, closed)
echo  Places ONE small market order on the Bybit SANDBOX account
echo  (Demo Trading api-demo.bybit.com or Testnet api-testnet.bybit.com),
echo  waits 3 seconds, shows the position, then closes it at once.
echo  NO REAL MONEY: the sandbox account holds practice USDT only.
echo  Paper trading and auto-mode records are NOT touched.
echo  Stop the auto-mode loop (Ctrl+C in its window) first.
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

echo [INFO] Sending one sandbox market order now. It is closed 3 seconds later.
echo.
python -m backend.testnet_auto --smoke-test %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [ERROR] The smoke test did not finish cleanly (code %CODE%). Read the messages above.
echo [INFO] Done. No real money was used.
pause
