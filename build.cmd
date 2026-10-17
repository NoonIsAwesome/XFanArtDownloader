@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "FANART_PROJECT_DIR=%~dp0"
set "FANART_PYTHON=%~dp0.venv\Scripts\python.exe"
pushd "%FANART_PROJECT_DIR%" || exit /b 1
if not exist "%FANART_PYTHON%" goto need_setup
"%FANART_PYTHON%" -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -r "%FANART_PROJECT_DIR%requirements-build.txt"
if errorlevel 1 goto failed
"%FANART_PYTHON%" -m pip check
if errorlevel 1 goto failed
rem Keep other developer tools' native DLLs out of the frozen application.
set "PATH=%FANART_PROJECT_DIR%.venv\Scripts;%SystemRoot%\System32;%SystemRoot%"
"%FANART_PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%FANART_PROJECT_DIR%dist" --workpath "%FANART_PROJECT_DIR%build" "%FANART_PROJECT_DIR%packaging\gallery-dl-helper.spec"
if errorlevel 1 goto failed
"%FANART_PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%FANART_PROJECT_DIR%dist" --workpath "%FANART_PROJECT_DIR%build" "%FANART_PROJECT_DIR%packaging\XFanArtDownloader.spec"
if errorlevel 1 goto failed
copy /Y "%FANART_PROJECT_DIR%README.md" "%FANART_PROJECT_DIR%dist\README.md" >nul
if errorlevel 1 goto failed
copy /Y "%FANART_PROJECT_DIR%THIRD_PARTY_NOTICES.txt" "%FANART_PROJECT_DIR%dist\THIRD_PARTY_NOTICES.txt" >nul
if errorlevel 1 goto failed
copy /Y "%FANART_PROJECT_DIR%LICENSE" "%FANART_PROJECT_DIR%dist\LICENSE" >nul
if errorlevel 1 goto failed
if not exist "%FANART_PROJECT_DIR%dist\licenses" mkdir "%FANART_PROJECT_DIR%dist\licenses"
copy /Y "%FANART_PROJECT_DIR%licenses\*.txt" "%FANART_PROJECT_DIR%dist\licenses\" >nul
if errorlevel 1 goto failed
"%FANART_PYTHON%" -c "import importlib.metadata as m, os, pathlib, shutil, sys; root=pathlib.Path(os.environ['FANART_PROJECT_DIR'])/'dist'/'licenses'; names=('gallery-dl','PyQt6','PyQt6-Qt6','PyQt6-sip','Pillow','websocket-client','requests','certifi','charset-normalizer','idna','urllib3','pyinstaller'); entries=[(d.metadata['Name'],d.locate_file(f)) for n in names for d in [m.distribution(n)] for f in (d.files or []) if '.dist-info/' in str(f).replace(chr(92),'/') and any(p.lower().startswith(('license','copying','notice','copyright')) for p in pathlib.PurePosixPath(str(f).replace(chr(92),'/')).parts) and d.locate_file(f).is_file()]; [((root/name).mkdir(parents=True,exist_ok=True),shutil.copy2(source,root/name/source.name)) for name,source in entries]; (root/'Python').mkdir(parents=True,exist_ok=True); shutil.copy2(pathlib.Path(sys.base_prefix)/'LICENSE.txt',root/'Python'/'LICENSE.txt')"
if errorlevel 1 goto failed
echo Build complete: dist\XFanArtDownloader.exe
echo The application EXE includes the separately executed gallery-dl helper.
echo README, third-party notices and original license texts are beside the EXE.
popd
pause
exit /b 0

:need_setup
echo Run setup.cmd first to create this project's .venv.
goto failed

:failed
echo Build did not complete. Keep the error message for troubleshooting.
popd
pause
exit /b 1
