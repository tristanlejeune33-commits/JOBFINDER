@echo off
title JobFinder
echo.
echo  ==========================================
echo    JobFinder - Assistant IA pour l'emploi
echo  ==========================================
echo.

set PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe

if not exist "%PYTHON%" (
    echo  [ERREUR] Python 3.12 introuvable.
    echo  Chemin attendu : %PYTHON%
    pause
    exit /b 1
)

echo  Python : %PYTHON%
echo.

echo  [1/3] Installation des dependances...
"%PYTHON%" -m pip install "flask>=3.0.0" "anthropic>=0.25.0" "openai>=1.66.0" "requests>=2.31.0" "beautifulsoup4>=4.12.0" "pypdf>=4.0.0" "cloudscraper>=1.2.71" "playwright>=1.40.0" --quiet --disable-pip-version-check
echo  OK.
echo.

echo  [2/3] Installation de Chromium pour la generation PDF (premiere fois uniquement)...
"%PYTHON%" -m playwright install chromium --with-deps >nul 2>&1
echo  OK.
echo.

echo  [3/3] Lancement de JobFinder dans le navigateur...
echo.
echo  -> http://localhost:5151
echo.
"%PYTHON%" "%~dp0jobfinder.py"

if errorlevel 1 (
    echo.
    echo  [ERREUR] JobFinder s'est ferme avec une erreur.
    pause
)
