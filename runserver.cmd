@echo off
chcp 65001 >nul
title 🚀 MySite Server

:: 切換到專案目錄
cd /d C:\Users\User\venv\mysite

:: 啟動虛擬環境
call C:\Users\User\venv\Scripts\activate.bat

:: 啟動 uvicorn
uvicorn mysite.asgi:application --host 192.168.0.157 --port 8000 --reload

pause