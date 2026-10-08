# -*- coding: utf-8 -*-
r"""
RoadSense – Straßenschäden aus Smartphone-Fotos auswerten (Prototyp v1.2)
======================================================================

Ordnerstruktur (ein Ordner je Straße, der Ordnername ist der Straßenname):
    Fotos\<Straße>\           – die Fotos dieser Straße (alle ca. 5 m, Kamera nach vorne, Fahrbahnmitte)
    Ergebnisse\<Straße>\      – wird automatisch angelegt und enthält:
        • Bericht.html        – Prüfbericht mit Zustandsklassen, Lageplan, Vogelperspektive, Details
        • Karte.html          – Luftbildkarte (basemap.at) mit farbigen 5-m-Abschnitten
        • Schaeden.csv        – Werte je Abschnitt (für Excel / Google Sheets)
        • Abschnitte.geojson  – Abschnitte als Geodaten (QGIS, uMap, Web-Karten)
        • Zustandsklassen.kml – für Google My Maps / Google Earth
        • ergebnisse.json     – alle Rohwerte
        • bilder\             – Overlays und Draufsichten

Installation (einmalig, in der Eingabeaufforderung):
    py -m pip install numpy opencv-python pillow

Aufruf (im Terminal, im Ordner dieses Programms):
    py "Lebende_Strasse.py"
        → wertet jede Straße im Ordner "Fotos" aus, die noch kein Ergebnis hat (oder neue Fotos bekommen hat);
          Ergebnisse landen in "Ergebnisse\<Straße>"
    py "Lebende_Strasse.py" --alle
        → alle Straßen neu auswerten
    py "Lebende_Strasse.py" "Teststrecke 02" --ort "Langenlois"
        → nur diese eine Straße auswerten

Wichtige Optionen (alle optional):
    STRASSE            nur diese Straße auswerten (Unterordner von "Fotos" oder Pfad zu einem Fotoordner)
    --ausgabe ORDNER   anderer Oberordner für die Ergebnisse (Standard: "Ergebnisse" neben dem Programm)
    --titel TEXT       Titel des Berichts (Standard: Name der Straße)
    --breite 5.80      vor Ort gemessene Fahrbahnbreite in m (genaueste Variante);
                       ohne Angabe wird sie aus den Fotos bestimmt
    --hoehe 1.60       Höhe des Handys über der Fahrbahn in m (nur ohne --breite, Standard 1,60)
    --von 4 --bis 9    Auswertebereich vor der Kamera in m (Länge = Fotoabstand)
    --ohne NAME ...    Fotos überspringen (z. B. Kalibrierfoto)
    --webhook URL      Ergebnisse zusätzlich an eine URL senden (z. B. Zapier „Catch Hook“)
    --alle             auch Straßen neu auswerten, die schon ein aktuelles Ergebnis haben

Fotos: JPG oder HEIC. HEIC-Fotos werden automatisch in JPG umgewandelt (über Windows,
ohne Zusatzpakete); GPS und Aufnahmezeit werden dabei unverändert übernommen und geprüft.
Voraussetzung unter Windows: „HEIF-Bilderweiterungen“ + „HEVC-Videoerweiterungen“ (Microsoft Store).

Fachliche Regeln (Projektvorgabe):
    Längs-/Querriss = Länge × 0,50 m Einflussbreite · Netzriss = umschlossene Fläche ·
    Flickstelle = Fläche · Gesamtschaden ohne Doppelzählung (Vereinigungsfläche) ·
    Zustandsklasse 1–5 in 10-%-Stufen des Schadanteils (keine RVS-Note).
Kartengrundlage: basemap.at (CC BY 4.0) – Quellenangabe „Datenquelle: basemap.at“.
"""

import argparse, base64, csv, glob, html, json, math, os, shutil, struct, subprocess, sys, tempfile, datetime, urllib.request

