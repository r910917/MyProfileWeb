@echo off
chcp 65001 >nul
title ⏹ Stop Nginx Server

echo 正在停止 Nginx...

:: 用 -s stop 方式 (需正確指定 prefix)
C:\Users\User\Downloads\nginx-1.28.0\nginx-1.28.0\nginx.exe -p C:\Users\User\Downloads\nginx-1.28.0\nginx-1.28.0\ -s stop

:: 保險：萬一上面沒成功，強制結束所有 nginx.exe
taskkill /F /IM nginx.exe >nul 2>&1

echo ✅ Nginx 已停止。
pause
