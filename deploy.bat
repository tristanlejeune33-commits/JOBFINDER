@echo off
title JobFinder Deploy
cd /d "%~dp0"
git config user.email "tristanlejeune33@gmail.com"
git config user.name "Tristan"
echo.
git status --short
echo.
set /p MSG="Message (Entree = update) : "
if "%MSG%"=="" set MSG=update
git add .
git commit -m "%MSG%"
git pull origin main --rebase --allow-unrelated-histories
git push origin main
if errorlevel 1 goto error
echo.
echo Deploye !
timeout /t 3 >nul
goto end
:error
echo ERREUR : voir message ci-dessus
pause
:end
