@echo off
rem Macht die laufende RoadAI-App ueber eine https-Adresse im Internet erreichbar (Cloudflare Quick Tunnel).
cd /d "%~dp0"
echo.
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo  cloudflared ist noch nicht installiert. Installation ueber winget ...
  winget install --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
  echo.
  echo  Fertig. Bitte dieses Fenster schliessen und die Datei noch einmal starten.
  pause
  exit /b 0
)
echo  ============================================================
echo   Gleich erscheint unten eine Adresse wie
echo     https://irgendwas.trycloudflare.com
echo   Diese Adresse am Handy oeffnen.
echo   Dieses Fenster OFFEN LASSEN, solange die Seite erreichbar sein soll.
echo   Die Adresse aendert sich bei jedem Neustart.
echo  ============================================================
echo.
cloudflared tunnel --url http://localhost:8000
echo.
echo  Der Tunnel wurde beendet.
pause
