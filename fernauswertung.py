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
        fotos = anfrage.files.getlist("fotos")
        teile = [("fotos", (f.filename or "foto.jpg", f.stream, f.mimetype or "image/jpeg")) for f in fotos]
        felder = {k: anfrage.form.get(k, "") for k in ("meta", "name", "ort", "breite")}
        try:
            r = requests.post(self.url + "/api/auswertung", headers=self._kopf(), data=felder, files=teile,
                              timeout=(6, 600))
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
