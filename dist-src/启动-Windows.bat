@echo off
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title Sandbox

set "PY="

where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto run

where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto run

where python3 >nul 2>nul
if not errorlevel 1 set "PY=python3"
if defined PY goto run

goto nopython

:run
%PY% start.py
if errorlevel 1 goto failed
goto done

:nopython
echo.
echo   Python 3 was not found on this computer.
echo.
echo   1. Open https://www.python.org/downloads/
echo   2. Download and run the Windows installer
echo   3. IMPORTANT: tick "Add python.exe to PATH" on the first screen
echo   4. Double-click this file again
echo.
echo   (Takes about 3 minutes, one time only.)
echo.
pause
exit /b 1

:failed
echo.
echo   Startup failed. The message above says why.
echo.
echo   Common causes:
echo     - Python installed without "Add to PATH"  - reinstall and tick it
echo     - The window opened Microsoft Store       - that is a Python stub,
echo       please install the real Python from python.org
echo     - Folder path contains unusual characters - try moving this folder
echo       to something simple like D:\sandbox
echo.
pause
exit /b 1

:done
pause
