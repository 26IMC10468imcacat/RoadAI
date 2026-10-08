@echo off
rem RoadSense: Diesen Laptop als Auswerte-Rechner fuer die Webseite (Render) starten.
rem Startet die App und macht sie ueber Tailscale Funnel unter einer festen https-Adresse erreichbar.
cd /d "%~dp0"
echo.
echo  RoadSense Auswerte-Laptop
echo  =========================
echo.
if not exist "app.py" (
  echo  FEHLER: app.py nicht gefunden. Bitte die ZIP zuerst mit "Alle extrahieren" entpacken.
  pause
  exit /b 1
)

rem --- Schluessel: einmalig erzeugen und in .env speichern ---
if not exist ".env" copy ".env.example" ".env" >nul
findstr /b /c:"AUSWERTUNG_TOKEN=" ".env" >nul 2>nul
if errorlevel 1 (
  echo.>> ".env"
  for /f %%t in ('py -c "import secrets;print(secrets.token_urlsafe(24))"') do echo AUSWERTUNG_TOKEN=%%t>> ".env"
)
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"AUSWERTUNG_TOKEN=" ".env"') do set "TOKEN=%%b"
if "%TOKEN%"=="" (
  echo  FEHLER: In der Datei .env steht "AUSWERTUNG_TOKEN=" ohne Wert. Bitte die Zeile loeschen und neu starten.
  pause
  exit /b 1
)

rem --- Tailscale pruefen ---
set "TS="
where tailscale >nul 2>nul && set "TS=tailscale"
if not defined TS if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not defined TS (
  echo  Tailscale ist noch nicht installiert. Installation ueber winget ...
  winget install --id tailscale.tailscale --accept-source-agreements --accept-package-agreements
  echo.
  echo  Fertig. Jetzt rechts unten im Infobereich auf das Tailscale-Symbol klicken,
  echo  "Log in" waehlen und mit dem Google-Konto anmelden. Danach diese Datei neu starten.
  pause
  exit /b 0
)

rem --- Pakete und App ---
echo  Pakete pruefen ...
py -m pip install --quiet --disable-pip-version-check flask requests numpy opencv-python pillow
if errorlevel 1 (
  echo  FEHLER: Pakete konnten nicht installiert werden. Ist Python installiert und Internet da?
  pause
  exit /b 1
)
start "RoadSense App (offen lassen)" cmd /k py app.py
timeout /t 4 /nobreak >nul

echo.
echo  ===================================================================
echo   Schluessel fuer Render (Environment, Name AUSWERTUNG_TOKEN):
echo     %TOKEN%
echo.
echo   Gleich erscheint unten die feste Adresse dieses Laptops, z. B.
echo     https://laptop-name.tail1234.ts.net
echo   Diese Adresse bei Render als AUSWERTUNG_URL eintragen (nur einmal noetig).
echo   Beim allerersten Start zeigt Tailscale einen Link, um Funnel freizuschalten:
echo   Link oeffnen, bestaetigen, dann diese Datei neu starten.
echo.
echo   BEIDE Fenster offen lassen, solange ausgewertet werden soll.
echo  ===================================================================
echo.
"%TS%" funnel 8000
echo.
echo  Der Tunnel wurde beendet.
pause
