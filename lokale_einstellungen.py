"""RoadSense: lokale Einstellungen außerhalb des Git-Repositories.

Echte Schlüssel werden absichtlich nur lokal gespeichert:
Windows: %LOCALAPPDATA%\\RoadSense\\.env
Andere Systeme: ~/.roadsense/.env

Beim ersten Start wird eine eventuell noch im Projektordner liegende alte .env
in den lokalen Einstellungsordner übernommen und danach aus dem Projektordner entfernt.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASIS = Path(__file__).resolve().parent


def config_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA", "").strip()
    return (Path(root) / "RoadSense") if root else (Path.home() / ".roadsense")


def config_path() -> Path:
    return config_dir() / ".env"


def _lesen(pfad: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not pfad.is_file():
        return out
    for zeile in pfad.read_text(encoding="utf-8-sig").splitlines():
        z = zeile.strip()
        if not z or z.startswith("#") or "=" not in z:
            continue
        k, v = z.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _schreiben(pfad: Path, werte: dict[str, str]) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    reihenfolge = [
        "ANTHROPIC_API_KEY",
        "ZAPIER_WEBHOOK_URL",
        "CLAUDE_MODEL",
        "AUSWERTUNG_TOKEN",
        "SECRET_KEY",
        "ROADSENSE_WEB_ORIGIN",
    ]
    zeilen = [
        "# RoadSense – ausschließlich lokale Einstellungen.",
        "# Diese Datei liegt außerhalb des GitHub-Ordners und darf nicht hochgeladen werden.",
        "",
    ]
    for k in reihenfolge:
        if k in werte:
            zeilen.append(f"{k}={werte[k]}")
    for k in sorted(set(werte) - set(reihenfolge)):
        zeilen.append(f"{k}={werte[k]}")
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def sicherstellen() -> Path:
    ziel = config_path()
    lokal = _lesen(ziel)

    # Einmalige Migration aus einer alten Version im aktuellen Projektordner.
    # Bereits vorhandene Werte im sicheren lokalen Speicher haben Vorrang.
    alt = BASIS / ".env"
    if alt.is_file():
        for k, v in _lesen(alt).items():
            if v and not lokal.get(k):
                lokal[k] = v

    lokal.setdefault("ANTHROPIC_API_KEY", "")
    lokal.setdefault("ZAPIER_WEBHOOK_URL", "")
    lokal.setdefault("CLAUDE_MODEL", "claude-sonnet-5-5")
    lokal.setdefault("ROADSENSE_WEB_ORIGIN", "https://roadai-01hq.onrender.com")
    if not lokal.get("AUSWERTUNG_TOKEN"):
        lokal["AUSWERTUNG_TOKEN"] = secrets.token_urlsafe(32)
    if not lokal.get("SECRET_KEY"):
        lokal["SECRET_KEY"] = secrets.token_urlsafe(48)

    _schreiben(ziel, lokal)

    # Erst nach erfolgreichem Schreiben die alte Repo-.env entfernen.
    if alt.is_file():
        try:
            alt.unlink()
        except OSError:
            pass
    return ziel


def laden_umgebung() -> Path:
    pfad = sicherstellen()
    for k, v in _lesen(pfad).items():
        if v and not os.environ.get(k):
            os.environ[k] = v
    return pfad


if __name__ == "__main__":
    import sys

    p = sicherstellen()
    werte = _lesen(p)
    befehl = sys.argv[1].lower() if len(sys.argv) > 1 else "path"
    if befehl == "token":
        print(werte.get("AUSWERTUNG_TOKEN", ""))
    elif befehl == "origin":
        print(werte.get("ROADSENSE_WEB_ORIGIN", ""))
    else:
        print(p)
