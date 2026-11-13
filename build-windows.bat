@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required to build the Windows app. Install Python 3.12 from python.org and enable the Python launcher.
  pause
  exit /b 1
)

if not exist ".venv-win\Scripts\python.exe" py -3.12 -m venv .venv-win
if errorlevel 1 goto :failed
.venv-win\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :failed
.venv-win\Scripts\python.exe -m pip install -r requirements-windows.txt
if errorlevel 1 goto :failed
.venv-win\Scripts\pyinstaller.exe --noconfirm --clean --onefile --console --name EFileMMD --distpath "Windows-EXE" --workpath "build\windows-exe" --add-data "index.html;." --add-data "app.js;." --add-data "styles.css;." --add-data "assets;assets" --collect-all keyring --collect-all psycopg --collect-all psycopg_binary --hidden-import keyring.backends.Windows windows_launcher.py
if errorlevel 1 goto :failed
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'Windows-EXE\EFileMMD-Windows.zip') { Remove-Item 'Windows-EXE\EFileMMD-Windows.zip' -Force }; Compress-Archive -Path 'Windows-EXE\EFileMMD.exe' -DestinationPath 'Windows-EXE\EFileMMD-Windows.zip' -Force"
if errorlevel 1 goto :failed
echo.
echo Windows app built successfully.
echo Run Windows-EXE\EFileMMD.exe on the host computer and share the LAN address shown in its console.
echo Share Windows-EXE\EFileMMD-Windows.zip with users only if they need a local copy; normally they use the host's browser address.
echo.
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error above, then run build-windows.bat again.
pause
exit /b 1
