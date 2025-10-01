@echo off
chcp 65001 >nul
title 🚀 Django + Nginx Server

:: ===== 啟動 Uvicorn =====
echo 啟動 Uvicorn 中...
cd /d C:\Users\User\venv\mysite
call C:\Users\User\venv\Scripts\activate.bat
start cmd /k "C:\Users\User\venv\Scripts\uvicorn.exe mysite.asgi:application --host 127.0.0.1 --port 7000 --proxy-headers"

:: ===== 驗證並啟動 Nginx =====
echo.
echo 檢查 Nginx 設定檔...
cd /d C:\Users\User\Downloads\nginx-1.28.0\nginx-1.28.0
nginx.exe -p C:\Users\User\Downloads\nginx-1.28.0\nginx-1.28.0\ -t

if %ERRORLEVEL% NEQ 0 (
    echo ⚠️ Nginx 配置檔有錯誤，請修正後再試。
    pause
    exit /b
)

echo 啟動 Nginx 中...
nginx.exe -p C:\Users\User\Downloads\nginx-1.28.0\nginx-1.28.0\ -t

echo.
echo ===============================
echo ✅ 伺服器已啟動：
echo - Django (Uvicorn): http://127.0.0.1:8000/find/
echo - Nginx Proxy:      http://127.0.0.1/
echo ===============================

pause
