@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_r2_publisher.ps1"
set "publisher_exit=%errorlevel%"
echo.
pause
exit /b %publisher_exit%
