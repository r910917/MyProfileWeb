@echo off
setlocal enabledelayedexpansion

:: 設定 GitHub 儲存庫 URL
set REPO_URL=r910917/MyProfileWeb

:: 取得當前日期和時間 (格式：YYYY-MM-DD_HH-MM-SS)
for /f "tokens=1-4 delims=-: " %%a in ("%date% %time%") do (
    set YEAR=%%a
    set MONTH=%%b
    set DAY=%%c
    set HOUR=%%d
    set MINUTE=%%e
    set SECOND=%%f
)

:: 格式化時間，避免不合法字元 (冒號 : 改為破折號 -)
set BRANCH_NAME=%YEAR%-%MONTH%-%DAY%_%HOUR%-%MINUTE%-%SECOND%

:: 顯示即將創建的分支名稱
echo Creating new branch: %BRANCH_NAME%

:: 切換到儲存庫目錄（此行視你的本地目錄結構調整）
cd /d C:\Users\User\venv\mysite

:: 確認是否在儲存庫根目錄，且是 git 倉庫
git rev-parse --is-inside-work-tree > nul 2>&1
if %errorlevel% neq 0 (
    echo Not a git repository. Please navigate to the correct directory.
    exit /b 1
)

:: 拉取最新的主分支
git checkout main
git pull origin main

:: 創建並切換到新分支
git checkout -b %BRANCH_NAME%

:: 推送新分支到 GitHub
git push origin %BRANCH_NAME%

:: 提示創建完畢
echo New branch %BRANCH_NAME% created and pushed to GitHub.

endlocal
