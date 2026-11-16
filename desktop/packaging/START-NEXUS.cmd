@echo off
REM ===================================================================================
REM  NEXUS - start the desktop client
REM ===================================================================================
REM  Double-click this file to open NEXUS.
REM
REM  It only starts the CLIENT. The backend server must already be running in another
REM  window (see README-FA.md, step 1). If the window opens but stays empty, the backend
REM  is not reachable.
REM
REM  PUT YOUR KEY BELOW, once. This file is shipped with a placeholder on purpose:
REM  an API key must never be committed to the repository. Generate one with:
REM      python -c "import secrets; print(secrets.token_urlsafe(48))"
REM ===================================================================================

REM ---- your settings ----------------------------------------------------------------
set "NEXUS_DESKTOP_API_URL=http://127.0.0.1:8000"
set "NEXUS_DESKTOP_API_KEY=PASTE-YOUR-API-KEY-HERE"
REM -----------------------------------------------------------------------------------

set "APP_DIR=%~dp0"
set "NEXUS_EXE=%APP_DIR%NEXUS.exe"

if not exist "%NEXUS_EXE%" (
    echo.
    echo   ERROR: NEXUS.exe was not found next to this file.
    echo   Expected: %NEXUS_EXE%
    echo.
    pause
    exit /b 1
)

if "%NEXUS_DESKTOP_API_KEY%"=="PASTE-YOUR-API-KEY-HERE" (
    echo.
    echo   NOTE: the API key in this file is still the placeholder.
    echo         Open START-NEXUS.cmd in Notepad and set NEXUS_DESKTOP_API_KEY,
    echo         otherwise the client cannot talk to the backend.
    echo.
    pause
)

echo.
echo   NEXUS
echo   server : %NEXUS_DESKTOP_API_URL%
echo.
echo   Starting the client...
echo   If the window is empty, start the backend first - see README-FA.md.
echo.

start "" "%NEXUS_EXE%"
exit /b 0