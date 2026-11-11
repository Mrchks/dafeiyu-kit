@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=%~dp0..\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo  Dafeiyu release
echo    1 = formal release  (commit, tag, push, upload to GitHub)
echo    2 = pre-release     (same, but installed users will NOT get it; for testers)
echo    3 = build only      (no commit, no tag, no upload; for self-test)
echo.
set "MODE="
set /p MODE=Choose 1/2/3: 
set "VER="
set /p VER=Version, e.g. 1.5.1: 
set "ARGS=%VER%"
if "%MODE%"=="2" set "ARGS=%VER% --pre"
if "%MODE%"=="3" set "ARGS=%VER% --dry"
echo.
"%PY%" -X utf8 "%~dp0release.py" %ARGS%
echo.
pause
