@echo off
setlocal
rem Push the Kit repository (branch main only, no tags) to GitHub.
rem First run opens a GitHub sign-in window from Git Credential Manager: click Authorize.
cd /d "%~dp0.."
set "REPO=https://github.com/Mrchks/dafeiyu-kit.git"
set "LOG=%~dp0..\logs\push-github.txt"
if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"

set "GIT=git"
where git >nul 2>nul
if errorlevel 1 set "GIT=%ProgramFiles%\Git\cmd\git.exe"
set "G="%GIT%" -c safe.directory=* -c credential.helper= -c credential.helper=manager"

echo [%date% %time%] start > "%LOG%"
echo Step 1/3: testing connection to GitHub ...
%G% ls-remote "%REPO%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo FAILED: cannot reach GitHub, or the repository does not exist yet.
  echo Turn on your proxy / accelerator and run this file again.
  echo connect-failed >> "%LOG%"
  pause
  exit /b 1
)

echo Step 2/3: setting remote "origin" ...
%G% remote get-url origin >nul 2>nul
if errorlevel 1 (
  %G% remote add origin "%REPO%" >> "%LOG%" 2>&1
) else (
  %G% remote set-url origin "%REPO%" >> "%LOG%" 2>&1
)

echo Step 3/3: pushing branch main. A GitHub window may open - click Authorize.
%G% push -u origin main >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo FAILED: push did not complete. Details are in logs\push-github.txt
  echo push-failed >> "%LOG%"
  pause
  exit /b 1
)
echo push-ok >> "%LOG%"
echo.
echo DONE: pushed to %REPO%
pause
