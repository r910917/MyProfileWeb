title "網站"
@echo off
REM 移動到專案資料夾（請改成你自己的路徑）
cd /d C:\Users\User\venv\mysite

REM 加入所有檔案
git add .

REM 自動建立 commit 訊息（時間戳記）
set datetime=%date% %time%
git commit -m "Auto commit at %datetime%"

REM 推送到 GitHub master 分支
git push origin master

pause