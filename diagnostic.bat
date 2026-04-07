@echo off
echo ==========================================
echo   DIAGNOSTIC JobFinder
echo ==========================================
echo.

echo --- Test python ---
python --version
echo Errorlevel: %errorlevel%
echo.

echo --- Test py ---
py --version
echo Errorlevel: %errorlevel%
echo.

echo --- Recherche dans AppData ---
dir "%LOCALAPPDATA%\Programs\Python" 2>nul || echo Aucun dossier Python dans AppData
echo.

echo --- Recherche dans C:\ ---
dir "C:\Python*" /b 2>nul || echo Aucun dossier Python dans C:\
echo.

echo --- Variable PATH ---
echo %PATH%
echo.

echo --- Fin du diagnostic ---
pause
