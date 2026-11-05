@echo off
setlocal
cd /d "%~dp0"
rem ---- Your settings: leave empty to use the defaults (see README, "Configure") ----
rem Example: set SATWATCH_ARGS=--saves "%LOCALAPPDATA%\FactoryGame\Saved\SaveGames\12345678901234567" --pattern "MySave*.sav"
set SATWATCH_ARGS=
rem Optional: full path to python.exe if it is not in C:\Python314 or on PATH
set "PYEXE="
if not defined PYEXE if exist "C:\Python314\python.exe" set "PYEXE=C:\Python314\python.exe"
if not defined PYEXE (py -3 --version >nul 2>&1 && set "PYEXE=py -3")
if not defined PYEXE (python --version >nul 2>&1 && set "PYEXE=python")
if not defined PYEXE (
  echo ERROR: Python not found. Install Python 3 to C:\Python314 or add it to PATH.
  pause
  exit /b 1
)
title Satisfactory Map Backfill
echo Re-reading older saves for the map (about 1 s per save)...
%PYEXE% "%~dp0satwatch.py" %SATWATCH_ARGS% --backfill-maps %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo satwatch exited with error code %RC%.
  pause
)
exit /b %RC%
