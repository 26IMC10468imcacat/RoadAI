"""Weiterleitung der Fotoauswertung an einen Auswerte-Laptop.

Die Webseite (z. B. Render, Gratis-Plan) hat zu wenig Speicher für die Bildauswertung.
Ist AUSWERTUNG_URL gesetzt, schickt sie Fotos, Statusabfragen und Ergebnisbilder an den Laptop
(z. B. https://laptop.xyz.ts.net über Tailscale Funnel) und reicht die Antworten an das Handy durch.
Beide Seiten kennen denselben geheimen Schlüssel AUSWERTUNG_TOKEN.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

import requests
from flask import Response, jsonify

KOPF = "X-RoadSense-Token"
_ID = re.compile(r"/api/auswertung/[0-9a-f]{16}(/(Bericht\.html|Karte\.html|bilder/[A-Za-z0-9_.-]{1,80}\.jpg))?")
_DURCHREICHEN = ("Content-Type", "Cache-Control")


def _fehler(text: str, code: int):
    return jsonify({"ok": False, "fehler": text}), code


class Fernauswertung:
    def __init__(self, url: str, token: str):
        url = (url or "").strip().rstrip("/")
        u = urlparse(url)
        lokal = u.hostname in ("localhost", "127.0.0.1")  # nur zum Testen auf einem Rechner
        self.aktiv = bool(url) and bool(u.hostname) and (u.scheme == "https" or (u.scheme == "http" and lokal))
        self.url, self.token = url, (token or "").strip()
        self._stand: tuple[float, bool] = (0.0, False)
        self._sperre = threading.Lock()

    def _kopf(self) -> dict:
        return {KOPF: self.token, "ngrok-skip-browser-warning": "1"}

    def erreichbar(self) -> bool:
        """Läuft der Laptop und kann er auswerten? Ergebnis 20 s zwischengespeichert."""
        with self._sperre:
            zeit, ok = self._stand
            if time.time() - zeit < 20:
                return ok
        try:
            r = requests.get(self.url + "/api/status", headers=self._kopf(), timeout=4)
            ok = r.ok and bool(r.json().get("auswertung"))
        except (requests.RequestException, ValueError):
            ok = False
        with self._sperre:
            self._stand = (time.time(), ok)
        return ok

    def _offline(self):
        with self._sperre:
            self._stand = (time.time(), False)
        return _fehler("Der Auswerte-Laptop ist gerade nicht erreichbar. Bitte später noch einmal versuchen.", 503)

    def hochladen(self, anfrage):
        """Leitet den Upload als Rohdatenstrom an den Auswerte-Laptop weiter.

        Wichtig für Render Free: ``request.files`` darf hier NICHT gelesen werden.
        Werkzeug würde sonst den Multipart-Upload zerlegen und ``requests`` beim
        erneuten Zusammensetzen die Fotos zusätzlich im RAM puffern. Bei mehreren
        hochauflösenden Fotos kann das die 512-MB-Grenze von Render überschreiten.

        Stattdessen reichen wir den originalen Multipart-Body samt Content-Type
        unverändert und streaming weiter. Die Fotos werden erst auf dem Laptop
        geparst und verarbeitet.
        """
        kopf = self._kopf()
        content_type = anfrage.headers.get("Content-Type")
        if content_type:
            kopf["Content-Type"] = content_type
        if anfrage.content_length is not None:
            kopf["Content-Length"] = str(anfrage.content_length)
        try:
            r = requests.post(
                self.url + "/api/auswertung",
                headers=kopf,
                data=anfrage.stream,
                timeout=(6, 600),
            )
        except requests.RequestException:
            return self._offline()
        return self._antwort(r)

    def weiter(self, pfad: str):
        if not _ID.fullmatch(pfad):
            return _fehler("Nicht gefunden.", 404)
        try:
            r = requests.get(self.url + pfad, headers=self._kopf(), timeout=(6, 60), stream=True)
        except requests.RequestException:
            return self._offline()
        return self._antwort(r, stream=True)

    def _antwort(self, r, stream: bool = False):
        if r.status_code in (502, 503, 504) and "json" not in r.headers.get("Content-Type", ""):
            r.close()
            return self._offline()  # Tunnel steht, aber die App auf dem Laptop läuft nicht
        kopf = {k: r.headers[k] for k in _DURCHREICHEN if k in r.headers}
        if stream:
            return Response(r.iter_content(64 * 1024), status=r.status_code, headers=kopf)
        return Response(r.content, status=r.status_code, headers=kopf)
