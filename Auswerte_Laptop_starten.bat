@echo off
rem RoadSense Auswerte-Laptop. Geheimnisse bleiben in %LOCALAPPDATA%\RoadSense\.env.
cd /d "%~dp0"
set "ROADSENSE_ROLE=local"
echo.
echo  RoadSense Auswerte-Laptop
echo  =========================
echo.
if not exist "app.py" (
  echo  FEHLER: app.py nicht gefunden.
  pause
  exit /b 1
)

rem --- Lokale Konfiguration ausserhalb des Git-Repositories -----------------
for /f "delims=" %%p in ('py lokale_einstellungen.py path') do set "ENVFILE=%%p"
for /f "delims=" %%t in ('py lokale_einstellungen.py token') do set "TOKEN=%%t"
if "%TOKEN%"=="" (
  echo  FEHLER: Lokaler Auswertungsschluessel konnte nicht erzeugt werden.
  pause
  exit /b 1
)

rem --- Tailscale pruefen -----------------------------------------------------
set "TS="
where tailscale >nul 2>nul && set "TS=tailscale"
if not defined TS if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not defined TS (
  echo  Tailscale ist noch nicht installiert. Installation ueber winget ...
  winget install --id tailscale.tailscale --accept-source-agreements --accept-package-agreements
  echo.
  echo  Fertig. Jetzt Tailscale anmelden und diese Datei danach neu starten.
  pause
  exit /b 0
)

rem --- Pakete und lokale App -------------------------------------------------
echo  Pakete pruefen ...
py -m pip install --quiet --disable-pip-version-check flask requests numpy opencv-python pillow
if errorlevel 1 (
  echo  FEHLER: Pakete konnten nicht installiert werden.
  pause
  exit /b 1
)
start "RoadSense App (offen lassen)" cmd /k "set ROADSENSE_ROLE=local&& py app.py"
timeout /t 4 /nobreak >nul

echo.
echo  ===================================================================
echo   Lokale RoadSense-Konfiguration:
echo     %ENVFILE%
echo.
echo   Lokaler Verbindungsschluessel - NICHT auf GitHub oder Render speichern:
echo     %TOKEN%
echo.
echo   Die Online-App speichert Adresse und Schluessel NUR in diesem Browser.
echo   Bei "Neue Strecke" einmal "Auswerte-Laptop verbinden" verwenden.
echo.
echo   Gleich erscheint die feste Tailscale-Adresse dieses Laptops.
echo   BEIDE Fenster offen lassen, solange ausgewertet werden soll.
echo  ===================================================================
echo.
"%TS%" funnel 8000
echo.
echo  Der Tunnel wurde beendet.
pause
