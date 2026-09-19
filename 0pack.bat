@echo off
rem ============================================================
rem  0pack.bat - Double-click launcher (Windows)
rem  The .ps1 association on this machine is missing / taken over
rem  by a text editor, so double-clicking 0pack.ps1 never reaches
rem  the packaging flow. This wrapper invokes 0pack.ps1 explicitly
rem  with -ExecutionPolicy Bypass, forwards arguments, and keeps
rem  the console open when the build fails.
rem ============================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp00pack.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [0pack.bat] BUILD FAILED (exit code %RC%^). Window kept open for review.
  pause
)
exit /b %RC%