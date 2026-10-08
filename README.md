# RoadSense v4

RoadSense ist eine mobile Web-App zur fotografischen Straßenzustandserfassung. Diese Version trennt **Web-Oberfläche** und **lokale Auswertung** konsequent.

## Datenschutz-/Secret-Prinzip

Echte Schlüssel werden ausschließlich lokal gespeichert unter:

`%LOCALAPPDATA%\RoadSense\.env`

Die Datei liegt außerhalb des Git-Repositories. Sie wird weder von GitHub Desktop verwaltet noch auf Render benötigt. `Auswerte_Laptop_starten.bat` migriert eine noch im Projektordner vorhandene alte `.env` einmalig an diesen Ort.

## Architektur

- **Render Free:** Oberfläche, PWA, Beispielstrecken; leichte Python-Abhängigkeiten.
- **Browser:** speichert Tailscale-Adresse + lokalen Verbindungstoken im `localStorage` des jeweiligen Browsers.
- **Auswerte-Laptop:** Fotoauswertung, Claude-Aufrufe und optional Zapier.
- **Tailscale Funnel:** HTTPS-Verbindung vom Browser direkt zum lokalen Auswerte-Laptop.

Große Foto-Uploads laufen damit nicht mehr durch Render. Das reduziert den RAM-Verbrauch auf Render und hält persönliche API-Schlüssel dort vollständig heraus.

## Lokaler Start

`Auswerte_Laptop_starten.bat` startet die lokale Flask-App und den Tailscale-Funnel. Beide Fenster müssen für Fotoauswertung/Claude geöffnet bleiben.

Lokale Einstellungen öffnen:

`Lokale_Einstellungen_oeffnen.bat`

## Render

`render.yaml` setzt nur `ROADSENSE_ROLE=web` und die Python-Version. Persönliche Schlüssel oder Verbindungstokens sind dort nicht erforderlich.

Nach Umstieg von einer älteren Version müssen bereits manuell in Render gespeicherte Werte wie `ANTHROPIC_API_KEY`, `AUSWERTUNG_TOKEN` oder `AUSWERTUNG_URL` einmalig aus **Environment** gelöscht werden.

## Streckenliste

Neu ausgewertete Strecken werden im Browser gespeichert und unter **„Oder eine ausgewertete Strecke ansehen“** angeboten. Das ist derzeit browserlokal (max. 20 eigene Strecken), noch keine zentrale Datenbank.

## Tests

```text
py -m unittest discover -s tests -v
```

Die lokale Bildauswertung benötigt die Pakete aus `requirements.txt`; Render verwendet bewusst nur `requirements-render.txt`.
