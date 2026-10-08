"""RoadSense Web-App (Flask). Start lokal: python app.py  ->  http://localhost:8000"""
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
import auswertung_server as aw
import fernauswertung as fern
import importlib.util

BASIS = Path(__file__).resolve().parent


def _env_datei_laden(pfad: Path) -> None:
    """Liest Einstellungen aus der Datei .env (z. B. ANTHROPIC_API_KEY=...), falls vorhanden.

    Werte, die schon in der Umgebung gesetzt sind (z. B. bei Render), haben Vorrang.
    """
    if not pfad.is_file():
        return
    for zeile in pfad.read_text(encoding="utf-8-sig").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        name, wert = zeile.split("=", 1)
        name, wert = name.strip(), wert.strip().strip('"').strip("'")
        if name and wert and not os.environ.get(name):
            os.environ[name] = wert


_env_datei_laden(BASIS / ".env")
DATEN = BASIS / "data"

BEISPIELE = {
    "TS01": ("teststrecke_01.json", "Auswertung vom 01.10.2026, 10 Fotos"),
    "TS02": ("teststrecke_02.json", "Auswertung vom 01.10.2026, 8 Fotos"),
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 320_000_000  # nur für den Foto-Upload; JSON-Anfragen siehe unten
JSON_MAX = 1_000_000  # 1 MB reicht für Hunderte Abschnitte
AUSWERTUNG_BEREIT = aw.PROGRAMM.is_file() and all(importlib.util.find_spec(m) for m in ("cv2", "numpy"))
app.json.ensure_ascii = False

GEHEIM = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Rollen (siehe LIES_MICH_ZUERST.txt, Abschnitt "Render + Auswerte-Laptop"):
#  - Webseite (Render): AUSWERTUNG_URL gesetzt  -> Fotos gehen an den Laptop weiter.
#  - Auswerte-Laptop:   AUSWERTUNG_TOKEN gesetzt -> nimmt Auswertungen nur mit diesem Schlüssel an.
FERN = fern.Fernauswertung(os.environ.get("AUSWERTUNG_URL", ""), os.environ.get("AUSWERTUNG_TOKEN", ""))
LAPTOP_TOKEN = "" if FERN.aktiv else os.environ.get("AUSWERTUNG_TOKEN", "").strip()
ki = core.KiClient()


# --- Schutz: Anfragen pro Minute und doppelte Zapier-Sendungen ---------------
_zaehler: dict[tuple[str, str], deque] = defaultdict(deque)
_gesendet: dict[str, float] = {}
_sperre = Lock()

GRENZEN = {"ki": 20, "zapier": 6, "standard": 120, "upload": 4}


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


@app.before_request
def laptop_schutz():
    """Auf dem Auswerte-Laptop: Auswertungen nur für die eigene Webseite (gemeinsamer Schlüssel)."""
    if LAPTOP_TOKEN and request.path.startswith("/api/auswertung"):
        if not hmac.compare_digest(request.headers.get(fern.KOPF, ""), LAPTOP_TOKEN):
            return fehler("Dieser Rechner wertet nur für die RoadSense-Webseite aus.", 403)


@app.before_request
def json_groesse():
    if request.method == "POST" and request.path != "/api/auswertung" and (request.content_length or 0) > JSON_MAX:
        return fehler("Die Datei ist zu groß (höchstens 1 MB).", 413)


@app.after_request
def sicherheits_header(antwort):
    antwort.headers.setdefault("X-Content-Type-Options", "nosniff")
    antwort.headers.setdefault("Referrer-Policy", "same-origin")
    antwort.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return antwort


# --- API ----------------------------------------------------------------------
@app.get("/api/status")
def status():
    return jsonify({
        "ok": True,
        "ki": ki.verfuegbar,
        "modell": ki.modell if ki.verfuegbar else None,
        "zapier": core.zapier_url_gueltig(os.environ.get("ZAPIER_WEBHOOK_URL", "")),
        **({"auswertung": FERN.erreichbar(), "auswertung_hinweis": None if FERN.erreichbar() else
            "Der Auswerte-Laptop ist gerade nicht erreichbar. Fotos aufnehmen geht, auswerten erst, wenn er läuft."}
           if FERN.aktiv else {"auswertung": AUSWERTUNG_BEREIT}),
        "max_fotos": aw.MAX_FOTOS,
    })


@app.get("/api/beispiele")
def beispiele():
    liste = []
    for sid, (datei, beschreibung) in BEISPIELE.items():
        if not (DATEN / datei).is_file():
            continue  # gelöschte Beispiele einfach überspringen
        roh = json.loads((DATEN / datei).read_text(encoding="utf-8"))
        liste.append({"id": sid, "name": roh["name"], "simuliert": roh.get("simuliert", False),
                      "beschreibung": beschreibung})
    return jsonify({"ok": True, "beispiele": liste})


@app.get("/api/beispiele/<sid>")
def beispiel(sid):
    if sid not in BEISPIELE or not (DATEN / BEISPIELE[sid][0]).is_file():
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
        return fehler("Diese Bewertung stammt nicht von RoadSense oder wurde verändert. Bitte neu bewerten.", 403)
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


# --- Fotos auswerten -------------------------------------------------------------
@app.post("/api/auswertung")
def auswertung_starten():
    if FERN.aktiv:
        if zu_viele("upload"):
            return fehler("Zu viele Auswertungen in kurzer Zeit. Bitte eine Minute warten.", 429)
        return FERN.hochladen(request)
    if not AUSWERTUNG_BEREIT:
        return fehler("Die Bildauswertung ist auf diesem Server nicht eingerichtet.", 503)
    if zu_viele("upload"):
        return fehler("Zu viele Auswertungen in kurzer Zeit. Bitte eine Minute warten.", 429)
    if aw.wartende() >= 5:
        return fehler("Gerade laufen viele Auswertungen. Bitte in ein paar Minuten erneut versuchen.", 503)
    fotos = request.files.getlist("fotos")
    try:
        metas = json.loads(request.form.get("meta") or "[]")
    except ValueError:
        metas = []
    if not isinstance(metas, list):
        metas = []
    a = aw.anlegen([(f.filename, f.stream) for f in fotos], metas[:aw.MAX_FOTOS + 1],
                   request.form.get("name", ""), request.form.get("ort", ""), request.form.get("breite"))
    return jsonify({"ok": True, "auftrag": aw.status(a.id)})


@app.get("/api/auswertung/<aid>")
def auswertung_status(aid):
    if FERN.aktiv:
        return FERN.weiter(f"/api/auswertung/{aid}")
    s = aw.status(aid)
    if not s:
        return fehler("Diese Auswertung gibt es nicht (mehr).", 404)
    return jsonify({"ok": True, "auftrag": s})


@app.get("/api/auswertung/<aid>/<path:teil>")
def auswertung_datei(aid, teil):
    if FERN.aktiv:
        return FERN.weiter(f"/api/auswertung/{aid}/{teil}")
    p = aw.datei_pfad(aid, teil)
    if not p:
        return fehler("Nicht gefunden.", 404)
    antwort = send_from_directory(p.parent, p.name)
    antwort.headers["Cache-Control"] = "private, max-age=3600"
    return antwort


# --- Fehler immer als freundliche Antwort ----------------------------------------
@app.errorhandler(core.DatenFehler)
def daten_fehler(e):
    return fehler(str(e), 422)


@app.errorhandler(HTTPException)
def http_fehler(e):
    texte = {404: "Nicht gefunden.", 405: "Diese Aktion ist hier nicht erlaubt.",
             413: "Die Datei ist zu groß.", 400: "Die Anfrage war ungültig."}
    if not request.path.startswith("/api/") and e.code == 404:
        return startseite()
    return fehler(texte.get(e.code, "Die Anfrage konnte nicht verarbeitet werden."), e.code or 400)


@app.errorhandler(Exception)
def unerwartet(e):
    app.logger.exception("Unerwarteter Fehler")
    return fehler("Da ist etwas schiefgegangen. Bitte noch einmal versuchen.", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