import numpy as np
import cv2
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Eigene Bildfunktionen (ersetzen scipy/scikit-image, deren Programmteile die
# intelligente App-Steuerung von Windows blockiert) – nur numpy + OpenCV
# ---------------------------------------------------------------------------
def skeletonize(maske):
    """Skelett (1 Pixel breite Mittellinie) nach Zhang & Suen."""
    im = np.pad(maske.astype(np.uint8), 1)
    while True:
        geaendert = False
        for schritt in (0, 1):
            P2, P3, P4 = im[:-2, 1:-1], im[:-2, 2:], im[1:-1, 2:]
            P5, P6, P7 = im[2:, 2:], im[2:, 1:-1], im[2:, :-2]
            P8, P9, C = im[1:-1, :-2], im[:-2, :-2], im[1:-1, 1:-1]
            B = P2 + P3 + P4 + P5 + P6 + P7 + P8 + P9
            folge = [P2, P3, P4, P5, P6, P7, P8, P9, P2]
            A = sum(((folge[i] == 0) & (folge[i + 1] == 1)).astype(np.uint8) for i in range(8))
            m = (C == 1) & (B >= 2) & (B <= 6) & (A == 1)
            if schritt == 0:
                m &= ((P2 * P4 * P6) == 0) & ((P4 * P6 * P8) == 0)
            else:
                m &= ((P2 * P4 * P8) == 0) & ((P2 * P6 * P8) == 0)
            if m.any():
                C[m] = 0; geaendert = True
        if not geaendert:
            break
    return im[1:-1, 1:-1].astype(bool)


def binary_fill_holes(maske):
    """Löcher in einer Maske füllen (alles, was nicht vom Rand aus erreichbar ist)."""
    m = np.pad(maske.astype(np.uint8), 1)
    flut = m.copy(); h, w = m.shape
    cv2.floodFill(flut, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 1)
    return (m.astype(bool) | (flut == 0))[1:-1, 1:-1]


def apply_hysteresis_threshold(bild, niedrig, hoch):
    """Hysterese: zusammenhängende Bereiche über 'niedrig', die irgendwo 'hoch' erreichen."""
    lo = (bild > niedrig).astype(np.uint8); hi = bild > hoch
    n, lab = cv2.connectedComponents(lo, connectivity=8)
    ids = np.unique(lab[hi]); ids = ids[ids > 0]
    return np.isin(lab, ids)

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

VERSION = '1.2'
RES = 0.005            # 5 mm je Pixel in der Draufsicht
MARGIN = 0.50          # Rand links/rechts in der Draufsicht [m]
CRACK_W = 0.50         # Einflussbreite Längs-/Querriss [m]
EDGE_KEEP = 0.40       # Abstand zur Fahrbahnkante, in dem nicht ausgewertet wird (Randstein, Pflasterrinne) [m]
# Schwellen der Schadenserkennung, jeweils in Vielfachen der normalen Asphalt-Streuung (robust, MAD)
RISS_K = 5.0           # Risse: Linienantwort über dem Asphaltrauschen (Hysterese: 0,6 × bis 1 ×)
RISS_MIN = 0.40        # Risse: Mindestlänge [m]
FLICK_K = 3.0          # Flickstellen: so viel dunkler (geglättet) als die Fahrbahn
HELL_K = 6.0           # helle Markierungen/Randsteine: so viel heller als die Fahrbahn
DUNKEL_K = 12.0        # Gitterroste/Schachtdeckel: so viel dunkler als die Fahrbahn

KLASSE_FARBE = {1: '#1b7f3b', 2: '#7ccf4f', 3: '#f2d23a', 4: '#f08a24', 5: '#e0312b'}
KLASSE_TEXT = {1: '#ffffff', 2: '#13240c', 3: '#2b2400', 4: '#2b1400', 5: '#ffffff'}
KLASSE_NAME = {1: '0–10 %', 2: '10–20 %', 3: '20–30 %', 4: '30–40 %', 5: '40 % und mehr'}
COL = {'Längsriss': (40, 40, 230), 'Querriss': (0, 140, 255), 'Netzriss': (0, 0, 150)}   # BGR
PATCH = (230, 120, 30)
NETZ = (60, 30, 170)
SCHATTEN = (200, 200, 200)


def klasse(p):
    return 1 if p < 10 else 2 if p < 20 else 3 if p < 30 else 4 if p < 40 else 5


def de(x, n=1):
    return f"{x:,.{n}f}".replace(',', 'X').replace('.', ',').replace('X', '.')


# ---------------------------------------------------------------------------
# Dateien lesen/schreiben (funktioniert auch mit Umlauten im Pfad unter Windows)
# ---------------------------------------------------------------------------
def imwrite(path, img, q=85):
    ext = os.path.splitext(path)[1]
    ok, buf = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        raise IOError(f'Bild konnte nicht geschrieben werden: {path}')
    buf.tofile(path)


# ---------------------------------------------------------------------------
# HEIC → JPG (automatisch, GPS bleibt immer erhalten)
#   Bildpunkte: Windows-eigener Bildbaustein (WIC) über PowerShell – von Microsoft
#               signiert, daher nicht von der intelligenten App-Steuerung blockiert.
#   EXIF/GPS:   mit reinem Python aus der HEIC-Datei gelesen und unverändert ins JPG
#               geschrieben; danach wird geprüft, ob GPS im JPG angekommen ist.
# ---------------------------------------------------------------------------
def _heic_boxen(buf, s, e):
    i = s
    while i < e:
        size, typ = struct.unpack('>I4s', buf[i:i + 8]); hdr = 8
        if size == 1: size = struct.unpack('>Q', buf[i + 8:i + 16])[0]; hdr = 16
        elif size == 0: size = e - i
        if size < hdr: break
        yield typ.decode('latin1'), i + hdr, i + size
        i += size


def heic_exif(path):
    """Liest die EXIF-Daten (TIFF-Block) aus einer HEIC-Datei – ohne Zusatzpakete."""
    buf = open(path, 'rb').read()
    meta = next(((s, e) for t, s, e in _heic_boxen(buf, 0, len(buf)) if t == 'meta'), None)
    if meta is None:
        return None
    items, loc, idat = {}, {}, 0
    for t, s, e in _heic_boxen(buf, meta[0] + 4, meta[1]):
        if t == 'iinf':
            v = buf[s]; p = s + 4 + (2 if v == 0 else 4)
            for t2, s2, e2 in _heic_boxen(buf, p, e):
                v2 = buf[s2]; q = s2 + 4; w = 2 if v2 == 2 else 4
                if v2 < 2: continue
                iid = int.from_bytes(buf[q:q + w], 'big'); q += w + 2
                items[iid] = buf[q:q + 4].decode('latin1')
        elif t == 'iloc':
            v = buf[s]; p = s + 4; a, b = buf[p], buf[p + 1]; p += 2
            osz, lsz, bsz, isz = a >> 4, a & 15, b >> 4, (b & 15) if v in (1, 2) else 0
            w = 2 if v < 2 else 4; n = int.from_bytes(buf[p:p + w], 'big'); p += w
            rd = lambda p, k: (int.from_bytes(buf[p:p + k], 'big') if k else 0, p + k)
            for _ in range(n):
                iid, p = rd(p, w); cm = 0
                if v in (1, 2): cm = buf[p + 1] & 15; p += 2
                p += 2; base, p = rd(p, bsz); ec, p = rd(p, 2); ext = []
                for _ in range(ec):
                    if isz: _, p = rd(p, isz)
                    off, p = rd(p, osz); ln, p = rd(p, lsz); ext.append((base + off, ln))
                loc[iid] = (cm, ext)
        elif t == 'idat':
            idat = s
    eid = next((i for i, t in items.items() if t == 'Exif'), None)
    if eid is None or eid not in loc:
        return None
    cm, ext = loc[eid]; base = idat if cm == 1 else 0
    d = b''.join(buf[base + o:base + o + l] for o, l in ext)
    off = struct.unpack('>I', d[:4])[0]
    exif = Image.Exif(); exif.load(d[4 + off:])
    return exif


_WIC_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore
$fs = [IO.File]::OpenRead($env:ROADAI_IN)
try {
  $dec = [Windows.Media.Imaging.BitmapDecoder]::Create($fs, [Windows.Media.Imaging.BitmapCreateOptions]::None, [Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
  $enc = New-Object Windows.Media.Imaging.PngBitmapEncoder
  $enc.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($dec.Frames[0]))
  $os = [IO.File]::Create($env:ROADAI_OUT); $enc.Save($os); $os.Close()
} finally { $fs.Close() }
"""


def _heic_pixel(path):
    """Dekodiert die Bildpunkte einer HEIC-Datei zu einem PIL-Bild."""
    tmp = tempfile.mkdtemp(prefix='roadai_')
    ziel = os.path.join(tmp, 'bild.png')
    try:
        if os.name == 'nt':
            cmd = base64.b64encode(_WIC_PS.encode('utf-16-le')).decode('ascii')
            env = dict(os.environ, ROADAI_IN=os.path.abspath(path), ROADAI_OUT=ziel)
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', cmd],
                               env=env, capture_output=True, text=True, timeout=180)
            if r.returncode != 0 or not os.path.exists(ziel):
                raise RuntimeError('Windows kann diese HEIC-Datei nicht öffnen. Bitte im Microsoft Store die '
                                   '„HEIF-Bilderweiterungen“ und „HEVC-Videoerweiterungen“ installieren '
                                   '(oder am iPhone: Einstellungen → Kamera → Formate → „Maximale Kompatibilität“). '
                                   f'Details: {(r.stderr or r.stdout).strip()[:300]}')
        elif shutil.which('heif-convert'):
            subprocess.run(['heif-convert', path, ziel], check=True, capture_output=True)
        else:
            raise RuntimeError('HEIC-Umwandlung ist hier nur unter Windows (oder mit „heif-convert“) möglich.')
        im = Image.open(ziel); im.load()
        return im.convert('RGB')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hat_gps(exif):
    if exif is None:
        return False
    g = exif.get_ifd(0x8825)
    return 2 in g and 4 in g


def heic_zu_jpg(path, ziel):
    """HEIC → JPG mit unveränderten EXIF-Daten. Bricht ab, wenn GPS dabei verloren ginge."""
    exif = heic_exif(path)
    gps_vorher = _hat_gps(exif)
    im = _heic_pixel(path)
    if exif is not None:
        ori = exif.get(0x0112, 1)
        # hat Windows das Bild bereits gedreht (Hochformat-Fotos), Drehhinweis zurücksetzen
        if ori in (5, 6, 7, 8) and im.height > im.width:
            exif[0x0112] = 1
        elif ori in (2, 3, 4):
            exif[0x0112] = ori
    im.save(ziel, quality=95, subsampling=0, exif=exif.tobytes() if exif is not None else b'')
    gps_nachher = _hat_gps(Image.open(ziel).getexif())
    if gps_vorher and not gps_nachher:
        os.remove(ziel)
        raise RuntimeError('GPS-Daten wären verloren gegangen – JPG verworfen.')
    return gps_vorher


def heic_umwandeln(ordner):
    """Wandelt alle HEIC-Dateien im Ordner in JPG um (vorhandene JPGs werden nicht überschrieben)."""
    heic = sorted(p for p in glob.glob(os.path.join(ordner, '*')) if p.lower().endswith(('.heic', '.heif')))
    if not heic:
        return
    print(f'HEIC-Umwandlung: {len(heic)} Datei(en) …')
    for p in heic:
        ziel = os.path.splitext(p)[0] + '.jpg'
        name = os.path.basename(p)
        if any(os.path.exists(os.path.splitext(p)[0] + e) for e in ('.jpg', '.JPG', '.jpeg', '.JPEG')):
            print(f'  · {name}: JPG schon vorhanden'); continue
        try:
            gps = heic_zu_jpg(p, ziel)
            print(f'  · {name} → {os.path.basename(ziel)}' + ('  (GPS übernommen)' if gps else '  (Achtung: Foto hat keine GPS-Daten)'))
        except Exception as e:
            print(f'  ! {name}: {e}')


def lade_foto(path):
    """Liefert (BGR-Bild, EXIF-Daten) für JPG/PNG. HEIC wird bewusst nicht gelesen
    (das nötige Zusatzpaket wird von der intelligenten App-Steuerung in Windows blockiert)."""
    im = Image.open(path)
    exif = im.getexif()
    im = ImageOps.exif_transpose(im)
    rgb = np.asarray(im.convert('RGB'))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), exif


def exif_daten(exif):
    out = dict(zeit='', lat=None, lon=None, gps_genauigkeit=None, f35=None, modell='')
    if exif is None:
        return out
    ie = exif.get_ifd(0x8769); gps = exif.get_ifd(0x8825)
    out['zeit'] = str(ie.get(0x9003, exif.get(0x0132, '')) or '')
    out['modell'] = str(exif.get(0x0110, '') or '')
    out['f35'] = ie.get(0xA405)

    def dd(v, ref):
        d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        return -d if str(ref) in ('S', 'W') else d
    if 2 in gps and 4 in gps:
        out['lat'] = dd(gps[2], gps.get(1, 'N')); out['lon'] = dd(gps[4], gps.get(3, 'E'))
    if 31 in gps:
        out['gps_genauigkeit'] = float(gps[31])
    return out


# ---------------------------------------------------------------------------
# Fahrbahn und Schatten im Foto finden
#   Abgestimmt an handmarkierten Lernfotos (Fahrbahn zwischen den Randsteinen bzw. der
#   Randlinie, Schatten getrennt): Übereinstimmung in der unteren Bildhälfte 92–97 %.
# ---------------------------------------------------------------------------
SEG_W = 1600           # Arbeitsbreite für die Fahrbahnerkennung [px]


def fahrbahn_segmentieren(img):
    """img: BGR-Bild (ca. SEG_W breit), Kamera steht auf der Fahrbahn.
    Liefert (links, rechts, schatten): Fahrbahnkante je Bildzeile (-1 = keine) und Schattenmaske."""
    h, w = img.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(img, (0, 0), 2.0), cv2.COLOR_BGR2LAB).astype(np.float32)
    Lc, ac, bc = lab[..., 0], lab[..., 1] - 128, lab[..., 2] - 128
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mu = cv2.blur(gray, (9, 9))
    tex = cv2.GaussianBlur(np.sqrt(np.maximum(cv2.blur(gray * gray, (9, 9)) - mu * mu, 0)), (0, 0), 3)

    # Bezug: Asphalt unten in der Bildmitte (dort steht die Kamera). Liegt ein Teil davon im
    # Schatten (zwei deutlich getrennte Helligkeiten), gilt der sonnige Teil als Bezug.
    y0, y1, x0, x1 = int(h * .80), int(h * .97), int(w * .30), int(w * .70)
    Lb = Lc[y0:y1, x0:x1]
    t, _ = cv2.threshold(Lb.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    sonnig = Lb > t
    if not (sonnig.mean() > 0.15 and Lb[sonnig].mean() > 1.35 * max(Lb[~sonnig].mean(), 1)):
        sonnig = np.ones_like(Lb, bool)

    def ref(c):
        v = c[y0:y1, x0:x1][sonnig]; m = np.median(v)
        return m, 1.4826 * np.median(np.abs(v - m))
    (Lm, Ls), (am, as_), (bm, bs), (tm, ts) = ref(Lc), ref(ac), ref(bc), ref(tex)
    Ls, as_, bs, ts = max(Ls, 4), max(as_, 1.5), max(bs, 1.5), max(ts, 1.5)

    # Schatten: deutlich dunkler, Farbe leicht Richtung blau (Himmelslicht), kaum Sättigung und –
    # anders als Autolack oder Himmel – mit der Körnung von Asphalt (relativ zur Helligkeit)
    rel = tex / np.maximum(Lc, 1); rel_m = tm / max(Lm, 1)
    schatten = ((Lc < 0.72 * Lm) & (Lc > 0.12 * Lm) & (bc < bm - 2) & (bc > bm - 16) & (np.abs(ac - am) < 5)
                & (rel > 0.4 * rel_m) & (rel < 3.0 * rel_m))
    schatten = cv2.morphologyEx(schatten.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    schatten = cv2.morphologyEx(schatten, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)).astype(bool)
    # als Fahrbahn zählen nur größere Schattenflächen – nicht der schmale Schattenstreifen am Randstein
    schatten_fb = cv2.morphologyEx(schatten.astype(np.uint8), cv2.MORPH_OPEN, np.ones((13, 13), np.uint8)).astype(bool)

    # Zeilenweise von unten nach oben: zusammenhängender Asphalt um die Fahrbahnmitte.
    # Die Bezugshelligkeit wird mitgeführt, weil der Asphalt nach hinten heller/dunkler wirkt.
    links, rechts = np.full(h, -1), np.full(h, -1)
    cx, Lr = w // 2, Lm
    luecke = max(6, int(w * 0.012))      # Risse, Kanaldeckel u. Ä. überbrücken
    for y in range(h - 1, int(h * 0.3), -1):
        L_ = Lc[y]
        sonne = ((np.abs(L_ - Lr) < max(3 * Ls, 0.16 * Lr)) & (np.abs(ac[y] - am) < 3 * as_ + 1)
                 & (np.abs(bc[y] - bm) < 3 * bs + 2) & (tex[y] < tm + 5 * ts))
        hell = L_ > Lr + max(5 * Ls, 0.28 * Lr)          # Randstein, Markierung: harte Grenze
        row = (sonne | schatten_fb[y]) & ~hell
        row = cv2.morphologyEx(row.astype(np.uint8)[None], cv2.MORPH_CLOSE, np.ones((1, 9), np.uint8))[0].astype(bool)
        if not row[max(cx - 3, 0):cx + 4].any():
            nz = np.nonzero(row[max(cx - w // 20, 0):cx + w // 20])[0]
            if len(nz) == 0:
                if y < h * 0.6: break
                continue
            cx = max(cx - w // 20, 0) + nz[np.argmin(np.abs(nz - w // 20))]

        def walk(dx):
            x = last = cx; miss = 0
            while 0 < x < w - 1:
                x += dx
                if hell[x]: break
                if row[x]: last = x; miss = 0
                else:
                    miss += 1
                    if miss > luecke: break
            return last
        l, r = walk(-1), walk(1)
        links[y], rechts[y] = l, r
        cx = (l + r) // 2
        m = np.zeros(w, bool); m[l:r + 1] = True; m &= ~schatten[y]
        if m.sum() > 20:
            Lr = 0.9 * Lr + 0.1 * np.median(L_[m])

    # Kanten als glatte Kurve x = f(y) anpassen (RANSAC): Lecks in Gehsteig/Rinne und Lücken sind Ausreißer
    ys = np.nonzero(links >= 0)[0]
    rng = np.random.default_rng(0)
    for arr in (links, rechts):
        v = arr[ys].astype(float)
        ok = (v > 2) & (v < w - 3)                       # Bildrand ist keine Kante
        yy, vv = ys[ok].astype(float), v[ok]
        if len(yy) < 30:
            continue
        best, best_n, tol = None, -1, 0.008 * w
        for _ in range(300):
            i = rng.choice(len(yy), 3, replace=False)
            if len(set(yy[i])) < 3: continue
            c = np.polyfit(yy[i], vv[i], 2)
            n = int((np.abs(np.polyval(c, yy) - vv) < tol).sum())
            if n > best_n: best, best_n = c, n
        inl = np.abs(np.polyval(best, yy) - vv) < tol
        c = np.polyfit(yy[inl], vv[inl], 2)
        fit = np.polyval(c, ys.astype(float))
        arr[ys] = np.where(ys >= yy[inl].min(), np.clip(fit, 0, w - 1), arr[ys]).astype(int)
    bad = rechts <= links
    links[bad] = rechts[bad] = -1
    return links, rechts, schatten


# ---------------------------------------------------------------------------
# Geometrie: Fahrbahnkanten, Fluchtpunkt, Kamera, Entzerrung
# ---------------------------------------------------------------------------
class Foto:
    """Ein Foto mit Kameramodell: Höhe h über der Fahrbahn, Neigung theta (nach unten positiv),
    Blickrichtung psi gegenüber der Fahrbahnachse, Brennweite F in Pixeln."""

    def __init__(self, path, img, meta, args):
        self.path, self.img, self.meta, self.a = path, img, meta, args
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.H, self.W = img.shape[:2]
        self.cx, self.cy = self.W / 2, self.H / 2
        f35 = meta['f35'] or 26
        self.F = float(f35) / 43.267 * math.hypot(self.W, self.H)
        self.theta = self.psi = self.h = self.k = None
        self.kal_ok, self.kal, self.kal_grund = False, '', ''
        self.breite_mess, self.xc = None, 0.0

    def segmentieren(self):
        self.s = SEG_W / self.W
        klein = cv2.resize(self.img, (SEG_W, int(round(self.H * self.s))), interpolation=cv2.INTER_AREA)
        self.seg_l, self.seg_r, self.schatten_klein = fahrbahn_segmentieren(klein)
        if (self.seg_l >= 0).sum() < 30:
            raise ValueError('Fahrbahn nicht gefunden (kein Straßenbild oder Kamera nicht auf der Fahrbahn?)')

    # --- Kameramodell -------------------------------------------------------
    def bild(self, X, D):
        """Bodenpunkt (X quer nach rechts, D voraus, in m ab Kamera) → Bildpunkt (u, v)."""
        th, ps, h = self.theta, self.psi, self.h
        xr = X * math.cos(ps) - D * math.sin(ps)
        yf = X * math.sin(ps) + D * math.cos(ps)
        zc = yf * math.cos(th) + h * math.sin(th)
        yc = h * math.cos(th) - yf * math.sin(th)
        return self.cx + self.F * xr / zc, self.cy + self.F * yc / zc

    def zeile(self, D):
        return self.bild(0.0, D)[1]

    def _boden_zu_bild(self):
        pts = [(X, D) for X in (-3.0, 3.0) for D in (self.a.von, self.a.bis)]
        return cv2.getPerspectiveTransform(np.float32(pts), np.float32([self.bild(X, D) for X, D in pts]))

    # --- Kalibrierung aus den Fahrbahnkanten ----------------------------------
    def kalibrieren(self, h_vorl):
        """Neigung, Blickrichtung und Verhältnis Fahrbahnbreite/Kamerahöhe (k) aus den Kantengeraden
        im Nahbereich. h_vorl: vorläufige Kamerahöhe (nur für die Lage des Auswertebereichs im Bild)."""
        ys = np.nonzero(self.seg_l >= 0)[0]
        ys = ys[ys >= ys.min() + 0.3 * (ys.max() - ys.min())]
        for _ in range(3):
            def fit(arr):
                v = arr[ys]; ok = (v > 2) & (v < SEG_W - 3)        # Punkte am Bildrand sind keine Kante
                if ok.sum() < 10:
                    raise ValueError('Fahrbahnkante nicht im Bild')
                return np.polyfit(ys[ok] / self.s, v[ok] / self.s, 1)
            (al, bl), (ar, br) = fit(self.seg_l), fit(self.seg_r)
            if ar - al < 1e-4:
                raise ValueError('Fahrbahnkanten laufen nicht zusammen')
            yh = (br - bl) / (al - ar); xh = al * yh + bl
            self.theta = math.atan((self.cy - yh) / self.F)
            self.psi = -math.atan((xh - self.cx) * math.cos(self.theta) / self.F)
            self.k = (ar - al) / math.cos(self.theta)          # Breite / Höhe
            self.h = h_vorl
            # nur die Zeilen des Auswertebereichs (± 1 m) – dort ist die Straße fast gerade
            y0 = self.zeile(self.a.bis + 1) * self.s; y1 = self.zeile(max(self.a.von - 1, 1)) * self.s
            neu = np.nonzero(self.seg_l >= 0)[0]; neu = neu[(neu >= y0) & (neu <= y1)]
            if len(neu) < 20:
                break
            ys = neu
        self.d = dict(al=al, bl=bl, ar=ar, br=br, yh=yh, xh=xh)
        th = math.degrees(self.theta)
        if not -5 < th < 40:
            raise ValueError(f'Neigung {th:.0f}° unplausibel')
        if abs(math.degrees(self.psi)) > 25:
            raise ValueError(f'Blickrichtung {math.degrees(self.psi):.0f}° schräg zur Fahrbahn')
        if not 1.2 < self.k < 9:
            raise ValueError('Fahrbahnbreite/Kamerahöhe unplausibel')
        self.kal_ok = True

    def messen(self):
        """Fahrbahnbreite und -mitte im Auswertebereich aus der Segmentierung (Kalibrierung muss stehen)."""
        Hi = np.linalg.inv(self._boden_zu_bild())
        y0 = self.zeile(self.a.bis) * self.s; y1 = self.zeile(self.a.von) * self.s
        ys = np.nonzero(self.seg_l >= 0)[0]; ys = ys[(ys >= y0) & (ys <= y1)]
        ys = ys[(self.seg_l[ys] > 2) & (self.seg_r[ys] < SEG_W - 3)]
        if len(ys) < 5:
            return
        pts = np.float32([[(self.seg_l[y] / self.s, y / self.s), (self.seg_r[y] / self.s, y / self.s)] for y in ys]).reshape(-1, 1, 2)
        g = cv2.perspectiveTransform(pts, Hi).reshape(-1, 2, 2)
        self.breite_mess = float(np.median(g[:, 1, 0] - g[:, 0, 0]))
        self.xc = float(np.median((g[:, 1, 0] + g[:, 0, 0]) / 2))

    # --- Draufsicht -----------------------------------------------------------
    def entzerren(self, B):
        """Draufsicht des Auswertebereichs, B m breit (+ Rand), mittig auf der Fahrbahn.
        Fahrbahn = erkannte Fahrbahn (folgt auch Kurven). Liefert (top, road, Hm, schatten)."""
        w = int(round((B + 2 * MARGIN) / RES)); hgt = int(round((self.a.bis - self.a.von) / RES))
        src, dst = [], []
        for X in (self.xc - B / 2, self.xc + B / 2):
            for D in (self.a.von, self.a.bis):
                src.append(self.bild(X, D))
                dst.append(((X - self.xc + B / 2 + MARGIN) / RES, (self.a.bis - D) / RES))
        Hm = cv2.getPerspectiveTransform(np.float32(src), np.float32(dst))
        top = cv2.warpPerspective(self.img, Hm, (w, hgt), flags=cv2.INTER_AREA)
        hk, wk = self.schatten_klein.shape
        seg = np.zeros((hk, wk), np.uint8)
        for y in np.nonzero(self.seg_l >= 0)[0]:
            seg[y, self.seg_l[y]:self.seg_r[y] + 1] = 1
        Hk = Hm @ np.diag([1 / self.s, 1 / self.s, 1.0])          # Homographie für das verkleinerte Bild
        road = cv2.warpPerspective(seg, Hk, (w, hgt), flags=cv2.INTER_NEAREST).astype(bool)
        road &= top.sum(2) > 0
        schatten = cv2.warpPerspective(self.schatten_klein.astype(np.uint8), Hk, (w, hgt), flags=cv2.INTER_NEAREST).astype(bool) & road
        return top, road, Hm, schatten


def kalibrierung_abgleichen(fotos, args):
    """Kamerahöhe je Foto festlegen; Fotos mit unsicherer Kalibrierung übernehmen das Streckenmittel
    (das Handy wird beim Gehen ähnlich gehalten). Liefert die Breite der Draufsicht in m."""
    ok = [f for f in fotos if f.kal_ok]
    if not ok:
        raise ValueError('bei keinem Foto wurden beide Fahrbahnkanten sicher erkannt')
    hoehe = (lambda f: args.breite / f.k) if args.breite else (lambda f: args.hoehe)
    th_m = float(np.median([f.theta for f in ok])); ps_m = float(np.median([f.psi for f in ok]))
    h_m = float(np.median([hoehe(f) for f in ok]))
    for f in fotos:
        if f.kal_ok and abs(f.theta - th_m) < math.radians(8) and 0.8 < hoehe(f) / h_m < 1.25:
            f.h, f.kal = hoehe(f), 'eigen'
        else:
            if f.kal_ok:
                f.kal_grund = 'weicht stark vom Streckenmittel ab'
            f.theta, f.psi, f.h, f.kal = th_m, ps_m, h_m, 'Streckenmittel'
        f.messen()
    if args.breite:
        return args.breite
    b = [f.breite_mess for f in fotos if f.breite_mess]
    return round(float(np.median(b)), 1) if b else 6.0


# ---------------------------------------------------------------------------
# Schadenserkennung in der Draufsicht
# ---------------------------------------------------------------------------
def _prune(sk, n=8):
    sk = sk.copy().astype(np.uint8)
    for _ in range(n):
        k = cv2.filter2D(sk, -1, np.ones((3, 3), np.float32)); sk[(k == 2) & (sk == 1)] = 0
    return sk.astype(bool)


def _richtung(skc):
    ys, xs = np.nonzero(skc)
    cov = np.cov(np.vstack([xs, ys])) if len(xs) > 2 else np.eye(2)
    ev, evec = np.linalg.eigh(cov); vx, vy = evec[:, -1]
    return math.degrees(math.atan2(abs(vx), abs(vy))), math.sqrt(ev[-1] / max(ev[0], 1e-6))


def _robust(v):
    m = np.median(v)
    return m, 1.4826 * np.median(np.abs(v - m)) + 1e-3


def linienkarte(gray, mask, laenge=31, richtungen=16):
    """Risse sind lange, schmale, dunkle Linien. Ein einzelner Risspunkt ist nicht dunkler als die
    Lücken zwischen den Asphaltkörnern – erst entlang einer Linie gemittelt hebt sich der Riss ab,
    die zufällige Körnung mittelt sich weg. Liefert die stärkste Linienantwort je Pixel."""
    bg = cv2.GaussianBlur(gray, (0, 0), 6)
    d = np.maximum(bg - cv2.GaussianBlur(gray, (0, 0), 1.0), 0)      # Dunkelheit gegenüber Umgebung
    d[~mask] = 0
    best = np.zeros_like(d)
    c = laenge // 2
    for i in range(richtungen):
        a = math.pi * i / richtungen
        k = np.zeros((laenge, laenge), np.float32)
        for s in np.linspace(-c, c, 4 * laenge):
            k[int(round(c + s * math.sin(a))), int(round(c + s * math.cos(a)))] = 1
        np.maximum(best, cv2.filter2D(d, -1, k / k.sum()), out=best)
    return best


def erkennen(top, road, schatten):
    """Liefert (Flickstellen-Maske, Risse, ausgewertete Fläche)."""
    ell = lambda d: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))
    # Schatten (mit Saum) nicht auswerten: Schattenkanten sehen wie Risse aus, Schattenflächen wie Flicken
    gueltig = road & ~cv2.dilate(schatten.astype(np.uint8), ell(21)).astype(bool)
    if gueltig.sum() * RES ** 2 < 0.5:
        return np.zeros_like(road), [], gueltig
    gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bg = cv2.GaussianBlur(cv2.medianBlur(gray.astype(np.uint8), 5), (0, 0), 200)
    norm = gray - bg
    med, mad = _robust(norm[gueltig])
    # Randstreifen (Randstein, Rinne) und helle Markierungen nicht auswerten
    kern = int(EDGE_KEEP / RES) * 2 + 1
    inner = cv2.erode(gueltig.astype(np.uint8), np.ones((1, kern), np.uint8)).astype(bool)
    hell = cv2.dilate((cv2.GaussianBlur(norm, (0, 0), 2) > med + HELL_K * mad).astype(np.uint8), ell(15)).astype(bool)
    n, lab, st, _ = cv2.connectedComponentsWithStats(hell.astype(np.uint8))
    gross = st[:, cv2.CC_STAT_AREA] * RES ** 2 >= 0.02; gross[0] = False      # nur zusammenhängende Markierungen
    inner &= ~gross[lab]
    # sehr dunkle, kompakte Einbauten (Gitterrost, Schachtdeckel) nicht als Riss werten
    dunkel = (cv2.GaussianBlur(norm, (0, 0), 3) < med - DUNKEL_K * mad).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(dunkel)
    fl = st[:, cv2.CC_STAT_AREA] * RES ** 2
    kurz = np.minimum(st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT]) * RES
    fuell = st[:, cv2.CC_STAT_AREA] / np.maximum(st[:, cv2.CC_STAT_WIDTH] * st[:, cv2.CC_STAT_HEIGHT], 1)
    einbau = (fl >= 0.01) & (fl <= 1.0) & (kurz >= 0.10) & (fuell > 0.4); einbau[0] = False   # kompakt, nicht linienförmig
    inner &= ~cv2.dilate(einbau[lab].astype(np.uint8), ell(31)).astype(bool)

    # Flickstellen: großflächig dunkler, glatt, mit Abstand zur Fahrbahnkante
    smooth = cv2.GaussianBlur(norm, (0, 0), 6)
    tex = cv2.GaussianBlur(np.abs(norm - cv2.GaussianBlur(norm, (0, 0), 3)), (0, 0), 8)
    tmed = np.median(tex[inner]) if inner.any() else 1.0
    patch = (smooth < med - FLICK_K * mad) & (tex < 1.3 * tmed) & inner
    patch = cv2.morphologyEx(patch.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    patch = cv2.morphologyEx(patch, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(patch)
    keep = st[:, cv2.CC_STAT_AREA] * RES ** 2 >= 0.04; keep[0] = False
    pmask = keep[lab]

    # Risse: gerichteter Linienfilter, Hysterese mit absoluter Schwelle über dem Rauschen der
    # Asphaltstruktur (nicht „die dunkelsten x %“ – sonst findet sich auf jeder Fläche etwas)
    risse = []
    if not inner.any():
        return pmask, risse, gueltig
    R = linienkarte(gray, gueltig)
    rm, rs = _robust(R[inner])
    cand = apply_hysteresis_threshold(np.where(inner, R, 0), rm + 0.6 * RISS_K * rs, rm + RISS_K * rs)
    cand &= ~cv2.dilate(pmask.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    cand = cv2.morphologyEx(cand.astype(np.uint8), cv2.MORPH_CLOSE, ell(5))
    n, lab, st, _ = cv2.connectedComponentsWithStats(cand)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) * RES < RISS_MIN * 0.6: continue
        sl = (slice(y, y + h), slice(x, x + w)); comp = lab[sl] == i
        skc = _prune(skeletonize(comp), 20)
        if skc.sum() < 3: continue
        # Länge entlang der Hauptrichtung (das Skelett einer breiten Markierung hat viele Seitenäste)
        pts = np.column_stack(np.nonzero(skc)).astype(float); pts -= pts.mean(0)
        proj = pts @ np.linalg.svd(pts, full_matrices=False)[2][0]
        L_skelett = skc.sum() * RES * 1.12
        L = min(L_skelett, 1.15 * (proj.max() - proj.min() + 1) * RES)
        if L < RISS_MIN or a * RES ** 2 / L_skelett > 0.08: continue   # zu kurz oder zu breit (Fleck)
        ang, elong = _richtung(skc)
        k = cv2.filter2D(skc.astype(np.uint8), -1, np.ones((3, 3), np.float32))
        verzw = int(((k >= 4) & skc).sum())
        # Netzriss: flächiges, stark verzweigtes Rissmuster (auch wenn das Feld lang und schmal ist)
        verzweigt = L_skelett > 3 * L / 1.15 and verzw >= 6
        typ = ('Netzriss' if (w * RES > 0.3 and h * RES > 0.3 and (verzweigt or (verzw >= 6 and elong < 4)))
               else ('Längsriss' if ang <= 45 else 'Querriss'))
        sk = np.zeros_like(road); sk[sl] = skc
        c = dict(typ=typ, laenge=L, sk=sk)
        if typ == 'Netzriss':
            pad = 40
            y0, x0 = max(y - pad, 0), max(x - pad, 0)
            y1, x1 = min(y + h + pad, road.shape[0]), min(x + w + pad, road.shape[1])
            lc = (lab[y0:y1, x0:x1] == i).astype(np.uint8)
            lc = cv2.morphologyEx(lc, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
            area = np.zeros_like(road); area[y0:y1, x0:x1] = binary_fill_holes(lc); area &= gueltig
            c['flaeche_mask'] = area
        risse.append(c)
    return pmask, risse, gueltig


def auswerten(foto, args, B):
    top, road_ges, Hm, schatten = foto.entzerren(B)
    pmask, risse, road = erkennen(top, road_ges, schatten)
    einzel = [c for c in risse if c['typ'] != 'Netzriss']; netz = [c for c in risse if c['typ'] == 'Netzriss']
    sk = np.zeros_like(road)
    for c in einzel: sk |= c['sk']
    r = int(round(CRACK_W / 2 / RES))
    buf = cv2.dilate(sk.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))).astype(bool) & road
    nmask = np.zeros_like(road)
    for c in netz: nmask |= c['flaeche_mask']
    union = (buf | nmask | pmask) & road
    # Bezugsfläche = tatsächlich ausgewertete Fahrbahn (ohne Schatten, folgt Kurven)
    A = max(road.sum() * RES ** 2, 1e-6)
    lr = sum(c['laenge'] for c in einzel if c['typ'] == 'Längsriss'); qr = sum(c['laenge'] for c in einzel if c['typ'] == 'Querriss')
    res = dict(datei=foto.name, zeit=foto.meta['zeit'], lat=foto.meta['lat'], lon=foto.meta['lon'],
               gps_genauigkeit=foto.meta['gps_genauigkeit'],
               fahrbahnbreite=foto.breite_mess if foto.breite_mess else B, bezugsflaeche=A,
               laengsriss=lr, querriss=qr, einzelriss_laenge=lr + qr, einzelriss_flaeche_regel=(lr + qr) * CRACK_W,
               einzelriss_flaeche=buf.sum() * RES ** 2, netzriss_flaeche=nmask.sum() * RES ** 2,
               flickflaeche=pmask.sum() * RES ** 2, schlagloecher=None,
               schattenflaeche=(schatten & road_ges).sum() * RES ** 2,
               schadflaeche=union.sum() * RES ** 2, schadanteil=union.sum() * RES ** 2 / A * 100,
               kamerahoehe=foto.h, neigung=math.degrees(foto.theta), kalibrierung=foto.kal)
    res['klasse'] = klasse(res['schadanteil'])
    return res, top, road, pmask, risse, buf, nmask, Hm, schatten


def _schraffur(shape, abstand=16):
    yy, xx = np.indices(shape)
    return ((xx + yy) % abstand) < 3


def overlay_draufsicht(top, road, pmask, risse, buf, nmask, schatten):
    out = top.copy(); out[~road] = (out[~road] * 0.35).astype(np.uint8)
    out[schatten & _schraffur(schatten.shape)] = SCHATTEN      # Schatten: nicht ausgewertet
    tint = out.copy(); tint[buf] = (0.55 * tint[buf] + 0.45 * np.array((80, 80, 255))).astype(np.uint8)
    out = cv2.addWeighted(out, 0.6, tint, 0.4, 0)
    nm = out.copy(); nm[nmask] = NETZ; out = cv2.addWeighted(out, 0.6, nm, 0.4, 0)
    cnt, _ = cv2.findContours(nmask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(out, cnt, -1, NETZ, 4)
    pm = out.copy(); pm[pmask] = PATCH; out = cv2.addWeighted(out, 0.5, pm, 0.5, 0)
    for c in risse:
        out[cv2.dilate(c['sk'].astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)] = COL[c['typ']]
    return out


def overlay_foto(img, Hm, top, road, pmask, risse, buf, nmask, schatten):
    Hi = np.linalg.inv(Hm); hh, ww = img.shape[:2]
    lay = np.zeros_like(top); a = np.zeros(road.shape, np.uint8)
    lay[road] = (180, 60, 160); a[road] = 45
    s = schatten & _schraffur(schatten.shape, 24); lay[s] = SCHATTEN; a[s] = 200
    lay[buf] = (80, 80, 255); a[buf] = 70
    lay[nmask] = NETZ; a[nmask] = 110
    lay[pmask] = PATCH; a[pmask] = 170
    for c in risse:
        k = cv2.dilate(c['sk'].astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool); lay[k] = COL[c['typ']]; a[k] = 255
    L = cv2.warpPerspective(lay, Hi, (ww, hh)); A = cv2.warpPerspective(a, Hi, (ww, hh)).astype(np.float32)[..., None] / 255
    return (img * (1 - A) + L * A).astype(np.uint8)


# ---------------------------------------------------------------------------
# Verortung: Fahrbahnachse aus GPS (auch für gekrümmte Straßen)
# ---------------------------------------------------------------------------
class Achse:
    def __init__(self, punkte):
        lat0, lon0 = punkte[0]
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111320 * math.cos(math.radians(lat0)); self.ky = 111132.954
        P = np.array([self.xy(la, lo) for la, lo in punkte])
        # leicht glätten (GPS-Rauschen), dann als Linienzug mit Laufmeter verwenden
        if len(P) >= 5:
            Q = P.copy()
            for i in range(1, len(P) - 1):
                Q[i] = (P[i - 1] + 2 * P[i] + P[i + 1]) / 4
            P = Q
        self.P = P
        seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
        self.s = np.concatenate([[0], np.cumsum(seg)])
        self.abw = None

    def xy(self, la, lo):
        return np.array([(lo - self.lon0) * self.kx, (la - self.lat0) * self.ky])

    def ll(self, p):
        return self.lat0 + p[1] / self.ky, self.lon0 + p[0] / self.kx

    def punkt(self, s):
        """Punkt und Richtung bei Laufmeter s (vor/nach dem Linienzug gerade verlängert)."""
        P, S = self.P, self.s
        if len(P) == 1:
            return P[0], np.array([1.0, 0.0])
        i = int(np.clip(np.searchsorted(S, s) - 1, 0, len(P) - 2))
        d = P[i + 1] - P[i]; L = np.linalg.norm(d) or 1.0; d = d / L
        return P[i] + d * (s - S[i]), d

    def streifen(self, s0, s1, breite, n=6):
        links, rechts = [], []
        for s in np.linspace(s0, s1, n):
            p, d = self.punkt(s); nrm = np.array([-d[1], d[0]])
            links.append(p + nrm * breite / 2); rechts.append(p - nrm * breite / 2)
        return links + rechts[::-1]


# ---------------------------------------------------------------------------
# Ausgaben
# ---------------------------------------------------------------------------
def maps_link(r):
    return f"https://www.google.com/maps?q={r['lat']:.6f},{r['lon']:.6f}" if r['lat'] is not None else ''


def svg_lageplan(ax, S, breite):
    sc = 17.0
    polys = [(r, ax.streifen(r['von'], r['bis'], breite)) for r in S]
    s_end = S[-1]['bis'] + 10
    ctx = ax.streifen(-10, s_end, breite, n=max(6, int(s_end / 2)))
    allp = np.vstack([p for _, ps in polys for p in ps] + ctx)
    pad = 60
    minx, maxx = allp[:, 0].min(), allp[:, 0].max(); miny, maxy = allp[:, 1].min(), allp[:, 1].max()
    W = (maxx - minx) * sc + 2 * pad; H = (maxy - miny) * sc + 2 * pad + 70
    X = lambda p: (p[0] - minx) * sc + pad
    Y = lambda p: (maxy - p[1]) * sc + pad
    pp = lambda ps: ' '.join(f"{X(p):.1f},{Y(p):.1f}" for p in ps)
    out = [f'<svg viewBox="0 0 {W:.0f} {H:.0f}" role="img" class="plan" aria-label="Lageplan mit Zustandsklassen je Abschnitt, Norden oben">',
           '<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="8" stroke="var(--muted)" stroke-width="1.2" opacity=".5"/></pattern></defs>',
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="var(--paper)"/>',
           f'<polygon points="{pp(ctx)}" fill="var(--bg)" stroke="var(--line)" stroke-width="1.5" stroke-dasharray="6 5"/>',
           f'<polygon points="{pp(ax.streifen(0, S[0]["von"], breite))}" fill="url(#hatch)" stroke="var(--line)"/>']
    labels = []
    for r, ps in polys:
        k = r['klasse']; c = np.mean(ps, axis=0); cx, cy = X(c), Y(c)
        out.append(f'<a href="#a{r["nr"]:02d}"><polygon points="{pp(ps)}" fill="{KLASSE_FARBE[k]}" stroke="var(--paper)" stroke-width="2">'
                   f'<title>Abschnitt {r["nr"]:02d} · Station {r["von"]:g}–{r["bis"]:g} m · {de(r["schadanteil"])} % · Klasse {k}</title></polygon></a>')
        st = f'fill="{KLASSE_TEXT[k]}" stroke="{KLASSE_FARBE[k]}" stroke-width="4" paint-order="stroke" text-anchor="middle" font-family="IBM Plex Mono, monospace"'
        labels.append(f'<a href="#a{r["nr"]:02d}"><text x="{cx:.1f}" y="{cy - 3:.1f}" font-size="15" font-weight="600" {st}>{r["nr"]:02d}</text>'
                      f'<text x="{cx:.1f}" y="{cy + 14:.1f}" font-size="11" {st}>{de(r["schadanteil"])} %</text></a>')
    for r in S:
        if r['lat'] is not None:
            p = ax.xy(r['lat'], r['lon'])
            out.append(f'<circle cx="{X(p):.1f}" cy="{Y(p):.1f}" r="3.5" fill="var(--ink)" stroke="var(--paper)" stroke-width="1.2"><title>{r["datei"]} (GPS-Standort)</title></circle>')
    out += labels
    for s in [0] + [r['von'] for r in S[::2]] + [S[-1]['bis']]:
        p, d = ax.punkt(s); p = p - np.array([-d[1], d[0]]) * (breite / 2 + 1.2)
        out.append(f'<text x="{X(p):.1f}" y="{Y(p) + 4:.1f}" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="11" fill="var(--muted)">{s:g} m</text>')
    nx, ny = W - 40, 40
    out.append(f'<path d="M{nx} {ny - 22} L{nx + 9} {ny + 6} L{nx} {ny} L{nx - 9} {ny + 6} Z" fill="var(--ink)"/>'
               f'<text x="{nx}" y="{ny + 22}" text-anchor="middle" font-family="IBM Plex Sans, sans-serif" font-size="12" font-weight="600" fill="var(--ink)">N</text>')
    bx, by = pad, H - 30
    out.append(f'<g font-family="IBM Plex Mono, monospace" font-size="11" fill="var(--muted)">'
               f'<rect x="{bx}" y="{by}" width="{5 * sc:.1f}" height="6" fill="var(--ink)"/><rect x="{bx + 5 * sc:.1f}" y="{by}" width="{5 * sc:.1f}" height="6" fill="var(--paper)" stroke="var(--ink)"/>'
               f'<text x="{bx}" y="{by - 6}">0</text><text x="{bx + 5 * sc:.1f}" y="{by - 6}" text-anchor="middle">5</text><text x="{bx + 10 * sc:.1f}" y="{by - 6}" text-anchor="middle">10 m</text>'
               f'<circle cx="{bx + 10 * sc + 40:.1f}" cy="{by + 3}" r="4" fill="var(--ink)"/><text x="{bx + 10 * sc + 50:.1f}" y="{by + 7}">Fotostandort (GPS)</text></g></svg>')
    return '\n'.join(out)


def geojson(ax, S, breite, titel):
    feats = []
    for r in S:
        ps = ax.streifen(r['von'], r['bis'], breite); ps = ps + [ps[0]]
        ring = [[round(ax.ll(p)[1], 7), round(ax.ll(p)[0], 7)] for p in ps]
        feats.append({'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
                      'properties': {'art': 'abschnitt', 'abschnitt': f"{r['nr']:02d}", 'station_von_m': r['von'], 'station_bis_m': r['bis'],
                                     'foto': r['datei'], 'aufnahme': r['zeit'], 'zustandsklasse': r['klasse'],
                                     'schadanteil_prozent': round(r['schadanteil'], 1), 'schadflaeche_m2': round(r['schadflaeche'], 2),
                                     'laengsrisse_m': round(r['laengsriss'], 1), 'querrisse_m': round(r['querriss'], 1),
                                     'netzrisse_m2': round(r['netzriss_flaeche'], 2), 'flickstellen_m2': round(r['flickflaeche'], 2),
                                     'schatten_m2': round(r.get('schattenflaeche', 0), 2),
                                     'bezugsflaeche_m2': r['bezugsflaeche']}})
    for r in S:
        if r['lat'] is not None:
            feats.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [round(r['lon'], 7), round(r['lat'], 7)]},
                          'properties': {'art': 'foto', 'foto': r['datei']}})
    return {'type': 'FeatureCollection', 'name': titel, 'features': feats}


def kml(ax, S, breite, titel):
    k = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
         f'<name>{html.escape(titel)} – Zustandsklassen</name>',
         '<description>RoadSense. Zustandsklasse nach Schadanteil: 1 = 0–10 %, 2 = 10–20 %, 3 = 20–30 %, 4 = 30–40 %, 5 = ab 40 %. Keine Bewertung nach RVS.</description>']
    for c in range(1, 6):
        h = KLASSE_FARBE[c]; bgr = h[5:7] + h[3:5] + h[1:3]
        k.append(f'<Style id="k{c}"><LineStyle><color>ff{bgr}</color><width>1</width></LineStyle><PolyStyle><color>d0{bgr}</color></PolyStyle></Style>')
    k.append('<Folder><name>Abschnitte</name>')
    for r in S:
        ps = ax.streifen(r['von'], r['bis'], breite); ps = ps + [ps[0]]
        coords = ' '.join(f"{ax.ll(p)[1]:.7f},{ax.ll(p)[0]:.7f},0" for p in ps)
        k.append(f'<Placemark><name>Abschnitt {r["nr"]:02d} · Klasse {r["klasse"]}</name><styleUrl>#k{r["klasse"]}</styleUrl>'
                 f'<description>Station {r["von"]:g}–{r["bis"]:g} m, Foto {r["datei"]}. Schadanteil {de(r["schadanteil"])} %, Schadfläche {de(r["schadflaeche"], 2)} m².</description>'
                 f'<ExtendedData><Data name="Zustandsklasse"><value>{r["klasse"]}</value></Data><Data name="Schadanteil_Prozent"><value>{r["schadanteil"]:.1f}</value></Data></ExtendedData>'
                 f'<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>')
    k.append('</Folder></Document></kml>')
    return '\n'.join(k)


KARTE_HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITEL__ – Karte</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
*{box-sizing:border-box} html,body{height:100%;margin:0}
body{font-family:"Segoe UI","Helvetica Neue",Arial,sans-serif;color:#1d2226;background:#e9ecea;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px;padding:12px 16px;background:#fff;border-bottom:1px solid #d9ddd9}
header h1{font-size:1.15rem;margin:0} header span{font-size:.85rem;color:#5d666d}
#map{flex:1;min-height:420px}
.legend{background:#fff;padding:10px 12px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.25);font-size:12.5px;line-height:1.4;min-width:170px}
.legend h3{margin:0 0 6px;font-size:13px} .legend div{display:flex;align-items:center;gap:8px;margin:3px 0}
.legend i{width:16px;height:12px;border-radius:2px;display:inline-block;border:1px solid rgba(0,0,0,.25)}
.legend small{display:block;color:#5d666d;margin-top:6px}
.popup b{font-size:13px} .popup table{border-collapse:collapse;margin-top:6px;font-size:12px}
.popup figure{margin:8px 0 0} .popup img{display:block;width:300px;max-width:100%;height:auto;border:1px solid #d9ddd9;background:#e9ecea}
.popup figcaption{font-size:11px;color:#5d666d;margin-top:3px;line-height:1.3}
.vorschau{background:#fff;padding:10px 12px;border-radius:4px;box-shadow:0 1px 6px rgba(0,0,0,.3);width:min(326px,70vw);pointer-events:none}
.vorschau[hidden]{display:none}
.popup figure img{cursor:zoom-in}
.gross{position:fixed;inset:0;z-index:2000;background:rgba(12,14,16,.94);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:56px 16px 16px;gap:10px;cursor:zoom-out}
.gross[hidden]{display:none}
.gross img{max-width:100%;max-height:calc(100% - 40px);object-fit:contain;background:#222;border-radius:4px}
.gross p{color:#e6e9ea;font-size:13px;margin:0;text-align:center}
.gross button{position:absolute;top:12px;right:12px;font:inherit;font-size:14px;background:#2b3237;color:#eef0f1;border:1px solid #4a535a;border-radius:6px;padding:7px 12px;cursor:pointer}
.popup td{padding:2px 8px 2px 0} .popup td:last-child{text-align:right;font-family:Consolas,monospace}
.badge{display:inline-block;padding:1px 7px;border-radius:3px;font-family:Consolas,monospace;font-size:12px}
.seglabel{background:transparent;border:0;box-shadow:none;font-family:Consolas,monospace;font-weight:700;font-size:12px;color:#111;text-shadow:0 0 3px #fff,0 0 3px #fff}
.seglabel::before{display:none}
</style></head><body>
<header><h1 id="titel"></h1><span id="info"></span></header>
<div id="map"></div>
<script>
var DATEN = __GEOJSON__;
var FARBE={1:'#1b7f3b',2:'#7ccf4f',3:'#f2d23a',4:'#f08a24',5:'#e0312b'}, TEXT={1:'#fff',2:'#13240c',3:'#2b2400',4:'#2b1400',5:'#fff'};
var NAME={1:'0–10 %',2:'10–20 %',3:'20–30 %',4:'30–40 %',5:'40 % und mehr'};
var QUELLE='Datenquelle: <a href="https://basemap.at" target="_blank" rel="noopener">basemap.at</a>';
function de(x,n){return Number(x).toLocaleString('de-AT',{minimumFractionDigits:n,maximumFractionDigits:n});}
var basis='https://mapsneu.wien.gv.at/basemap/', opt={maxNativeZoom:20,maxZoom:22,attribution:QUELLE};
var orthofoto=L.tileLayer(basis+'bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg',opt),
    grundkarte=L.tileLayer(basis+'geolandbasemap/normal/google3857/{z}/{y}/{x}.png',opt),
    grau=L.tileLayer(basis+'bmapgrau/normal/google3857/{z}/{y}/{x}.png',opt),
    beschriftung=L.tileLayer(basis+'bmapoverlay/normal/google3857/{z}/{y}/{x}.png',opt);
var map=L.map('map',{layers:[orthofoto]}); L.control.scale({imperial:false,maxWidth:160}).addTo(map);
var vorschau=L.control({position:'bottomright'});
vorschau.onAdd=function(){this.div=L.DomUtil.create('div','vorschau'); this.div.hidden=true; return this.div;};
vorschau.zeige=function(h){this.div.innerHTML=h; this.div.hidden=false;};
vorschau.verberge=function(){this.div.hidden=true;};
vorschau.addTo(map);
var abschnitte=L.geoJSON(DATEN,{filter:function(f){return f.properties.art==='abschnitt';},
  style:function(f){return {color:'#fff',weight:1.5,fillColor:FARBE[f.properties.zustandsklasse],fillOpacity:0.72};},
  onEachFeature:function(f,layer){var p=f.properties,k=p.zustandsklasse;
    // Draufsicht der markierten Schäden + Werte: beim Darüberfahren im Vorschaufeld, beim Klick als Popup
    var inhalt='<div class="popup"><b>Abschnitt '+p.abschnitt+' · Station '+p.station_von_m+'–'+p.station_bis_m+' m</b><br>'+
      '<span class="badge" style="background:'+FARBE[k]+';color:'+TEXT[k]+'">Klasse '+k+' · '+de(p.schadanteil_prozent,1)+' %</span>'+
      '<figure><img src="bilder/'+encodeURIComponent(p.foto)+'_draufsicht_klein.jpg" alt="Draufsicht mit markierten Schäden, '+p.foto+'">'+
      '<figcaption>Draufsicht · Fahrtrichtung ↑ · rot Längs-, orange Querriss, dunkelrot Netzriss, blau Flickstelle, grau schraffiert Schatten<br><b>Bild antippen zum Vergrößern</b></figcaption></figure><table>'+
      '<tr><td>Längsrisse</td><td>'+de(p.laengsrisse_m,1)+' m</td></tr><tr><td>Querrisse</td><td>'+de(p.querrisse_m,1)+' m</td></tr>'+
      '<tr><td>Netzrisse</td><td>'+de(p.netzrisse_m2,2)+' m²</td></tr><tr><td>Flickstellen</td><td>'+de(p.flickstellen_m2,2)+' m²</td></tr>'+
      '<tr><td>Schadfläche</td><td>'+de(p.schadflaeche_m2,2)+' m²</td></tr>'+
      (p.schatten_m2 ? '<tr><td>Schatten (nicht ausgewertet)</td><td>'+de(p.schatten_m2,2)+' m²</td></tr>' : '')+
      '<tr><td>Foto</td><td>'+p.foto+'</td></tr></table></div>';
    layer.bindPopup(inhalt,{maxWidth:340,minWidth:300});
    layer.bindTooltip(p.abschnitt,{permanent:true,direction:'center',className:'seglabel'});
    layer.on('mouseover',function(){layer.setStyle({weight:3,color:'#111'}); vorschau.zeige(inhalt);});
    layer.on('mouseout',function(){abschnitte.resetStyle(layer); vorschau.verberge();});}}).addTo(map);
var fotos=L.geoJSON(DATEN,{filter:function(f){return f.properties.art==='foto';},
  pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:4,color:'#fff',weight:1.5,fillColor:'#1d2226',fillOpacity:1});},
  onEachFeature:function(f,l){l.bindTooltip(f.properties.foto+' (GPS-Standort)');}});
L.control.layers({'Luftbild (Orthofoto)':orthofoto,'Grundkarte':grundkarte,'Grundkarte grau':grau},
  {'Zustandsklassen':abschnitte,'Fotostandorte':fotos,'Straßennamen':beschriftung},{collapsed:false}).addTo(map);
var leg=L.control({position:'bottomleft'}); leg.onAdd=function(){var d=L.DomUtil.create('div','legend'),h='<h3>Zustandsklasse</h3>',a={};
  DATEN.features.forEach(function(f){var k=f.properties.zustandsklasse; if(k) a[k]=(a[k]||0)+1;});
  for(var k=1;k<=5;k++) h+='<div><i style="background:'+FARBE[k]+'"></i>'+k+' · '+NAME[k]+'<span style="margin-left:auto">'+(a[k]||0)+'</span></div>';
  d.innerHTML=h+'<small>Schadanteil je Abschnitt, Projektvorgabe (keine RVS-Note)</small>'; return d;}; leg.addTo(map);
document.getElementById('titel').textContent=DATEN.name;
var n=DATEN.features.filter(function(f){return f.properties.art==='abschnitt';}).length;
document.getElementById('info').textContent=n+' Abschnitte · Luftbild 29 cm (Detailgebiete 15 cm) · Datenquelle: basemap.at';
// Vogelperspektive im Infofenster antippen: groß anzeigen (volle Auflösung, falls vorhanden)
var gross=document.createElement('div'); gross.className='gross'; gross.hidden=true;
gross.innerHTML='<button type="button">Schließen ✕</button><img alt=""><p></p>';
document.body.appendChild(gross);
function grossZeigen(src,titel){var im=gross.querySelector('img'), voll=src.replace('_draufsicht_klein.jpg','_draufsicht_markiert.jpg');
  im.onerror=function(){im.onerror=null; if(im.getAttribute('src')!==src) im.src=src;};
  im.src=voll; im.alt=titel; gross.querySelector('p').textContent=titel+' · Fahrtrichtung ↑ · zum Schließen antippen oder Esc'; gross.hidden=false;}
gross.addEventListener('click',function(){gross.hidden=true;});
document.addEventListener('keydown',function(e){if(e.key==='Escape') gross.hidden=true;});
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.leaflet-popup .popup figure img');
  if(b){e.preventDefault(); e.stopPropagation(); grossZeigen(b.getAttribute('src'),b.alt);}},true);
map.fitBounds(abschnitte.getBounds(),{padding:[60,60],maxZoom:20});
</script></body></html>
"""

BERICHT_CSS = """
:root{--bg:#f3f4f2;--paper:#fff;--ink:#1d2226;--muted:#5d666d;--line:#d9ddd9;--accent:#d9480f;--accent-soft:#fbe6dc;--mark:#3f6fb5;
--display:"IBM Plex Sans Condensed","Arial Narrow",Arial,sans-serif;--body:"IBM Plex Sans","Segoe UI","Helvetica Neue",Arial,sans-serif;--mono:"IBM Plex Mono",Consolas,ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#15191c;--paper:#1d2226;--ink:#e6e9ea;--muted:#9aa4aa;--line:#323a3f;--accent:#ff8a4c;--accent-soft:#3a2418;--mark:#7ea6e6;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.55;padding:28px 16px 60px}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:44px}
a{color:var(--mark)} .mono{font-family:var(--mono)} .small{font-size:.8rem;color:var(--muted);margin:0}
h1,h2,h3{font-family:var(--display);line-height:1.1;margin:0}
h1{font-size:clamp(2rem,4.5vw,3rem);font-weight:700} h2{font-size:1.6rem;font-weight:600;margin-bottom:6px} h3{font-size:1.15rem;font-weight:600}
.eyebrow{font-family:var(--mono);font-size:.74rem;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.lead{color:var(--muted);max-width:68ch;margin:10px 0 0}
.meta{display:flex;flex-wrap:wrap;gap:8px 22px;font-family:var(--mono);font-size:.8rem;color:var(--muted);margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:2px solid var(--ink);border-bottom:2px solid var(--ink);margin-top:22px}
.stats div{padding:14px 16px;border-right:1px solid var(--line)} .stats div:last-child{border-right:0}
.stats b{display:block;font-family:var(--display);font-size:2rem;font-weight:600}
.stats span{font-size:.82rem;color:var(--muted)}
@media (max-width:700px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.note{background:var(--accent-soft);border-left:3px solid var(--accent);padding:12px 16px;font-size:.9rem;max-width:80ch;margin:0}
.warn{background:var(--paper);border:1px solid var(--line);padding:10px 14px;font-size:.88rem}
.planwrap{overflow-x:auto;border:1px solid var(--line);background:var(--paper)} .plan{display:block;width:100%;min-width:640px;height:auto}
.classes{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--line);background:var(--paper);margin-top:14px}
@media (max-width:640px){.classes{grid-template-columns:repeat(2,minmax(0,1fr))}}
.classes div{padding:10px 12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:4px} .classes div:last-child{border-right:0}
.classes i{display:block;height:10px;border-radius:2px} .classes b{font-family:var(--display);font-size:1.05rem}
.classes span{font-size:.8rem;color:var(--muted)} .classes em{font-style:normal;font-family:var(--mono);font-size:.8rem}
.overview{display:grid;grid-template-columns:auto minmax(0,1fr);gap:32px;align-items:start}
@media (max-width:820px){.overview{grid-template-columns:minmax(0,1fr)}}
.strip{display:grid;grid-template-columns:132px minmax(0,1fr);gap:10px;width:min(420px,100%)}
.strip img{display:block;width:132px;height:auto;border:1px solid var(--line)}
.labels{position:relative}
.lab{position:absolute;left:0;transform:translateY(-50%);display:flex;gap:8px;align-items:baseline;font-size:.8rem;text-decoration:none;color:var(--ink);white-space:nowrap}
.lab b{font-family:var(--mono);font-weight:500;padding:1px 6px;border-radius:3px} .lab span{color:var(--muted);font-family:var(--mono);font-size:.76rem}
.dir{font-family:var(--mono);font-size:.74rem;color:var(--muted);margin:6px 0 0}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:.84rem;margin:0 0 14px;padding:0;list-style:none}
.legend i{display:inline-block;width:14px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}
.tablewrap{overflow-x:auto;border:1px solid var(--line);background:var(--paper)}
table{border-collapse:collapse;width:100%;font-size:.88rem;min-width:620px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left} td.mono{white-space:nowrap}
th{font-weight:600;font-size:.76rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;background:var(--bg)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums} tfoot td{font-weight:600;border-top:2px solid var(--ink);border-bottom:0}
.pct{font-family:var(--mono);font-size:.82rem;padding:2px 7px;border-radius:3px;white-space:nowrap} .pct.big{font-size:1rem;padding:4px 10px}
.z1,.lab.z1 b{background:#1b7f3b;color:#fff} .z2,.lab.z2 b{background:#7ccf4f;color:#13240c} .z3,.lab.z3 b{background:#f2d23a;color:#2b2400}
.z4,.lab.z4 b{background:#f08a24;color:#2b1400} .z5,.lab.z5 b{background:#e0312b;color:#fff}
.scale{font-size:.78rem;color:var(--muted);margin-top:8px}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px} @media (max-width:900px){.cards{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--paper);border:1px solid var(--line);padding:16px;display:flex;flex-direction:column;gap:12px;min-width:0}
.card-h{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.pics{display:grid;grid-template-columns:1.55fr 1fr;gap:8px} .pics figure{margin:0;min-width:0}
.pics img{width:100%;height:auto;display:block;border:1px solid var(--line)} .pics figcaption{font-size:.74rem;color:var(--muted);margin-top:3px}
.kv{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 14px;margin:0}
@media (max-width:520px){.kv{grid-template-columns:repeat(2,minmax(0,1fr))}.pics{grid-template-columns:minmax(0,1fr)}}
.kv dt{font-size:.72rem;color:var(--muted)} .kv dd{margin:0;font-family:var(--mono);font-size:.86rem}
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:28px} @media (max-width:820px){.cols{grid-template-columns:minmax(0,1fr)}}
.cols ul{padding-left:1.1em;margin:8px 0 0} .cols li{margin-bottom:6px}
.zoom{all:unset;cursor:zoom-in;position:relative;display:block}
.zoom span{position:absolute;right:6px;top:6px;background:rgba(0,0,0,.65);color:#fff;font-size:.72rem;padding:2px 7px;border-radius:3px;font-family:var(--mono)}
.lb{position:fixed;inset:0;z-index:50;background:#0f1215;display:flex;flex-direction:column;padding:12px 16px} .lb[hidden]{display:none}
.lb-bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;color:#eef0f1;margin-bottom:10px} .lb-bar h3{font-size:1.05rem;margin-right:auto}
.lb-bar button{font:inherit;font-size:.85rem;background:#2b3237;color:#eef0f1;border:1px solid #4a535a;border-radius:4px;padding:6px 12px;cursor:pointer}
.lb-bar button[aria-pressed="true"]{background:#eef0f1;color:#15191c}
.lb-img{flex:1;min-height:0;overflow:auto;display:flex;justify-content:center;align-items:flex-start}
.lb-img img{max-width:none;height:auto;width:min(100%,1320px)} .lb-img.fit img{width:auto;max-width:100%;max-height:100%}
.lb-hint{color:#9aa4aa;font-size:.78rem;margin-top:8px}
"""

BERICHT_JS = """
(function(){
  var btns=[].slice.call(document.querySelectorAll('.zoom')), lb=document.getElementById('lb'), img=document.getElementById('lbimg'),
      t=document.getElementById('lbt'), mk=document.getElementById('lbmark'), fit=document.getElementById('lbfit'), box=document.getElementById('lbbox'), cur=0, marked=true, last=null;
  if(!btns.length) return;
  function show(i){cur=(i+btns.length)%btns.length; var b=btns[cur];
    img.src='bilder/'+b.dataset.n+(marked?'_draufsicht_markiert.jpg':'_draufsicht_roh.jpg'); t.textContent=b.dataset.t+' · '+b.dataset.n;}
  btns.forEach(function(b,i){b.addEventListener('click',function(){last=b; lb.hidden=false; show(i); document.body.style.overflow='hidden';});});
  function close(){lb.hidden=true; document.body.style.overflow=''; if(last) last.focus();}
  document.getElementById('lbclose').onclick=close;
  document.getElementById('lbprev').onclick=function(){show(cur-1)}; document.getElementById('lbnext').onclick=function(){show(cur+1)};
  mk.onclick=function(){marked=!marked; mk.setAttribute('aria-pressed',marked); mk.textContent=marked?'Markierungen an':'Markierungen aus'; show(cur);};
  fit.onclick=function(){var f=!box.classList.contains('fit'); box.classList.toggle('fit',f); fit.setAttribute('aria-pressed',f);};
  document.addEventListener('keydown',function(e){if(lb.hidden)return; if(e.key==='Escape')close(); if(e.key==='ArrowRight')show(cur+1); if(e.key==='ArrowLeft')show(cur-1);});
})();
"""


def bericht_html(args, S, extra, fehler, ax, plan, n_ohne_gps):
    A_ges = sum(r['bezugsflaeche'] for r in S)
    tot_a = sum(r['schadflaeche'] for r in S); tot_p = sum(r['flickflaeche'] for r in S)
    tot_n = sum(r['netzriss_flaeche'] for r in S)
    tl = sum(r['laengsriss'] for r in S); tq = sum(r['querriss'] for r in S)
    anteil = tot_a / A_ges * 100 if A_ges else 0
    esc = html.escape
    zeiten = [r['zeit'] for r in S if r['zeit']]
    datum = ''
    if zeiten:
        try:
            t0 = datetime.datetime.strptime(min(zeiten), '%Y:%m:%d %H:%M:%S'); t1 = datetime.datetime.strptime(max(zeiten), '%Y:%m:%d %H:%M:%S')
            datum = f"Aufnahme {t0:%d.%m.%Y}, {t0:%H:%M}–{t1:%H:%M} Uhr"
        except ValueError:
            datum = f"Aufnahme {min(zeiten)}"
    lat = [r['lat'] for r in S if r['lat'] is not None]; lon = [r['lon'] for r in S if r['lon'] is not None]
    pos = f"{de(np.mean(lat), 4)}° N · {de(np.mean(lon), 4)}° O" if lat else 'ohne GPS'
    lz = lambda p: f"z{klasse(p)}"

    rows = ''.join(f"<tr><td class='mono'>{r['nr']:02d}</td><td class='mono'>{r['von']:g}–{r['bis']:g} m</td><td class='num'>{de(r['laengsriss'])}</td>"
                   f"<td class='num'>{de(r['querriss'])}</td><td class='num'>{de(r['netzriss_flaeche'], 2)}</td><td class='num'>{de(r['flickflaeche'], 2)}</td>"
                   f"<td class='num'>{de(r['schadflaeche'], 2)}</td><td class='num'>{de(r['schadanteil'])} %</td><td class='num'><span class='pct {lz(r['schadanteil'])}'>Klasse {r['klasse']}</span></td></tr>" for r in S)
    n = len(S)
    labels = ''.join(f"<a class='lab {lz(r['schadanteil'])}' href='#a{r['nr']:02d}' style='top:{(n - 1 - k) * 100 / n + 50 / n:.2f}%'><b>{r['nr']:02d}</b>"
                     f"<span>{r['von']:g}–{r['bis']:g} m · {de(r['schadanteil'])} %</span></a>" for k, r in enumerate(S))
    cnt = {c: sum(1 for r in S if r['klasse'] == c) for c in range(1, 6)}
    lab_len = args.bis - args.von
    classes = ''.join(f"<div><i style='background:{KLASSE_FARBE[c]}'></i><b>Klasse {c}</b><span>{KLASSE_NAME[c]}</span><em>{cnt[c]} Abschnitt{'e' if cnt[c] != 1 else ''} · {cnt[c] * lab_len:g} m</em></div>" for c in range(1, 6))

    def card(r, anker, titel):
        gps = f" · GPS ±{de(r['gps_genauigkeit'])} m" if r['gps_genauigkeit'] else ''
        ml = maps_link(r)
        return (f"<article class='card' id='{anker}'><header class='card-h'><div><h3>{esc(titel)}</h3><p class='mono small'>{esc(r['datei'])} · {esc(r['zeit'][11:] or '–')} Uhr{gps}</p></div>"
                f"<span class='pct big {lz(r['schadanteil'])}'>{de(r['schadanteil'])} %</span></header>"
                f"<div class='pics'><figure><img src='bilder/{esc(r['datei'])}_foto.jpg' alt='Foto {esc(r['datei'])}' loading='lazy'><figcaption>Foto mit Auswertefläche {args.von:g}–{args.bis:g} m</figcaption></figure>"
                f"<figure><button class='zoom' type='button' data-n='{esc(r['datei'])}' data-t='{esc(titel)}'><img src='bilder/{esc(r['datei'])}_draufsicht_klein.jpg' alt='Draufsicht {esc(r['datei'])}' loading='lazy'><span>⤢ Groß</span></button>"
                f"<figcaption>Draufsicht {de(args.breite, 2)} × {de(lab_len, 2)} m · antippen zum Vergrößern</figcaption></figure></div>"
                f"<dl class='kv'><div><dt>Längsrisse</dt><dd>{de(r['laengsriss'])} m</dd></div><div><dt>Querrisse</dt><dd>{de(r['querriss'])} m</dd></div>"
                f"<div><dt>Netzrisse (Fläche)</dt><dd>{de(r['netzriss_flaeche'], 2)} m²</dd></div><div><dt>Einzelrisse L × 0,5 m</dt><dd>{de(r['einzelriss_flaeche_regel'], 2)} m²</dd></div>"
                f"<div><dt>Einzelrisse ohne Überlappung</dt><dd>{de(r['einzelriss_flaeche'], 2)} m²</dd></div><div><dt>Flickstellen</dt><dd>{de(r['flickflaeche'], 2)} m²</dd></div>"
                f"<div><dt>Schatten (nicht ausgewertet)</dt><dd>{de(r.get('schattenflaeche', 0), 2)} m²</dd></div><div><dt>Ausgewertete Fläche</dt><dd>{de(r['bezugsflaeche'], 1)} m²</dd></div><div><dt>Schadfläche (Union)</dt><dd>{de(r['schadflaeche'], 2)} m²</dd></div>"
                f"<div><dt>Kamera</dt><dd>{de(r['kamerahoehe'], 2)} m · {de(r['neigung'])}°</dd></div></dl>"
                + (f"<a href='{ml}' target='_blank' rel='noopener'>In Google Maps öffnen ↗</a>" if ml else '') + "</article>")
    cards = ''.join(card(r, f"a{r['nr']:02d}", f"Abschnitt {r['nr']:02d} · Station {r['von']:g}–{r['bis']:g} m") for r in S)
    cards += ''.join(card(r, f"x{i}", 'Übersprungen (--ohne) · nicht Teil der Strecke') for i, r in enumerate(extra))

    hinweise = ''
    if fehler:
        hinweise += "<p class='warn'><b>Nicht ausgewertet:</b> " + '; '.join(f"{esc(n_)} ({esc(m)})" for n_, m in fehler) + "</p>"
    if n_ohne_gps:
        hinweise += f"<p class='warn'><b>{n_ohne_gps} Foto(s) ohne GPS-Daten.</b> Für Lageplan und Karte werden Originaldateien mit Standort benötigt.</p>"
    if abs(lab_len - args.abstand) > 0.01:
        hinweise += f"<p class='warn'><b>Hinweis:</b> Auswertelänge {de(lab_len, 1)} m ≠ Fotoabstand {de(args.abstand, 1)} m – Abschnitte überlappen oder haben Lücken.</p>"

    karte = ''
    if plan:
        karte = f"""<section><h2>Zustandsklassen auf der Karte</h2>
<p class="lead" style="margin:0 0 18px">Jeder Abschnitt ist nach seinem Schadanteil eingefärbt. Die Abschnitte liegen auf der Fahrbahnachse aus den GPS-Standorten der Fotos. Ein Klick springt zur Detailansicht. Das Luftbild dazu zeigt die Datei <b>Karte.html</b> (basemap.at).</p>
<div class="planwrap">{plan}</div><div class="classes">{classes}</div>
<p class="scale">Klassen nach Projektvorgabe: 1 = 0–10 %, 2 = 10–20 %, 3 = 20–30 %, 4 = 30–40 %, 5 = ab 40 % Schadanteil. Keine Note nach RVS 13.01.15.</p></section>"""

    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(args.titel)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{BERICHT_CSS}</style></head><body><div class="wrap">
<header><div class="eyebrow">RoadSense · Auswertung v{VERSION} · Bildverarbeitung ohne trainiertes Modell</div>
<h1>{esc(args.titel)}</h1>
<p class="lead">Automatische Auswertung von {n} Fotos{(' in ' + esc(args.ort)) if args.ort else ''}. Jedes Foto wird einzeln kalibriert, der Bereich {args.von:g}–{args.bis:g} m vor der Kamera in die Vogelperspektive entzerrt und auf Risse und Flickstellen untersucht.</p>
<div class="meta"><span>{esc(datum)}</span><span>{esc(S[0].get('modell', '') or '')}</span><span>Fahrbahnbreite {de(args.breite, 2)} m{' (aus Fotos bestimmt)' if getattr(args, 'breite_gemessen', False) else ''}</span><span>{pos}</span><span>Erstellt {datetime.datetime.now():%d.%m.%Y %H:%M}</span></div>
<div class="stats"><div><b>{de(anteil)} %</b><span>Schadanteil gesamt · Klasse {klasse(anteil)}</span></div>
<div><b>{de(tot_a)} m²</b><span>Schadfläche ohne Doppelzählung</span></div>
<div><b>{de(tl + tq, 0)} m</b><span>Längs- und Querrisse</span></div>
<div><b>{de(tot_n, 1)} m²</b><span>Netzrisse · dazu {de(tot_p, 2)} m² Flickstellen</span></div></div></header>
<p class="note"><b>Prototyp, Zustandsklassen nach Projektvorgabe, nicht nach RVS.</b> Die Werte stammen aus klassischer Bildverarbeitung und sind nicht gegen Messungen vor Ort geprüft. Schatten werden erkannt und nicht ausgewertet; sehr feine Haarrisse werden teils übersehen.</p>
{hinweise}
{karte}
<section><h2>Vogelperspektive der Strecke</h2>
<p class="lead" style="margin:0 0 18px">Die entzerrten Abschnitte aneinandergereiht, Fahrtrichtung nach oben. Station = Meter ab dem ersten Foto.</p>
<ul class="legend"><li><i style="background:#e62828"></i>Längsriss</li><li><i style="background:#ff8c00"></i>Querriss</li><li><i style="background:#aa1e3c"></i>Netzriss (Fläche)</li><li><i style="background:#1e78e6"></i>Flickstelle</li><li><i style="background:#ff8080"></i>Einflussfläche Längs-/Querriss (2 × 0,25 m)</li><li><i style="background:repeating-linear-gradient(45deg,#c8c8c8 0 2px,#555 2px 6px)"></i>Schatten (nicht ausgewertet)</li></ul>
<div class="overview"><div><div class="strip"><img src="bilder/strecke_vogelperspektive_klein.jpg" alt="Draufsicht der gesamten Strecke"><div class="labels">{labels}</div></div>
<p class="dir">↑ Fahrtrichtung · 1 Abschnitt = {de(args.breite, 2)} × {de(lab_len, 2)} m</p></div>
<div style="min-width:0"><div class="tablewrap"><table><thead><tr><th>Abschn.</th><th>Station</th><th class="num">Längsrisse m</th><th class="num">Querrisse m</th><th class="num">Netzrisse m²</th><th class="num">Flicken m²</th><th class="num">Schaden m²</th><th class="num">Anteil</th><th class="num">Zustand</th></tr></thead>
<tbody>{rows}</tbody><tfoot><tr><td colspan="2">Summe {S[0]['von']:g}–{S[-1]['bis']:g} m</td><td class="num">{de(tl)}</td><td class="num">{de(tq)}</td><td class="num">{de(tot_n, 2)}</td><td class="num">{de(tot_p, 2)}</td><td class="num">{de(tot_a, 2)}</td><td class="num">{de(anteil)} %</td><td class="num"><span class="pct {lz(anteil)}">Klasse {klasse(anteil)}</span></td></tr></tfoot></table></div>
<p class="scale">Die Tabelle gibt es als Schaeden.csv für Excel oder Google Sheets.</p></div></div></section>
<section><h2>Abschnitte im Detail</h2><p class="lead" style="margin:0 0 18px">Links das Originalfoto mit der zurückprojizierten Auswertung, rechts die entzerrte Draufsicht.</p><div class="cards">{cards}</div></section>
<div class="cols"><section><h2>Methode</h2><ul>
<li><b>Fahrbahnerkennung:</b> Die Asphaltfläche wird Zeile für Zeile von der Bildmitte aus verfolgt; Randsteine, Markierungen, Gehsteig und Rinne begrenzen sie. Abgestimmt an handmarkierten Lernfotos (Übereinstimmung 96–98 %).</li><li><b>Kalibrierung je Foto:</b> Aus dem Fluchtpunkt der Fahrbahnkanten folgen Kameraneigung und Blickrichtung, aus Fahrbahnbreite bzw. Handyhöhe der Maßstab. Unsichere Fotos (verdeckte Kante, Ausreißer) übernehmen das Streckenmittel.</li><li><b>Schatten</b> (dunkler, leicht bläulich, mit Asphaltkörnung) werden erkannt und von der Bezugsfläche abgezogen.</li>
<li><b>Auswertebereich {args.von:g}–{args.bis:g} m</b> vor der Kamera, entzerrt auf 5 mm je Pixel.</li>
<li><b>Schadensarten</b> (Begriffe nach RVS 13.01.11): Längsriss, Querriss (Richtung bis/über 45° zur Fahrbahnachse), Netzriss, Flickstelle.</li>
<li><b>Risserkennung:</b> Dunkelheit wird entlang kurzer Linienstücke in 16 Richtungen gemittelt – die zufällige Asphaltkörnung mittelt sich weg, Risse bleiben. Absolute Schwelle über dem Rauschen, Mindestlänge 40 cm; Gitterroste und Schachtdeckel ausgenommen.</li>
<li><b>Flächen:</b> Einzelrisse = Länge × 0,50 m (Streifen 0,25 m je Seite); Netzrisse = umschlossene Fläche; Flickstellen = Fläche; Gesamtschaden ohne Doppelzählung.</li></ul></section>
<section><h2>Grenzen</h2><ul>
<li><b>Zustandsklassen</b> in 10-%-Stufen nach Projektvorgabe, keine RVS-Note.</li>
<li><b>Schlaglöcher</b> sind aus Fotos allein nicht sicher von dunklen Flickstellen zu trennen (Tiefeninformation/LiDAR nötig).</li>
<li><b>Fahrbahnrand:</b> Im {EDGE_KEEP * 100:.0f}-cm-Streifen an der Kante (Randstein, Rinne) wird nicht ausgewertet.</li><li><b>Schatten:</b> Schattenflächen werden nicht ausgewertet; liegt ein Abschnitt großteils im Schatten, beruht sein Anteil auf der kleinen sonnigen Restfläche.</li>
<li><b>Auflösung:</b> Ab ca. 6 m vor der Kamera werden feine Risse und Netzrisse unsicher; Fotos alle 2,5 m mit Bereich 4–6,5 m verbessern das.</li>
<li><b>Lage:</b> Abschnitte im Sollabstand auf der GPS-Achse; Abweichungen von ca. 1 m möglich. Entfernungen gelten für ebene Fahrbahn.</li></ul></section></div>
<p class="small">Kartengrundlage der Luftbildkarte: Datenquelle: <a href="https://basemap.at">basemap.at</a> (CC BY 4.0).</p>
</div>
<div class="lb" id="lb" hidden role="dialog" aria-modal="true" aria-labelledby="lbt"><div class="lb-bar"><h3 id="lbt"></h3>
<button type="button" id="lbmark" aria-pressed="true">Markierungen an</button><button type="button" id="lbfit" aria-pressed="false">Einpassen</button>
<button type="button" id="lbprev" aria-label="Vorheriger Abschnitt">←</button><button type="button" id="lbnext" aria-label="Nächster Abschnitt">→</button><button type="button" id="lbclose">Schließen ✕</button></div>
<div class="lb-img" id="lbbox"><img id="lbimg" alt=""></div><p class="lb-hint">Volle Auflösung: 1 Pixel = 5 mm · Fahrtrichtung nach oben</p></div>
<script>{BERICHT_JS}</script></body></html>"""


def sende_webhook(url, daten):
    req = urllib.request.Request(url, data=json.dumps(daten, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------
FOTOS = 'Fotos'              # je Straße ein Unterordner mit den Fotos
ERGEBNISSE = 'Ergebnisse'    # je Straße ein Unterordner mit Bericht, Karte und Tabellen


def strassen_ordner(fotos, hier):
    """Fotoordner, die ausgewertet werden – ein Ordner = eine Straße."""
    fotos_root = os.path.join(hier, FOTOS)
    if fotos:
        p = fotos if os.path.isdir(fotos) else os.path.join(fotos_root, fotos)
        if not os.path.isdir(p):
            sys.exit(f'Fotoordner nicht gefunden: {fotos}\n(weder als Pfad noch als Unterordner von {fotos_root})')
        return [os.path.abspath(p)]
    if not os.path.isdir(fotos_root):
        sys.exit(f'Ordner "{FOTOS}" fehlt: {fotos_root}\nBitte je Straße einen Unterordner mit den Fotos anlegen, z. B. {FOTOS}\\Hauptstraße')
    ordner = sorted(d.path for d in os.scandir(fotos_root) if d.is_dir())
    if not ordner:
        sys.exit(f'Keine Straßenordner in {fotos_root}\nBitte je Straße einen Unterordner mit den Fotos anlegen, z. B. {FOTOS}\\Hauptstraße')
    return ordner


def ist_aktuell(ordner, ergebnis_root):
    """True, wenn die Straße schon einen Bericht hat und seither kein Foto dazugekommen oder geändert worden ist."""
    bericht = os.path.join(ergebnis_root, os.path.basename(os.path.normpath(ordner)), 'Bericht.html')
    if not os.path.exists(bericht):
        return False
    fotos = [p for p in glob.glob(os.path.join(ordner, '*')) if p.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.heif'))]
    # getctime = Zeitpunkt, an dem das Foto in den Ordner kopiert wurde (Windows übernimmt beim Kopieren das alte Änderungsdatum)
    return all(max(os.path.getmtime(p), os.path.getctime(p)) <= os.path.getmtime(bericht) for p in fotos)


def strasse_auswerten(ordner, args_alle, ergebnis_root):
    """Wertet einen Fotoordner aus; Ergebnisse landen in <ergebnis_root>/<Name des Fotoordners>."""
    strasse = os.path.basename(os.path.normpath(ordner))
    args = argparse.Namespace(**vars(args_alle))
    args.titel = args_alle.titel or strasse
    aus = os.path.join(ergebnis_root, strasse)
    print(f'\n=== {strasse} ===')

    heic_umwandeln(ordner)
    pfade = sorted(p for p in glob.glob(os.path.join(ordner, '*')) if p.lower().endswith(('.jpg', '.jpeg', '.png')))
    if not pfade:
        print(f'  ! Keine Fotos gefunden in: {ordner} – bitte die Fotos als JPG oder HEIC (mit GPS-Daten) hineinkopieren.')
        return None
    print(f'{len(pfade)} Fotos in {ordner}')
    bild_dir = os.path.join(aus, 'bilder')
    shutil.rmtree(bild_dir, ignore_errors=True)    # Bilder eines früheren Laufs dieser Straße ersetzen
    os.makedirs(bild_dir, exist_ok=True)

    geladen = []
    for p in pfade:
        try:
            img, ex = lade_foto(p)
        except Exception as e:
            print(f'  ! {os.path.basename(p)}: kann nicht gelesen werden ({e})'); continue
        geladen.append((p, img, exif_daten(ex)))
    geladen.sort(key=lambda t: (t[2]['zeit'] or '', os.path.basename(t[0])))   # nach Aufnahmezeit

    S, extra, fehler, alle = [], [], [], []
    ohne = {o.lower() for o in args.ohne}

    # Schritt 1: Fahrbahn finden und jedes Foto kalibrieren
    fotos = []
    print('  Fahrbahn erkennen und kalibrieren …')
    for p, img, meta in geladen:
        foto = Foto(p, img, meta, args)
        try:
            foto.segmentieren()
        except Exception as e:
            print(f'  · {foto.name}: übersprungen: {e}'); fehler.append((foto.name, str(e))); continue
        try:
            foto.kalibrieren(args.hoehe)
        except Exception as e:
            foto.kal_grund = str(e)
        fotos.append(foto)
    try:
        B = kalibrierung_abgleichen(fotos, args)
    except Exception as e:
        print(f'  ! Kalibrierung nicht möglich: {e}')
        return None
    if not args.breite:
        args.breite = B          # für Bericht, Lageplan und Karte: gemessene Fahrbahnbreite
        args.breite_gemessen = True
    for f in fotos:
        if f.kal != 'eigen':
            print(f'  · {f.name}: Kalibrierung vom Streckenmittel ({f.kal_grund})')

    # Schritt 2: Schäden je Foto
    for foto in fotos:
        img, meta = foto.img, foto.meta
        print(f'  · {foto.name} …', end=' ', flush=True)
        try:
            res, top, road, pm, risse, buf, nm, Hm, sch = auswerten(foto, args, B)
        except Exception as e:
            print(f'übersprungen: {e}'); fehler.append((foto.name, str(e))); continue
        res['modell'] = meta['modell']
        ov = overlay_draufsicht(top, road, pm, risse, buf, nm, sch)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_markiert.jpg'), ov)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_roh.jpg'), top, 88)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_klein.jpg'), cv2.resize(ov, (ov.shape[1] // 2, ov.shape[0] // 2), interpolation=cv2.INTER_AREA), 80)
        of = overlay_foto(img, Hm, top, road, pm, risse, buf, nm, sch)
        imwrite(os.path.join(bild_dir, f'{foto.name}_foto.jpg'), cv2.resize(of, (960, int(960 * of.shape[0] / of.shape[1])), interpolation=cv2.INTER_AREA), 78)
        if foto.name.lower() in ohne:
            extra.append(res); print(f'{res["schadanteil"]:.1f} % (übersprungen per --ohne)')
        else:
            S.append(res); alle.append(ov); print(f'{res["schadanteil"]:.1f} % · Klasse {res["klasse"]}')
    if not S:
        print('  ! Kein Foto konnte ausgewertet werden.')
        return None

    lab_len = args.bis - args.von
    for k, r in enumerate(S):
        r['nr'] = k + 1; r['von'] = round(args.abstand * k + args.von, 2); r['bis'] = round(r['von'] + lab_len, 2)

    # Vogelperspektive der Strecke (erstes Foto unten)
    mos = np.vstack(alle[::-1])
    imwrite(os.path.join(bild_dir, 'strecke_vogelperspektive.jpg'), mos, 85)
    klein = cv2.resize(mos, (264, int(264 * mos.shape[0] / mos.shape[1])), interpolation=cv2.INTER_AREA)
    imwrite(os.path.join(bild_dir, 'strecke_vogelperspektive_klein.jpg'), klein, 80)

    # Verortung
    mit_gps = [r for r in S if r['lat'] is not None]
    ax, plan = None, ''
    if len(mit_gps) >= 2:
        ax = Achse([(r['lat'], r['lon']) for r in mit_gps])
        plan = svg_lageplan(ax, S, args.breite)
        gj = geojson(ax, S, args.breite, args.titel)
        with open(os.path.join(aus, 'Abschnitte.geojson'), 'w', encoding='utf-8') as fh:
            json.dump(gj, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(aus, 'Karte.html'), 'w', encoding='utf-8') as fh:
            fh.write(KARTE_HTML.replace('__GEOJSON__', json.dumps(gj, ensure_ascii=False)).replace('__TITEL__', html.escape(args.titel)))
        with open(os.path.join(aus, 'Zustandsklassen.kml'), 'w', encoding='utf-8') as fh:
            fh.write(kml(ax, S, args.breite, args.titel))
    else:
        print('  ! Zu wenige Fotos mit GPS – Lageplan und Luftbildkarte werden nicht erzeugt.')

    # Tabelle
    with open(os.path.join(aus, 'Schaeden.csv'), 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['Abschnitt', 'Station von (m)', 'Station bis (m)', 'Foto', 'Aufnahmezeit', 'Breitengrad', 'Längengrad', 'Fahrbahnbreite (m)', 'Ausgewertete Fläche (m²)', 'Schatten, nicht ausgewertet (m²)',
                    'Längsrisse (m)', 'Querrisse (m)', 'Einzelrissfläche L×0,5 (m²)', 'Einzelrissfläche ohne Überlappung (m²)', 'Netzrissfläche (m²)',
                    'Flickfläche (m²)', 'Schadfläche (m²)', 'Schadanteil (%)', 'Zustandsklasse', 'Google Maps'])
        for r in S:
            w.writerow([f"{r['nr']:02d}", de(r['von'], 1), de(r['bis'], 1), r['datei'], r['zeit'],
                        de(r['lat'], 6) if r['lat'] is not None else '', de(r['lon'], 6) if r['lon'] is not None else '',
                        de(r['fahrbahnbreite'], 2), de(r['bezugsflaeche'], 1), de(r.get('schattenflaeche', 0), 2), de(r['laengsriss']), de(r['querriss']),
                        de(r['einzelriss_flaeche_regel'], 2), de(r['einzelriss_flaeche'], 2), de(r['netzriss_flaeche'], 2),
                        de(r['flickflaeche'], 2), de(r['schadflaeche'], 2), de(r['schadanteil']), r['klasse'], maps_link(r)])
    with open(os.path.join(aus, 'ergebnisse.json'), 'w', encoding='utf-8') as fh:
        json.dump({'titel': args.titel, 'ort': args.ort, 'version': VERSION, 'abschnitte': S, 'uebersprungen': extra,
                   'nicht_ausgewertet': fehler}, fh, ensure_ascii=False, indent=1, default=float)

    # Bericht
    n_ohne_gps = sum(1 for r in S if r['lat'] is None)
    with open(os.path.join(aus, 'Bericht.html'), 'w', encoding='utf-8') as fh:
        fh.write(bericht_html(args, S, extra, fehler, ax, plan, n_ohne_gps))

    # Optional: an Zapier o. Ä. senden
    if args.webhook:
        try:
            A_ges = sum(r['bezugsflaeche'] for r in S); tot = sum(r['schadflaeche'] for r in S)
            daten = {'titel': args.titel, 'ort': args.ort, 'abschnitte': len(S), 'schadanteil_gesamt': round(tot / A_ges * 100, 1),
                     'zustandsklasse_gesamt': klasse(tot / A_ges * 100),
                     'werte': [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in S]}
            print(f'  Webhook: Status {sende_webhook(args.webhook, daten)}')
        except Exception as e:
            print(f'  ! Webhook fehlgeschlagen: {e}')

    print(f'  → {aus}')
    return aus


def main():
    ap = argparse.ArgumentParser(description='RoadSense – Straßenschäden aus Fotos auswerten, Bericht und Luftbildkarte erzeugen.')
    hier = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument('fotos', nargs='?', default=None,
                    help=f'Straße (Unterordner von "{FOTOS}") oder Pfad zu einem Fotoordner; ohne Angabe werden alle Straßen in "{FOTOS}" ausgewertet')
    ap.add_argument('--ausgabe', default=os.path.join(hier, ERGEBNISSE),
                    help=f'Oberordner für die Ergebnisse, darin je Straße ein eigener Unterordner (Standard: "{ERGEBNISSE}" neben diesem Programm)')
    ap.add_argument('--titel', default=None, help='Titel des Berichts (Standard: Name der Straße)')
    ap.add_argument('--ort', default='', help='Ort, erscheint im Bericht')
    ap.add_argument('--breite', type=float, default=None,
                    help='vor Ort gemessene Fahrbahnbreite in m (genaueste Variante); ohne Angabe wird sie aus den Fotos bestimmt')
    ap.add_argument('--hoehe', type=float, default=1.60,
                    help='Höhe des Handys über der Fahrbahn in m, wenn --breite fehlt (Standard 1,60)')
    ap.add_argument('--von', type=float, default=4.0, help='Beginn des Auswertebereichs vor der Kamera in m (Standard 4)')
    ap.add_argument('--bis', type=float, default=9.0, help='Ende des Auswertebereichs vor der Kamera in m (Standard 9)')
    ap.add_argument('--abstand', type=float, default=None, help='Fotoabstand in m (Standard = bis − von)')
    ap.add_argument('--ohne', nargs='*', default=[], help='Fotos, die nicht zur Strecke gehören (z. B. Kalibrierfoto IMG_1663)')
    ap.add_argument('--webhook', default=None, help='URL, an die die Ergebnisse als JSON gesendet werden (z. B. Zapier Catch Hook)')
    ap.add_argument('--alle', action='store_true', help='alle Straßen neu auswerten, auch solche, die schon ein aktuelles Ergebnis haben')
    args = ap.parse_args()
    if args.abstand is None:
        args.abstand = args.bis - args.von

    ordner = strassen_ordner(args.fotos, hier)
    ergebnis_root = os.path.abspath(args.ausgabe)
    if not args.fotos and not args.alle:
        aktuell = [o for o in ordner if ist_aktuell(o, ergebnis_root)]
        ordner = [o for o in ordner if o not in aktuell]
        if aktuell:
            print('Schon ausgewertet (übersprungen, neu auswerten mit --alle): ' + ', '.join(os.path.basename(o) for o in aktuell))
        if not ordner:
            print(f'\nKeine neuen oder geänderten Straßen in "{FOTOS}". Ergebnisse in: {ergebnis_root}')
            return
    print(f'RoadSense v{VERSION}: {len(ordner)} Straße(n) – ' + ', '.join(os.path.basename(o) for o in ordner))
    fertig = [aus for o in ordner if (aus := strasse_auswerten(o, args, ergebnis_root))]

    print(f'\nFertig: {len(fertig)} von {len(ordner)} Straße(n) ausgewertet. Ergebnisse in: {ergebnis_root}')
    for aus in fertig:
        print(f'  · {os.path.basename(aus)}\\Bericht.html')


if __name__ == '__main__':
    main()
