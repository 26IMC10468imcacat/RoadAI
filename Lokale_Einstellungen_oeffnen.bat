@echo off
cd /d "%~dp0"
for /f "delims=" %%p in ('py lokale_einstellungen.py path') do set "ENVFILE=%%p"
start "" notepad "%ENVFILE%"
