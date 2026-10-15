@echo off
setlocal enableextensions
cd /d "%~dp0"

echo ============================================================
echo  AAFL 24-Month MRP - build + publish
echo ============================================================

REM --- 1. Build the live dashboard -> index.html -----------------------------
echo.
echo [1/4] Building dashboard from live DWH (gateway)...
python build_dashboard.py
if errorlevel 1 (
    echo.
    echo  ERROR: build failed. Check gateway_credentials.json / network.
    pause
    exit /b 1
)

REM --- 2. Publish index.html (renamed/overwritten automatically) -------------
echo.
echo [2/4] Publishing index.html...
if not exist ".git" (
    git init -b main
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    git remote add origin https://github.com/TamannaSultana-Akijresource/AAFL_24-month-MRP.git
)

git add -A
git commit -m "Auto-refresh AAFL MRP dashboard %date% %time%" >nul 2>&1
if errorlevel 1 (
    echo      nothing new to commit.
) else (
    echo      committed.
)

REM --- 3. Push to GitHub -----------------------------------------------------
echo.
echo [3/4] Pushing to GitHub...
git push -u origin main
if errorlevel 1 (
    echo.
    echo  WARNING: git push failed - check credentials / remote.
) else (
    echo      pushed.
)

REM --- 4. Open the dashboard -------------------------------------------------
echo.
echo [4/4] Opening dashboard in browser...
start "" "%~dp0index.html"

echo.
echo Done.
pause
