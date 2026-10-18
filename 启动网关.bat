@echo off
chcp 65001 >nul
setlocal EnableExtensions
title WB Gateway 127.0.0.1:8788

rem ============================================================
rem  WB Gateway launcher (this project's entry point)
rem
rem  IMPORTANT: keep this file ASCII-only. cmd.exe reads .bat using
rem  the console code page, so non-ASCII bytes can break the parser.
rem  Chinese text belongs in the browser panel, not here.
rem
rem  Do NOT use gateway\start-wb-proxy.bat. That is the upstream
rem  launcher: it starts the bare proxy and skips run_gateway.py,
rem  which is what injects the panel. You would get the old
rem  dashboard at / and no /wb-panel at all.
rem ============================================================

rem ---- find python --------------------------------------------------------
set "PY="
if exist "C:\Users\26619\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=C:\Users\26619\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.10+ and retry.
    pause
    exit /b 1
)

rem ---- always serve the CN realm -----------------------------------------
rem The upstream defaults to intl; multiplying CN credits by the intl table
rem gives wrong numbers. accounts\active_realm.json already says cn, this is
rem a second layer in case that file is lost.
set "WB_PROXY_DEFAULT_REALM=cn"

rem ---- keep the proxy off WB domains -------------------------------------
rem With an accelerator running, Python picks up the system proxy and sends
rem www.workbuddy.cn abroad. Loopback too, so Codex always talks directly.
set "NO_PROXY=127.0.0.1,localhost,.workbuddy.ai,.workbuddy.cn,workbuddy.ai,workbuddy.cn"
set "no_proxy=%NO_PROXY%"

cd /d "%~dp0"

echo ============================================================
echo   WB Gateway
echo   panel     : http://127.0.0.1:8788/            (auto-opens)
echo   upstream  : http://127.0.0.1:8788/upstream    (password: admin)
echo   stop      : close this window
echo ============================================================
echo.

rem ---- waiter: opens the browser only after the port is really listening
start "" /b "%PY%" open_panel.py --port 8788 --timeout 30

"%PY%" -u run_gateway.py --host 127.0.0.1 --port 8788

echo.
echo gateway exited.
pause
