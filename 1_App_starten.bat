@echo off
rem RoadSense lokal starten. Geheimnisse liegen ausserhalb des Git-Repositories.
cd /d "%~dp0"
set "ROADSENSE_ROLE=local"
echo.
echo  RoadSense wird lokal gestartet ...
echo.
if not exist "app.py" (
  echo  FEHLER: app.py nicht gefunden.
  pause
  exit /b 1
)
for /f "delims=" %%p in ('py lokale_einstellungen.py path') do set "ENVFILE=%%p"
echo  Lokale Einstellungen: %ENVFILE%
py -m pip install --quiet --disable-pip-version-check flask requests numpy opencv-python pillow
if errorlevel 1 (
  echo  FEHLER: Pakete konnten nicht installiert werden.
  pause
  exit /b 1
)
echo.
echo  RoadSense laeuft unter http://localhost:8000
echo.
py app.py
pause
