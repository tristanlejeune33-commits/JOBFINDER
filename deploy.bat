@echo off
title JobFinder — Deploy
chcp 65001 >nul

cd /d "%~dp0"

:: ── Identite Git (necessaire au premier commit) ───────────────────────────
git config user.email "tristanlejeune33@gmail.com"
git config user.name "Tristan"

echo.
echo  Fichiers modifies :
git status --short
echo.

:: ── Message de commit ────────────────────────────────────────────────────
set /p MSG="  Message de commit (Entree = 'update') : "
if "%MSG%"=="" set MSG=update

:: ── Push ─────────────────────────────────────────────────────────────────
git add .
git commit -m "%MSG%"
git push origin main

if errorlevel 1 (
    echo.
    echo  [ERREUR] Push echoue. Verifier les droits GitHub.
    pause
    exit /b 1
)

echo.
echo  Deploye ! Le site se met a jour automatiquement.
echo.
timeout /t 3 >nul
