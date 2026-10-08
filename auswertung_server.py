"""Fotos annehmen, das RoadSense-Auswerteprogramm starten und das Ergebnis für die App aufbereiten.

Das eigentliche Auswerteprogramm liegt unverändert in auswertung/Lebende_Strasse.py.
Wird es verbessert, einfach diese Datei ersetzen. Es läuft als eigener Prozess, damit ein
Absturz oder hoher Speicherbedarf den Webserver nicht mitreißt.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from PIL.TiffImagePlugin import IFDRational

import roadai_core as core

try:  # HEIC-Fotos (iPhone) direkt lesen, wenn das Paket installiert ist
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_MOEGLICH = True
except Exception:  # pragma: no cover - hängt von der Installation ab
    HEIC_MOEGLICH = False

BASIS = Path(__file__).resolve().parent
PROGRAMM = BASIS / "auswertung" / "Lebende_Strasse.py"
ARBEIT = Path(os.environ.get("ROADAI_ARBEIT") or Path(tempfile.gettempdir()) / "roadai_auftraege")

MAX_FOTOS = 25
MIN_FOTOS = 2
MAX_FOTO_BYTES = 30_000_000
MAX_KANTE = 4032              # px; spart Speicher, Ergebnis weicht im Test um 1–2 Prozentpunkte ab
LAUFZEIT_MAX = 20 * 60        # s
AUFBEWAHRUNG = 24 * 3600      # s, danach werden Bericht und Bilder gelöscht
MAX_AUFTRAEGE = 30

ENDUNGEN = (".jpg", ".jpeg", ".png", ".heic", ".heif")
_ID = re.compile(r"[0-9a-f]{32}")


class UploadFehler(core.DatenFehler):
    pass


# ---------------------------------------------------------------------------
# Fotos vorbereiten: verkleinern, EXIF (Zeit, GPS, Brennweite) sicherstellen
# ---------------------------------------------------------------------------

def _grad(wert: float):
    wert = abs(wert)
    g = int(wert)
    m = int((wert - g) * 60)
    s = (wert - g - m / 60) * 3600
    return (IFDRational(g, 1), IFDRational(m, 1), IFDRational(round(s * 10000), 10000))


def _meta_pruefen(m) -> dict:
    """Metadaten, die das Handy mitschickt (aus EXIF gelesen oder vom Handy-GPS beim Fotografieren)."""
    if not isinstance(m, dict):
        return {}
    out = {}
    lat, lon = m.get("lat"), m.get("lon")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)) \
            and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
        out["lat"], out["lon"] = float(lat), float(lon)
        g = m.get("genauigkeit")
        if isinstance(g, (int, float)) and 0 < g < 10000:
            out["genauigkeit"] = float(g)
    z = m.get("zeit")
    if isinstance(z, str) and re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", z):
        out["zeit"] = z
    f = m.get("f35")
    if isinstance(f, (int, float)) and 10 <= f <= 300:
        out["f35"] = int(f)
    mod = m.get("modell")
    if isinstance(mod, str):
        out["modell"] = re.sub(r"[^\w .,()+-]", "", mod)[:40]
    return out


def foto_vorbereiten(quelle: Path, ziel: Path, meta: dict) -> dict:
    """Öffnet ein Foto, dreht es richtig, verkleinert es und schreibt die nötigen EXIF-Daten.

    Vorhandene EXIF-Daten des Fotos haben Vorrang; nur Fehlendes wird aus `meta` ergänzt.
    Gibt zurück, was am Ende im Foto steht (für Hinweise in der App).
    """
    try:
        im = Image.open(quelle)
        im.load()
    except Exception as e:
        if quelle.suffix.lower() in (".heic", ".heif") and not HEIC_MOEGLICH:
            raise UploadFehler(f"{quelle.name}: HEIC kann der Server nicht lesen. Am iPhone unter "
                               "Einstellungen → Kamera → Formate „Maximale Kompatibilität“ wählen.") from e
        raise UploadFehler(f"{quelle.name}: Das ist kein lesbares Foto.") from e
    if im.width * im.height > 120_000_000:
        raise UploadFehler(f"{quelle.name}: Das Foto ist zu groß.")

    exif = im.getexif()
    if not exif and "exif" in im.info:
        try:
            exif.load(im.info["exif"])
        except Exception:
            pass
    im = ImageOps.exif_transpose(im).convert("RGB")
    s = MAX_KANTE / max(im.size)
    if s < 1:
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)

    exif[0x0112] = 1  # Bild ist jetzt aufrecht
    ie = exif.get_ifd(0x8769)
    gps = exif.get_ifd(0x8825)
    if meta.get("zeit") and not ie.get(0x9003):
        ie[0x9003] = meta["zeit"]
    if meta.get("f35") and not ie.get(0xA405):
        ie[0xA405] = meta["f35"]
    if meta.get("modell") and not exif.get(0x0110):
        exif[0x0110] = meta["modell"]
    gps_da = 2 in gps and 4 in gps
    if not gps_da and "lat" in meta:
        gps[1] = "N" if meta["lat"] >= 0 else "S"
        gps[2] = _grad(meta["lat"])
        gps[3] = "E" if meta["lon"] >= 0 else "W"
        gps[4] = _grad(meta["lon"])
        if "genauigkeit" in meta:
            gps[31] = IFDRational(round(meta["genauigkeit"] * 100), 100)
        gps_da = True
    im.save(ziel, "JPEG", quality=92, exif=exif.tobytes())
    return {"gps": gps_da, "zeit": bool(ie.get(0x9003))}


# ---------------------------------------------------------------------------
# Ergebnis des Auswerteprogramms → Format roadai.strecke.v1
# ---------------------------------------------------------------------------

def _zahl(v, std=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else std


def ergebnis_zu_strecke(daten: dict, auftrag_id: str, *, name: str, ort: str, quelle: str = "",
                        bilder: set[str] | None = None, basis: str | None = None,
                        karte: bool = False, bericht: bool = True) -> dict:
    """Übersetzt ergebnisse.json des Python-Programms in das Format der App."""
    ab_roh = daten.get("abschnitte") or []
    if not ab_roh:
        raise UploadFehler("Auf keinem Foto konnte eine Fahrbahn ausgewertet werden.")
    breiten = sorted(_zahl(a.get("fahrbahnbreite")) for a in ab_roh if _zahl(a.get("fahrbahnbreite")) > 0)
    breite = breiten[len(breiten) // 2] if breiten else 5.0
    bilder = bilder or set()
    basis = basis or f"/api/auswertung/{auftrag_id}"

    abschnitte = []
    for a in ab_roh:
        foto = str(a.get("datei") or "")
        datei = f"{foto}_draufsicht_klein.jpg"
        kal = str(a.get("kalibrierung") or "")
        zeit = str(a.get("zeit") or "")
        abschnitte.append({
            "nr": int(a.get("nr") or len(abschnitte) + 1),
            "station_von_m": _zahl(a.get("von")),
            "station_bis_m": _zahl(a.get("bis"), _zahl(a.get("von")) + 5),
            "laengsrisse_m": round(_zahl(a.get("laengsriss")), 2),
            "querrisse_m": round(_zahl(a.get("querriss")), 2),
            "netzrisse_m2": round(_zahl(a.get("netzriss_flaeche")), 3),
            "flickstellen_m2": round(_zahl(a.get("flickflaeche")), 3),
            "schaden_m2": round(_zahl(a.get("schadflaeche")), 3),
            "bezugsflaeche_m2": round(_zahl(a.get("bezugsflaeche"), 0) or breite * 5, 3) or None,
            "schadanteil_pct": round(min(max(_zahl(a.get("schadanteil")), 0), 100), 2),
            "foto": re.sub(r"^\d{2}_", "", foto),
            "aufnahmezeit": zeit,
            "lat": a.get("lat") if isinstance(a.get("lat"), (int, float)) else None,
            "lon": a.get("lon") if isinstance(a.get("lon"), (int, float)) else None,
            "gps_genauigkeit_m": a.get("gps_genauigkeit") if isinstance(a.get("gps_genauigkeit"), (int, float)) else None,
            "bildqualitaet": "gut" if kal == "eigen" else "maessig",
            "hinweis": "" if kal == "eigen" else "Kalibrierung vom Streckenmittel übernommen",
            "bild": f"{basis}/bilder/{datei}" if datei in bilder else None,
            "foto_bild": f"{basis}/bilder/{foto}_foto.jpg" if f"{foto}_foto.jpg" in bilder else None,
        })

    nicht = []
    for x in daten.get("nicht_ausgewertet") or []:
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            nicht.append({"foto": str(x[0]), "grund": str(x[1])})
        elif isinstance(x, dict):
            nicht.append({"foto": str(x.get("foto", "")), "grund": str(x.get("grund", ""))})

    zeiten = sorted(a["aufnahmezeit"] for a in abschnitte if a["aufnahmezeit"])
    aufnahme = ""
    if zeiten:
        try:
            aufnahme = datetime.strptime(zeiten[0], "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    ohne_gps = sum(1 for a in abschnitte if a["lat"] is None)
    grenzen = [
        "Prototyp: Zustandsklassen nach Projektvorgabe (10-%-Stufen des Schadanteils), nicht nach RVS 13.01.15.",
        "Klassische Bildverarbeitung ohne trainiertes Modell; Werte sind Schätzungen, etwa ±10 %.",
        "Grobe Oberflächentextur kann als Riss gezählt werden, einzelne feine Risse können fehlen.",
        "Schlaglöcher werden nicht bestimmt; dafür braucht es Tiefeninformation (LiDAR).",
        "Für das Hochladen wurden die Fotos auf 4032 Pixel verkleinert; das verändert Werte um etwa 1–2 Prozentpunkte.",
    ]
    if ohne_gps:
        grenzen.append(f"{ohne_gps} Foto(s) ohne GPS: Lage dieser Abschnitte unbekannt.")

    return {
        "schema": core.SCHEMA,
        "strecke_id": f"UP_{auftrag_id[:8]}",
        "name": name or str(daten.get("titel") or "Neue Strecke"),
        "ort": ort or str(daten.get("ort") or ""),
        "aufnahme": aufnahme,
        "geraet": next((str(a.get("modell")) for a in ab_roh if a.get("modell")), ""),
        "methode": f"RoadSense Auswerteprogramm v{daten.get('version', '?')}, klassische Bildverarbeitung",
        "quelle": quelle or "In der App hochgeladene Fotos",
        "simuliert": False,
        "fahrbahnbreite_m": round(breite, 2),
        "abschnittslaenge_m": 5.0,
        "abschnitte": abschnitte,
        "grenzen": grenzen,
        "nicht_ausgewertet": nicht,
        "bericht": f"{basis}/Bericht.html" if bericht else None,
        "karte": f"{basis}/Karte.html" if karte else None,
    }


# ---------------------------------------------------------------------------
# Aufträge: eine Auswertung nach der anderen (spart Speicher)
# ---------------------------------------------------------------------------

class Auftrag:
    def __init__(self, name: str, ort: str, breite: float | None, anzahl: int):
        self.id = uuid.uuid4().hex
        self.name, self.ort, self.breite = name, ort, breite
        self.status = "wartet"          # wartet | laeuft | fertig | fehler
        self.phase = "In der Warteschlange"
        self.gesamt, self.erledigt = anzahl, 0
        self.fehler = ""
        self.strecke = None
        self.hinweise: list[str] = []
        self.erstellt = time.time()
        self.ordner = ARBEIT / self.id

    def als_dict(self, position: int = 0) -> dict:
        d = {"id": self.id, "status": self.status, "phase": self.phase, "gesamt": self.gesamt,
             "erledigt": self.erledigt, "hinweise": self.hinweise}
        if self.status == "wartet" and position:
            d["phase"] = f"In der Warteschlange (Platz {position})"
        if self.status == "fehler":
            d["fehler"] = self.fehler
        if self.status == "fertig":
            d["strecke"] = self.strecke
        return d


_auftraege: dict[str, Auftrag] = {}
_warteschlange: list[str] = []
_sperre = threading.Lock()
_signal = threading.Condition(_sperre)
_arbeiter: threading.Thread | None = None


def auftrag(aid: str) -> Auftrag | None:
    if not isinstance(aid, str) or not _ID.fullmatch(aid):
        return None
    with _sperre:
        return _auftraege.get(aid)


def status(aid: str) -> dict | None:
    with _sperre:
        a = _auftraege.get(aid) if isinstance(aid, str) else None
        if not a:
            return None
        pos = _warteschlange.index(aid) + 1 if aid in _warteschlange else 0
        return a.als_dict(pos)


def anlegen(dateien: list, metas: list, name: str, ort: str, breite) -> Auftrag:
    """dateien: Liste von (Dateiname, Stream). Prüft alles, speichert die Fotos und reiht den Auftrag ein."""
    if not dateien:
        raise UploadFehler("Es wurden keine Fotos geschickt.")
    if len(dateien) < MIN_FOTOS:
        raise UploadFehler(f"Bitte mindestens {MIN_FOTOS} Fotos aufnehmen (alle ca. 5 m eines).")
    if len(dateien) > MAX_FOTOS:
        raise UploadFehler(f"Höchstens {MAX_FOTOS} Fotos pro Strecke.")
    name = core._text(name, "name", 60) or f"Strecke {datetime.now():%d.%m.%Y %H:%M}"
    ort = core._text(ort, "ort", 60)
    b = None
    if breite not in (None, ""):
        try:
            b = float(str(breite).replace(",", "."))
        except ValueError:
            raise UploadFehler("Die Fahrbahnbreite muss eine Zahl sein, z. B. 5,80.")
        if not 2 <= b <= 20:
            raise UploadFehler("Die Fahrbahnbreite muss zwischen 2 und 20 m liegen.")

    aufraeumen()
    a = Auftrag(name, ort, b, len(dateien))
    roh = a.ordner / "roh"
    fotos = a.ordner / "fotos" / "Strecke"
    roh.mkdir(parents=True)
    fotos.mkdir(parents=True)
    ohne_gps = []
    try:
        for i, (dateiname, stream) in enumerate(dateien):
            endung = Path(dateiname or "").suffix.lower()
            if endung not in ENDUNGEN:
                endung = ".jpg"
            basis = re.sub(r"[^A-Za-z0-9_-]", "_", Path(dateiname or f"foto_{i + 1}").stem)[:40] or f"foto_{i + 1}"
            basis = f"{i + 1:02d}_{basis}"
            pfad = roh / f"{basis}{endung}"
            groesse = 0
            with open(pfad, "wb") as fh:
                while chunk := stream.read(1 << 20):
                    groesse += len(chunk)
                    if groesse > MAX_FOTO_BYTES:
                        raise UploadFehler(f"{dateiname}: Das Foto ist größer als 30 MB.")
                    fh.write(chunk)
            if groesse == 0:
                raise UploadFehler(f"{dateiname or 'Ein Foto'} ist leer.")
            meta = _meta_pruefen(metas[i] if i < len(metas) else None)
            info = foto_vorbereiten(pfad, fotos / f"{basis}.jpg", meta)
            pfad.unlink(missing_ok=True)
            if not info["gps"]:
                ohne_gps.append(dateiname or basis)
    except Exception:
        shutil.rmtree(a.ordner, ignore_errors=True)
        raise
    shutil.rmtree(roh, ignore_errors=True)
    if ohne_gps:
        a.hinweise.append(f"{len(ohne_gps)} Foto(s) ohne GPS. Lageplan und Karte fehlen dann teilweise.")

    with _signal:
        _auftraege[a.id] = a
        _warteschlange.append(a.id)
        _arbeiter_starten()
        _signal.notify()
    return a


def _arbeiter_starten():
    global _arbeiter
    if _arbeiter is None or not _arbeiter.is_alive():
        _arbeiter = threading.Thread(target=_arbeiten, name="roadai-auswertung", daemon=True)
        _arbeiter.start()


def _arbeiten():
    while True:
        with _signal:
            while not _warteschlange:
                _signal.wait()
            aid = _warteschlange.pop(0)
            a = _auftraege.get(aid)
        if a:
            try:
                ausfuehren(a)
            except Exception as e:  # nie den Arbeiter sterben lassen
                a.status, a.fehler = "fehler", f"Unerwarteter Fehler bei der Auswertung ({type(e).__name__})."


_ZEILE_FOTO = re.compile(r"^\s+· (\S+) … (.*)$")


def ausfuehren(a: Auftrag):
    a.status, a.phase = "laeuft", "Fahrbahn erkennen und kalibrieren"
    befehl = [sys.executable, "-u", str(PROGRAMM), str(a.ordner / "fotos" / "Strecke"),
              "--ausgabe", str(a.ordner / "ergebnis"), "--titel", a.name, "--ort", a.ort]
    if a.breite:
        befehl += ["--breite", f"{a.breite:.2f}"]
    umgebung = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    letzte = []
    start = time.time()
    proc = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", cwd=str(PROGRAMM.parent), env=umgebung)
    try:
        for zeile in proc.stdout:
            zeile = zeile.rstrip()
            letzte = (letzte + [zeile])[-12:]
            if "Schäden" in zeile or _ZEILE_FOTO.match(zeile) and zeile.endswith("…"):
                a.phase = "Schäden auswerten"
            m = _ZEILE_FOTO.match(zeile)
            if m and ("%" in m.group(2) or "übersprungen" in m.group(2)):
                a.erledigt = min(a.erledigt + 1, a.gesamt)
                a.phase = f"Schäden auswerten ({a.erledigt}/{a.gesamt})"
            if zeile.strip().startswith("→"):
                a.phase = "Bericht erstellen"
            if time.time() - start > LAUFZEIT_MAX:
                proc.kill()
                raise TimeoutError
        proc.wait(timeout=30)
    except TimeoutError:
        a.status, a.fehler = "fehler", "Die Auswertung hat zu lange gedauert. Bitte mit weniger Fotos versuchen."
        return
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(a.ordner / "fotos", ignore_errors=True)  # Originalfotos sofort löschen

    ziel = a.ordner / "ergebnis" / "Strecke"
    datei = ziel / "ergebnisse.json"
    if proc.returncode != 0 or not datei.exists():
        a.status = "fehler"
        a.fehler = _fehlertext(letzte, proc.returncode)
        return
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
        bilder = {p.name for p in (ziel / "bilder").glob("*.jpg")}
        roh = ergebnis_zu_strecke(daten, a.id, name=a.name, ort=a.ort, bilder=bilder,
                                  karte=(ziel / "Karte.html").is_file())
        a.strecke = core.pruefe_strecke(roh)
    except core.DatenFehler as e:
        a.status, a.fehler = "fehler", str(e)
        return
    a.erledigt = a.gesamt
    a.status, a.phase = "fertig", "Fertig"


def _fehlertext(zeilen: list[str], code) -> str:
    text = "\n".join(zeilen)
    if "Kein Foto konnte ausgewertet werden" in text or "Fahrbahn nicht gefunden" in text:
        return ("Auf den Fotos wurde keine Fahrbahn erkannt. Bitte in Fahrtrichtung fotografieren, "
                "beide Fahrbahnränder im Bild, Kamera auf Augenhöhe.")
    if "Kalibrierung nicht möglich" in text:
        return "Die Kamera ließ sich nicht kalibrieren. Bitte die Fahrbahnbreite eingeben und erneut versuchen."
    if "MemoryError" in text or code in (-9, 137):
        return "Der Server hatte nicht genug Speicher. Bitte weniger Fotos auf einmal hochladen."
    if "ModuleNotFoundError" in text:
        return "Am Server fehlt ein Paket für die Bildauswertung (siehe requirements.txt)."
    return "Die Auswertung ist fehlgeschlagen. Bitte die Fotos prüfen und erneut versuchen."


def datei_pfad(aid: str, teil: str) -> Path | None:
    """Sicherer Pfad zu Bericht.html oder bilder/<name>.jpg eines Auftrags."""
    a = auftrag(aid)
    if not a or a.status != "fertig":
        return None
    basis = (a.ordner / "ergebnis" / "Strecke").resolve()
    if teil in ("Bericht.html", "Karte.html"):
        p = basis / teil
    elif re.fullmatch(r"bilder/[A-Za-z0-9_.-]{1,80}\.jpg", teil):
        p = (basis / teil).resolve()
        if basis not in p.parents:
            return None
    else:
        return None
    return p if p.is_file() else None


def aufraeumen():
    jetzt = time.time()
    with _sperre:
        alt = sorted(_auftraege.values(), key=lambda x: x.erstellt)
        weg = [x for x in alt if jetzt - x.erstellt > AUFBEWAHRUNG and x.id not in _warteschlange]
        fertig = [x for x in alt if x.status in ("fertig", "fehler") and x not in weg]
        weg += fertig[:max(0, len(fertig) - MAX_AUFTRAEGE)]
        for x in weg:
            _auftraege.pop(x.id, None)
            shutil.rmtree(x.ordner, ignore_errors=True)


def wartende() -> int:
    with _sperre:
        return len(_warteschlange) + sum(1 for x in _auftraege.values() if x.status == "laeuft")
