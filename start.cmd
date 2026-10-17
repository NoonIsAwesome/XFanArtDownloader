@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "FANART_PROJECT_DIR=%~dp0"
pushd "%FANART_PROJECT_DIR%" || exit /b 1
if exist "%FANART_PROJECT_DIR%dist\XFanArtDownloader.exe" goto run_exe
if not exist "%FANART_PROJECT_DIR%.venv\Scripts\pythonw.exe" goto need_setup
"%FANART_PROJECT_DIR%.venv\Scripts\python.exe" -c "import PyQt6.QtWidgets, PIL, websocket, requests" >nul 2>&1
if errorlevel 1 goto need_setup
start "" /D "%FANART_PROJECT_DIR%" "%FANART_PROJECT_DIR%.venv\Scripts\pythonw.exe" "%FANART_PROJECT_DIR%desktop\main.py" %*
set "FANART_EXIT=%ERRORLEVEL%"
popd
exit /b %FANART_EXIT%

:run_exe
start "" /D "%FANART_PROJECT_DIR%" "%FANART_PROJECT_DIR%dist\XFanArtDownloader.exe" %*
set "FANART_EXIT=%ERRORLEVEL%"
popd
exit /b %FANART_EXIT%

:need_setup
echo The project Python environment is missing or incomplete.
echo Run setup.cmd in this folder, then run start.cmd again.
popd
pause
exit /b 1
