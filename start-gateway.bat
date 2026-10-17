@echo off
chcp 65001 >nul
setlocal
title WB Gateway 127.0.0.1:8788

rem ---- 找 Python ----------------------------------------------------------
set "PY="
if exist "C:\Users\26619\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=C:\Users\26619\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

rem ---- 固定用国内版 -----------------------------------------------------
rem 网关默认 intl（国际版），用国际版内置旧表算倍率会出错。
rem accountsctive_realm.json 已记着 cn，这里再兜一层，文件丢了也不会跑偏。
set "WB_PROXY_DEFAULT_REALM=cn"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [错误] 找不到 Python。
    pause
    exit /b 1
)

rem ---- 关键：把 WB 域名从系统代理里排掉 --------------------------------
rem 加速器开着时，Python 会自动读系统代理（127.0.0.1:7890）。
rem www.workbuddy.ai / www.workbuddy.cn 不在系统代理白名单里，
rem 请求会绕到境外节点 —— 国内服务从境外 IP 访问有风控风险。
rem 本机回环地址也一并排掉，保证 Codex 连网关永远走直连。
set "NO_PROXY=127.0.0.1,localhost,.workbuddy.ai,.workbuddy.cn,workbuddy.ai,workbuddy.cn"
set "no_proxy=%NO_PROXY%"

cd /d "%~dp0"
echo ============================================================
echo  网关地址 : http://127.0.0.1:8788
echo  看板     : http://127.0.0.1:8788/
echo  停止     : 直接关掉这个窗口
echo ============================================================
echo.
"%PY%" -u run_gateway.py --host 127.0.0.1 --port 8788
echo.
echo 网关已退出。
pause
