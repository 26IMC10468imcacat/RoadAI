"""RoadAI Web-App (Flask). Start lokal: python app.py  ->  http://localhost:8000"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import roadai_core as core

BASIS = Path(__file__).resolve().parent
DATEN = BASIS / "data"

BEISPIELE = {
    "TS01": ("teststrecke_01.json", "Echte Auswertung vom 01.10.2026, 10 Fotos"),
    "DEMO_GUT": ("demo_gut.json", "Simuliert: zeigt den Weg „Kein Handlungsbedarf“"),
    "DEMO_MITTEL": ("demo_mittel.json", "Simuliert: zeigt den Weg „Sanierung einplanen“"),
    "DEMO_SCHLECHTE_FOTOS": ("demo_schlechte_fotos.json", "Simuliert: zeigt den Weg „Bitte neu aufnehmen“"),
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # 1 MB reicht für Hunderte Abschnitte
app.json.ensure_ascii = False

GEHEIM = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ki = core.KiClient()


# --- Schutz: Anfragen pro Minute und doppelte Zapier-Sendungen ---------------
_zaehler: dict[tuple[str, str], deque] = defaultdict(deque)
_gesendet: dict[str, float] = {}
_sperre = Lock()

GRENZEN = {"ki": 20, "zapier": 6, "standard": 120}


def zu_viele(art: str) -> bool:
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    jetzt = time.time()
    with _sperre:
        q = _zaehler[(ip, art)]
        while q and jetzt - q[0] > 60:
            q.popleft()
        if len(q) >= GRENZEN[art]:
            return True
        q.append(jetzt)
    return False


def fehler(text: str, code: int = 400):
    return jsonify({"ok": False, "fehler": text}), code


def json_body() -> dict:
    daten = request.get_json(silent=True)
    if not isinstance(daten, dict):
        raise core.DatenFehler("Anfrage ohne gültige Daten.")
    return daten


def strecke_aus(daten: dict) -> dict:
    return core.pruefe_strecke(daten.get("strecke"))


# --- Signatur: Entscheidungen kommen nur vom Server ---------------------------
def _fingerabdruck(strecke: dict, entscheidung: dict) -> str:
    kern = {k: entscheidung.get(k) for k in
            ("route", "begruendung", "prioritaet_abschnitte", "naechster_schritt", "quelle", "entscheidung_id")}
    roh = json.dumps({"s": strecke, "e": kern}, sort_keys=True, ensure_ascii=False, default=str)
    return hmac.new(GEHEIM.encode(), roh.encode(), hashlib.sha256).hexdigest()


# --- Seiten -------------------------------------------------------------------
@app.get("/")
def startseite():
    antwort = send_from_directory(BASIS / "static", "index.html")
    antwort.headers["Cache-Control"] = "no-cache"
    return antwort


@app.get("/sw.js")
def service_worker():
    antwort = send_from_directory(BASIS / "static", "sw.js", mimetype="application/javascript")
    antwort.headers["Cache-Control"] = "no-cache"
    return antwort


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASIS / "static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/static/<path:datei>")
def statisch(datei):
    return send_from_directory(BASIS / "static", datei)


@app.after_request
def sicherheits_header(antwort):
    antwort.headers.setdefault("X-Content-Type-Options", "nosniff")
    antwort.headers.setdefault("Referrer-Policy", "same-origin")
    antwort.headers.setdefault("X-Frame-Options", "DENY")
    return antwort


# --- API ----------------------------------------------------------------------
@app.get("/api/status")
def status():
    return jsonify({
        "ok": True,
        "ki": ki.verfuegbar,
        "modell": ki.modell if ki.verfuegbar else None,
        "zapier": core.zapier_url_gueltig(os.environ.get("ZAPIER_WEBHOOK_URL", "")),
    })


@app.get("/api/beispiele")
def beispiele():
    liste = []
    for sid, (datei, beschreibung) in BEISPIELE.items():
        roh = json.loads((DATEN / datei).read_text(encoding="utf-8"))
        liste.append({"id": sid, "name": roh["name"], "simuliert": roh.get("simuliert", False),
                      "beschreibung": beschreibung})
    return jsonify({"ok": True, "beispiele": liste})


@app.get("/api/beispiele/<sid>")
def beispiel(sid):
    if sid not in BEISPIELE:
        return fehler("Diese Strecke gibt es nicht.", 404)
    roh = json.loads((DATEN / BEISPIELE[sid][0]).read_text(encoding="utf-8"))
    return jsonify({"ok": True, "strecke": core.pruefe_strecke(roh)})


@app.post("/api/pruefen")
def pruefen():
    if zu_viele("standard"):
        return fehler("Zu viele Anfragen. Bitte kurz warten.", 429)
    return jsonify({"ok": True, "strecke": strecke_aus(json_body())})


@app.post("/api/entscheidung")
def entscheidung():
    if zu_viele("ki"):
        return fehler("Zu viele Anfragen. Bitte eine Minute warten.", 429)
    strecke = strecke_aus(json_body())
    e = core.entscheide(strecke, ki)
    e["signatur"] = _fingerabdruck(strecke, e)
    return jsonify({"ok": True, "entscheidung": e})


@app.post("/api/chat")
def chat():
    if zu_viele("ki"):
        return fehler("Zu viele Fragen in kurzer Zeit. Bitte eine Minute warten.", 429)
    daten = json_body()
    strecke = strecke_aus(daten)
    e = daten.get("entscheidung")
    if not (isinstance(e, dict) and hmac.compare_digest(str(e.get("signatur", "")), _fingerabdruck(strecke, e))):
        e = None  # unbestätigte Entscheidungen fließen nicht in den Assistenten ein
    return jsonify({"ok": True, **core.chat_antwort(strecke, e, daten.get("verlauf"), ki)})


@app.post("/api/zapier")
def zapier():
    if zu_viele("zapier"):
        return fehler("Zu viele Sendungen. Bitte eine Minute warten.", 429)
    daten = json_body()
    strecke = strecke_aus(daten)
    e = daten.get("entscheidung")
    if not isinstance(e, dict) or not hmac.compare_digest(str(e.get("signatur", "")), _fingerabdruck(strecke, e)):
        return fehler("Diese Bewertung stammt nicht von RoadAI oder wurde verändert. Bitte neu bewerten.", 403)
    eid = e.get("entscheidung_id", "")
    with _sperre:
        if eid in _gesendet:
            return jsonify({"ok": True, "doppelt": True, "meldung": "Diese Bewertung wurde bereits übertragen."})
        _gesendet[eid] = time.time()
        if len(_gesendet) > 5000:
            for alt in sorted(_gesendet, key=_gesendet.get)[:1000]:
                _gesendet.pop(alt, None)
    ok, meldung = core.sende_an_zapier(os.environ.get("ZAPIER_WEBHOOK_URL", ""), core.zapier_payload(strecke, e))
    if not ok:
        with _sperre:
            _gesendet.pop(eid, None)  # erneuter Versuch erlaubt
        return fehler(meldung, 502)
    return jsonify({"ok": True, "meldung": meldung})


# --- Fehler immer als freundliche Antwort ----------------------------------------
@app.errorhandler(core.DatenFehler)
def daten_fehler(e):
    return fehler(str(e), 422)


@app.errorhandler(HTTPException)
def http_fehler(e):
    texte = {404: "Nicht gefunden.", 405: "Diese Aktion ist hier nicht erlaubt.",
             413: "Die Datei ist zu groß (höchstens 1 MB).", 400: "Die Anfrage war ungültig."}
    if not request.path.startswith("/api/") and e.code == 404:
        return startseite()
    return fehler(texte.get(e.code, "Die Anfrage konnte nicht verarbeitet werden."), e.code or 400)


@app.errorhandler(Exception)
def unerwartet(e):
    app.logger.exception("Unerwarteter Fehler")
    return fehler("Da ist etwas schiefgegangen. Bitte noch einmal versuchen.", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
