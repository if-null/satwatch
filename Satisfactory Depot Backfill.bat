@echo off
setlocal
cd /d "%~dp0"
rem ---- Optional settings (power users). Leave empty: save folder and session are auto-detected. ----
rem Example: set SATWATCH_ARGS=--saves "D:\MySaves" --pattern "MySave_*.sav"
set SATWATCH_ARGS=
rem Optional: full path to python.exe (leave empty to auto-detect; Python is installed with winget if missing)
set "PYEXE="
call :findpy
if not defined PYEXE (
  echo Python 3 was not found. Trying to install it with winget ^(per-user, no admin^)...
  winget --version >nul 2>&1 && winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :findpy
)
if not defined PYEXE (
  echo.
  echo Python 3 could not be installed automatically.
  echo Please install it from https://www.python.org/downloads/ and run this file again.
  pause
  exit /b 1
)
title Satisfactory Depot Backfill
echo Re-reading older saves for depot + network data (about 1 s per save)...
%PYEXE% "%~dp0satwatch.py" %SATWATCH_ARGS% --backfill-depot %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo satwatch exited with error code %RC%.
  pause
)
exit /b %RC%

:findpy
if defined PYEXE goto :eof
if exist "C:\Python314\python.exe" set "PYEXE=C:\Python314\python.exe"& goto :eof
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" set PYEXE="%%~fD\python.exe"
if defined PYEXE goto :eof
py -3 --version >nul 2>&1 && set "PYEXE=py -3"
if defined PYEXE goto :eof
python --version >nul 2>&1 && set "PYEXE=python"
goto :eof
