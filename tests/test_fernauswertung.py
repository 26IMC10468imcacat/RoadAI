"""Schutz des lokalen Auswerte-Laptops und alter Weiterleitungs-Fallback."""
import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fernauswertung as fern  # noqa: E402


def app_mit(**env):
    namen = ("ROADSENSE_ROLE", "AUSWERTUNG_URL", "AUSWERTUNG_TOKEN")
    alt = {k: os.environ.get(k) for k in namen}
    for k in namen:
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


class Laptop(unittest.TestCase):
    ID = "0123456789abcdef0123456789abcdef"

    def test_ohne_schluessel_abgelehnt(self):
        c = app_mit(ROADSENSE_ROLE="local", AUSWERTUNG_TOKEN="abc")
        self.assertEqual(c.post("/api/auswertung").status_code, 403)
        self.assertEqual(c.get(f"/api/auswertung/{self.ID}").status_code, 403)

    def test_mit_schluessel_durchgelassen(self):
        c = app_mit(ROADSENSE_ROLE="local", AUSWERTUNG_TOKEN="abc")
        r = c.get(f"/api/auswertung/{self.ID}", headers={fern.KOPF: "abc"})
        self.assertEqual(r.status_code, 404)

    def test_status_bleibt_offen(self):
        c = app_mit(ROADSENSE_ROLE="local", AUSWERTUNG_TOKEN="abc")
        self.assertEqual(c.get("/api/status").status_code, 200)


class Webseite(unittest.TestCase):
    def test_render_verwendet_keine_alten_worker_secrets(self):
        c = app_mit(ROADSENSE_ROLE="web", AUSWERTUNG_URL="https://laptop.example.ts.net", AUSWERTUNG_TOKEN="abc")
        s = c.get("/api/status").get_json()
        self.assertEqual(s["rolle"], "web")
        self.assertFalse(s["auswertung"])
        self.assertEqual(c.post("/api/auswertung").status_code, 503)

    def test_fremde_pfade_nicht_weitergeleitet(self):
        f = fern.Fernauswertung("https://laptop.example.ts.net", "abc")
        from flask import Flask
        with Flask(__name__).app_context():
            for pfad in ("/api/auswertung/../../etc/passwd", "/api/status", "/api/auswertung/xyz/Bericht.html"):
                self.assertEqual(f.weiter(pfad)[1], 404, pfad)

    def test_nur_https(self):
        self.assertFalse(fern.Fernauswertung("http://laptop.example.ts.net", "abc").aktiv)
        self.assertTrue(fern.Fernauswertung("https://laptop.example.ts.net/", "abc").aktiv)


if __name__ == "__main__":
    unittest.main()
