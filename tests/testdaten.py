"""Künstliche Prüfstrecken nur für die automatischen Tests.

Sie werden beim Testen im Speicher erzeugt und erscheinen nie in der App.
"""

_BASIS = {
    "schema": "roadai.strecke.v1",
    "geraet": "Testdaten",
    "fahrbahnbreite_m": 5.8,
    "abschnittslaenge_m": 5.0,
    "methode": "Künstliche Testdaten",
    "simuliert": True,
    "ort": "Testort",
    "aufnahme": "2026-10-01T09:00:00+02:00",
}

_STRECKEN = {
    # Name: (Titel, Schadanteile je Abschnitt in %, Bildqualität je Abschnitt)
    "gut": ("Prüfstrecke gut", [4.2, 6.8, 3.1, 9.5, 5.0, 7.7, 2.9, 8.1], None),
    "mittel": ("Prüfstrecke mittel", [12.4, 22.8, 17.5, 26.1, 14.0, 19.9, 24.3, 11.2], None),
    "schwer": ("Prüfstrecke schwer", [18.2, 34.5, 27.9, 41.3, 22.0, 36.8, 15.4, 29.1], None),
    "schlechte_fotos": ("Prüfstrecke schlechte Fotos", [8.0, 0.0, 0.0, 41.0, 0.0, 12.0],
                        ["gut", "unbrauchbar", "unbrauchbar", "maessig", "unbrauchbar", "gut"]),
}


def pruefstrecke(name: str) -> dict:
    titel, anteile, qualitaet = _STRECKEN[name]
    abschnitte = []
    for i, p in enumerate(anteile):
        s = round(p / 100 * 29, 2)
        abschnitte.append({
            "nr": i + 1, "station_von_m": 4 + 5 * i, "station_bis_m": 9 + 5 * i,
            "laengsrisse_m": round(s * 1.4, 1), "querrisse_m": round(s * 0.2, 1),
            "netzrisse_m2": round(s * 0.3, 2), "flickstellen_m2": 0.0, "schaden_m2": s,
            "schadanteil_pct": p, "foto": f"TEST_{i + 1:02d}",
            "lat": round(48.4672 + 0.00002 * i, 6), "lon": round(15.6600 + 0.00006 * i, 6),
            "gps_genauigkeit_m": 5.0, "bildqualitaet": qualitaet[i] if qualitaet else "gut",
        })
    return dict(_BASIS, strecke_id=f"TEST_{name.upper()}", name=titel, abschnitte=abschnitte,
                grenzen=["Künstliche Testdaten."])
