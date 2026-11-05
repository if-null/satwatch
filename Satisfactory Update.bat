@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
if exist "C:\Python314\python.exe" set "PYEXE=C:\Python314\python.exe"
if not defined PYEXE (py -3 --version >nul 2>&1 && set "PYEXE=py -3")
if not defined PYEXE (python --version >nul 2>&1 && set "PYEXE=python")
if not defined PYEXE (
  echo ERROR: Python not found. Install Python 3 to C:\Python314 or add it to PATH.
  pause
  exit /b 1
)
title Satisfactory Update
%PYEXE% "%~dp0satwatch.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo satwatch exited with error code %RC%.
  pause
)
exit /b %RC%
