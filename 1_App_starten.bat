@echo off
rem RoadAI Web-App auf diesem PC starten. Diese Datei muss neben app.py liegen.
cd /d "%~dp0"
echo.
echo  RoadAI Web-App wird gestartet ...
echo.
if not exist "app.py" (
  echo  FEHLER: app.py nicht gefunden. Bitte die ZIP zuerst mit "Alle extrahieren" entpacken.
  pause
  exit /b 1
)
if not exist ".env" (
  echo  Hinweis: Keine Datei .env gefunden. Die App laeuft ohne KI (Offline-Modus).
  echo  Fuer die KI: .env.example kopieren, Kopie ".env" nennen, API-Schluessel eintragen.
  echo.
)
py -m pip install --quiet --disable-pip-version-check flask requests numpy opencv-python pillow
if errorlevel 1 (
  echo.
  echo  FEHLER: Pakete konnten nicht installiert werden. Ist Python installiert und Internet da?
  pause
  exit /b 1
)
echo.
echo  ============================================================
echo   RoadAI laeuft unter  http://localhost:8000
echo   Dieses Fenster OFFEN LASSEN. Danach 2_Tunnel_starten.bat
echo   starten, damit die Seite am Handy erreichbar ist.
echo  ============================================================
echo.
py app.py
echo.
echo  Die App wurde beendet. Falls oben ein Fehler steht, bitte Screenshot machen.
pause
