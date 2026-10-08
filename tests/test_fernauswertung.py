"""Weiterleitung an den Auswerte-Laptop: Schlüssel, Offline-Fall, keine fremden Pfade."""
import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fernauswertung as fern  # noqa: E402


def app_mit(**env):
    alt = {k: os.environ.get(k) for k in ("AUSWERTUNG_URL", "AUSWERTUNG_TOKEN")}
    for k in alt:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        import app as modul
        return importlib.reload(modul).app.test_client()
    finally:
        for k, v in alt.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def tearDownModule():
    app_mit()  # App wieder im Normalbetrieb laden, damit die anderen Tests nicht betroffen sind


class Laptop(unittest.TestCase):
    def test_ohne_schluessel_abgelehnt(self):
        c = app_mit(AUSWERTUNG_TOKEN="abc")
        self.assertEqual(c.post("/api/auswertung").status_code, 403)
        self.assertEqual(c.get("/api/auswertung/0123456789abcdef").status_code, 403)

    def test_mit_schluessel_durchgelassen(self):
        c = app_mit(AUSWERTUNG_TOKEN="abc")
        r = c.get("/api/auswertung/0123456789abcdef", headers={fern.KOPF: "abc"})
        self.assertEqual(r.status_code, 404)  # Schlüssel stimmt, Auftrag gibt es nur nicht

    def test_startseite_bleibt_offen(self):
        c = app_mit(AUSWERTUNG_TOKEN="abc")
        self.assertEqual(c.get("/api/status").status_code, 200)


class Webseite(unittest.TestCase):
    def test_laptop_aus(self):
        c = app_mit(AUSWERTUNG_URL="http://127.0.0.1:9", AUSWERTUNG_TOKEN="abc")
        s = c.get("/api/status").get_json()
        self.assertFalse(s["auswertung"])
        self.assertIn("Laptop", s["auswertung_hinweis"])
        r = c.get("/api/auswertung/0123456789abcdef")
        self.assertEqual(r.status_code, 503)

    def test_fremde_pfade_nicht_weitergeleitet(self):
        f = fern.Fernauswertung("http://127.0.0.1:9", "abc")
        from flask import Flask
        with Flask(__name__).app_context():
            self._pruefe(f)

    def _pruefe(self, f):
        for pfad in ("/api/auswertung/../../etc/passwd", "/api/status", "/api/auswertung/xyz/Bericht.html"):
            self.assertEqual(f.weiter(pfad)[1], 404, pfad)

    def test_nur_https(self):
        self.assertFalse(fern.Fernauswertung("http://laptop.example.ts.net", "abc").aktiv)
        self.assertTrue(fern.Fernauswertung("https://laptop.example.ts.net/", "abc").aktiv)


if __name__ == "__main__":
    unittest.main()
