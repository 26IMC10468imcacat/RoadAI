"""Crash Test für den Foto-Upload.  Ausführen mit:  python -m unittest -v"""
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("ANTHROPIC_API_KEY", None)

from PIL import Image  # noqa: E402

import app as roadai_app  # noqa: E402
import auswertung_server as aw  # noqa: E402

sys.path.insert(0, str(aw.PROGRAMM.parent))
import Lebende_Strasse as programm  # noqa: E402


def jpeg(farbe=(120, 120, 120), groesse=(800, 600), exif=None) -> bytes:
    b = io.BytesIO()
    im = Image.new("RGB", groesse, farbe)
    im.save(b, "JPEG", exif=exif or b"")
    return b.getvalue()


class FotoVorbereiten(unittest.TestCase):
    def test_gps_und_zeit_vom_handy_landen_im_foto(self):
        d = Path(aw.ARBEIT) / "test_vorbereiten"
        d.mkdir(parents=True, exist_ok=True)
        q, z = d / "q.jpg", d / "z.jpg"
        q.write_bytes(jpeg())
        meta = aw._meta_pruefen({"lat": 48.466911, "lon": 15.658644, "genauigkeit": 4.5,
                                 "zeit": "2026:10:01 08:10:44", "f35": 24, "modell": "iPhone 16 Pro Max"})
        info = aw.foto_vorbereiten(q, z, meta)
        self.assertTrue(info["gps"])
        # Das Auswerteprogramm liest dieselben Werte wieder heraus
        _, exif = programm.lade_foto(str(z))
        werte = programm.exif_daten(exif)
        self.assertAlmostEqual(werte["lat"], 48.466911, places=5)
        self.assertAlmostEqual(werte["lon"], 15.658644, places=5)
        self.assertEqual(werte["zeit"], "2026:10:01 08:10:44")
        self.assertEqual(werte["f35"], 24)
        self.assertAlmostEqual(werte["gps_genauigkeit"], 4.5)

    def test_grosse_fotos_werden_verkleinert(self):
        d = Path(aw.ARBEIT) / "test_vorbereiten"
        d.mkdir(parents=True, exist_ok=True)
        q, z = d / "gross.jpg", d / "gross_z.jpg"
        q.write_bytes(jpeg(groesse=(5712, 4284)))
        aw.foto_vorbereiten(q, z, {})
        self.assertEqual(max(Image.open(z).size), aw.MAX_KANTE)

    def test_unsinnige_metadaten_werden_ignoriert(self):
        for m in (None, "x", [], {"lat": "48", "lon": 15}, {"lat": 999, "lon": 0}, {"lat": True, "lon": False},
                  {"zeit": "gestern"}, {"f35": 99999}, {"lat": 0, "lon": 0}, {"modell": "<script>" * 20}):
            out = aw._meta_pruefen(m)
            self.assertNotIn("lat", out)
            self.assertNotIn("zeit", out)
            self.assertNotIn("f35", out)
            self.assertNotIn("<", out.get("modell", ""))


class Upload(unittest.TestCase):
    def setUp(self):
        roadai_app.app.testing = True
        self.c = roadai_app.app.test_client()
        roadai_app._zaehler.clear()

    def post(self, fotos, **felder):
        daten = {"fotos": [(io.BytesIO(b), n) for n, b in fotos], **felder}
        return self.c.post("/api/auswertung", data=daten, content_type="multipart/form-data")

    def test_zu_wenige_und_falsche_dateien(self):
        self.assertEqual(self.post([]).status_code, 422)
        self.assertEqual(self.post([("a.jpg", jpeg())]).status_code, 422)
        r = self.post([("a.jpg", b"kein bild"), ("b.jpg", jpeg())])
        self.assertEqual(r.status_code, 422)
        self.assertIn("kein lesbares Foto", r.get_json()["fehler"])
        r = self.post([("a.jpg", b""), ("b.jpg", jpeg())])
        self.assertEqual(r.status_code, 422)

    def test_zu_viele_fotos(self):
        r = self.post([(f"{i}.jpg", jpeg()) for i in range(aw.MAX_FOTOS + 1)])
        self.assertEqual(r.status_code, 422)

    def test_falsche_breite(self):
        for b in ("abc", "-3", "500"):
            r = self.post([("a.jpg", jpeg()), ("b.jpg", jpeg())], breite=b)
            self.assertEqual(r.status_code, 422, b)

    def test_boese_dateinamen_und_meta(self):
        r = self.post([("../../etc/passwd.jpg", jpeg()), ("<script>.heic", jpeg())], meta="kein json", name="<b>x</b>" * 30)
        self.assertEqual(r.status_code, 200)
        a = aw.auftrag(r.get_json()["auftrag"]["id"])
        namen = sorted(p.name for p in (a.ordner / "fotos" / "Strecke").glob("*")) if (a.ordner / "fotos").exists() else []
        self.assertTrue(all("/" not in n and ".." not in n for n in namen))
        self.assertLessEqual(len(a.name), 60)
        self._warten(a.id)

    def test_keine_strasse_auf_den_fotos(self):
        """Ein anderes Team lädt Selfies hoch: es gibt eine verständliche Meldung statt eines Absturzes."""
        r = self.post([("selfie1.jpg", jpeg((200, 150, 120))), ("selfie2.jpg", jpeg((30, 60, 200)))])
        self.assertEqual(r.status_code, 200)
        s = self._warten(r.get_json()["auftrag"]["id"])
        self.assertEqual(s["status"], "fehler")
        self.assertIn("Fahrbahn", s["fehler"])

    def test_dateien_nur_vom_eigenen_auftrag(self):
        for pfad in ("/api/auswertung/0000000000000000/Bericht.html", "/api/auswertung/../app.py",
                     "/api/auswertung/zzzz/bilder/x.jpg", "/api/auswertung/0123456789abcdef/bilder/../../app.py"):
            self.assertIn(self.c.get(pfad).status_code, (404, 308))
        self.assertIsNone(aw.datei_pfad("0123456789abcdef", "../app.py"))

    def _warten(self, aid, sekunden=120):
        ende = time.time() + sekunden
        while time.time() < ende:
            s = self.c.get(f"/api/auswertung/{aid}").get_json()["auftrag"]
            if s["status"] in ("fertig", "fehler"):
                return s
            time.sleep(0.5)
        self.fail("Auswertung hängt")


class Uebersetzung(unittest.TestCase):
    def test_echte_ergebnisdatei(self):
        roh = json.loads((Path(__file__).resolve().parents[1] / "data" / "teststrecke_02.json").read_text("utf-8"))
        import roadai_core as core
        s = core.pruefe_strecke(roh)
        self.assertEqual(s["kennzahlen"]["abschnitte"], 8)
        self.assertEqual(core.regel_route(s)[0], "beobachten")

    def test_fremde_bild_links_werden_entfernt(self):
        import roadai_core as core
        roh = json.loads((Path(__file__).resolve().parents[1] / "data" / "teststrecke_02.json").read_text("utf-8"))
        roh["abschnitte"][0]["bild"] = "javascript:alert(1)"
        roh["abschnitte"][1]["bild"] = "https://evil.example/x.jpg"
        roh["bericht"] = "https://evil.example/Bericht.html"
        s = core.pruefe_strecke(roh)
        self.assertIsNone(s["abschnitte"][0]["bild"])
        self.assertIsNone(s["abschnitte"][1]["bild"])
        self.assertIsNone(s["bericht"])


if __name__ == "__main__":
    unittest.main()
