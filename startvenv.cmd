@echo off
REM 移動到專案目錄 (這裡假設你在 C:\Users\User\venv\mysite)
cd /d C:\Users\User\venv\mysite

REM 啟動虛擬環境
run ..\Scripts\activate.bat

REM 確認 Python 與 pip 正確版本
python --version
pip --version
