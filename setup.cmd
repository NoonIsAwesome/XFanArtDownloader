@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "FANART_PROJECT_DIR=%~dp0"
set "FANART_PYTHON=%~dp0.venv\Scripts\python.exe"
pushd "%FANART_PROJECT_DIR%" || exit /b 1
if exist "%FANART_PYTHON%" goto install
py -3.12 -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_launcher
python -c "import struct, sys; sys.exit(0 if sys.version_info >= (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_python
echo Install 64-bit Python 3.12 or newer from https://www.python.org/downloads/windows/
echo Then run setup.cmd again.
goto failed

:create_with_launcher
py -3.12 -m venv "%FANART_PROJECT_DIR%.venv"
if errorlevel 1 goto failed
goto install

:create_with_python
python -m venv "%FANART_PROJECT_DIR%.venv"
if errorlevel 1 goto failed

:install
"%FANART_PYTHON%" -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -r "%FANART_PROJECT_DIR%requirements.txt" -r "%FANART_PROJECT_DIR%requirements-gallery.txt"
if errorlevel 1 goto failed
"%FANART_PYTHON%" -m pip check
if errorlevel 1 goto failed
"%FANART_PYTHON%" -c "import PyQt6.QtWidgets, PIL, websocket, requests"
if errorlevel 1 goto failed
"%FANART_PYTHON%" "%FANART_PROJECT_DIR%extractor\gallery_bridge.py" --smoke-test >nul
if errorlevel 1 goto failed
echo Setup complete. Run start.cmd to open X FanArt Downloader.
popd
pause
exit /b 0

:failed
echo Setup did not complete. Keep the error message for troubleshooting.
echo Do not bypass a TLS or package hash verification failure; retry the download.
popd
pause
exit /b 1
