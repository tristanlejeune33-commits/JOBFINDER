@echo off
title JobFinder — Build executable
chcp 65001 >nul
echo.
echo  ============================================
echo    JobFinder — Compilation executable Windows
echo  ============================================
echo.

:: ── Python ────────────────────────────────────────────────────────────────
set PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe

if not exist "%PYTHON%" (
    echo  [ERREUR] Python 3.12 introuvable.
    echo  Chemin attendu : %PYTHON%
    echo  Installe Python 3.12 depuis https://python.org
    pause
    exit /b 1
)

echo  Python : %PYTHON%
echo.

:: ── Dépendances ───────────────────────────────────────────────────────────
echo  [1/3] Installation des dependances...
"%PYTHON%" -m pip install ^
    "flask>=3.0.0" ^
    "anthropic>=0.25.0" ^
    "openai>=1.66.0" ^
    "requests>=2.31.0" ^
    "beautifulsoup4>=4.12.0" ^
    "pypdf>=4.0.0" ^
    "cloudscraper>=1.2.71" ^
    "pyinstaller>=6.0.0" ^
    --quiet --disable-pip-version-check
echo  OK.
echo.

:: ── Nettoyage des anciens builds ──────────────────────────────────────────
echo  [2/3] Nettoyage des anciens builds...
if exist "dist\JobFinder.exe" del /f /q "dist\JobFinder.exe"
if exist "build"              rmdir /s /q "build"
echo  OK.
echo.

:: ── Compilation ───────────────────────────────────────────────────────────
echo  [3/3] Compilation avec PyInstaller...
echo  (peut prendre 1-3 minutes selon la machine)
echo.
"%PYTHON%" -m PyInstaller jobfinder.spec --noconfirm

if errorlevel 1 (
    echo.
    echo  [ERREUR] La compilation a echoue. Voir les messages ci-dessus.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo    Build termine avec succes !
echo    Executable : dist\JobFinder.exe
echo  ============================================
echo.
echo  Pour tester : double-cliquer sur dist\JobFinder.exe
echo  Pour GitHub : zipper dist\JobFinder.exe et le publier en Release
echo.
pause
