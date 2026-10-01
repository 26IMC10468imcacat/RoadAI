# -*- coding: utf-8 -*-
"""
RoadAI – Straßenschäden aus Smartphone-Fotos auswerten (Prototyp v2.2)
======================================================================

Erzeugt aus einem Ordner mit JPG-Fotos (alle ca. 5 m, Kamera nach vorne, Fahrbahnmitte):
  • Bericht.html        – Prüfbericht mit Zustandsklassen, Lageplan, Vogelperspektive, Details
  • Karte.html          – Luftbildkarte (basemap.at) mit farbigen 5-m-Abschnitten
  • Schaeden.csv        – Werte je Abschnitt (für Excel / Google Sheets)
  • Abschnitte.geojson  – Abschnitte als Geodaten (QGIS, uMap, Web-Karten)
  • Zustandsklassen.kml – für Google My Maps / Google Earth
  • ergebnisse.json     – alle Rohwerte
  • bilder/             – Overlays und Draufsichten

Installation (einmalig, in der Eingabeaufforderung):
    py -m pip install numpy opencv-python pillow torch transformers

Aufruf (im Terminal, im Ordner dieses Programms):
    py "Lebende Straße.py"
        → nimmt die Fotos aus dem Unterordner "bilder", Ergebnisse landen in "Ergebnis"
    py "Lebende Straße.py" --ohne IMG_1663 --titel "Teststrecke 01 Langenlois" --ort "Langenlois"

Wichtige Optionen (alle optional):
    FOTOORDNER         anderer Fotoordner als "bilder" (erste Angabe nach dem Programmnamen)
    --ausgabe ORDNER   Zielordner (Standard: Unterordner "Ergebnis" neben dem Programm)
    --breite 5.80      Referenzbreite am Kalibrierfoto (Standard 5,80 m)
    --referenzfoto IMG_1663  Foto mit bekannter Referenzbreite
    --max-breite 7.0    maximale zulässige Fahrbahnbreite
    --von 2.0 --bis 7.0  Ziel-Auswertebereich; wird bei Bedarf automatisch nur so weit wie nötig nach vorne verschoben
    --ohne NAME ...    Fotos überspringen (z. B. Kalibrierfoto)
    --webhook URL      Ergebnisse zusätzlich an eine URL senden (z. B. Zapier „Catch Hook“)

Fotos: JPG/PNG mit EXIF-Daten; unter Windows werden HEIC/HEIF automatisch über die Windows-Bildkomponenten in JPG-Arbeitskopien umgewandelt.
Die HEIC-Originale bleiben unverändert; GPS und Aufnahmezeit werden zusätzlich aus Windows-Metadaten übernommen.

Fachliche Regeln (Projektvorgabe):
    Längs-/Querriss = Länge × 0,50 m Einflussbreite · Netzriss = umschlossene Fläche ·
    Flickstelle = Fläche · Gesamtschaden ohne Doppelzählung (Vereinigungsfläche) ·
    Zustandsklasse 1–5 in 10-%-Stufen des Schadanteils (keine RVS-Note).
Kartengrundlage: basemap.at (CC BY 4.0) – Quellenangabe „Datenquelle: basemap.at“.
"""

import argparse, csv, glob, html, json, math, os, sys, datetime, urllib.request, subprocess, tempfile

import numpy as np
import cv2
from PIL import Image, ImageOps

try:
    import torch
    import torch.nn.functional as F
    from transformers import AutoImageProcessor, AutoModelForSemanticSegmentation
    HAVE_ROAD_AI = True
except Exception:
    HAVE_ROAD_AI = False

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------------------------------------------------------------------------
# OpenCV-/NumPy-Ersatz für frühere SciPy-/scikit-image-Funktionen
# ---------------------------------------------------------------------------
# Hintergrund:
# Auf manchen Windows-Rechnern blockiert Smart App Control native SciPy-DLLs.
# RoadAI v2.2 benötigt deshalb für die Schadensauswertung nur noch NumPy/OpenCV.

def skeletonize(mask):
    """Morphologische Skelettierung einer Binärmaske, Ergebnis = bool."""
    img = (np.asarray(mask) > 0).astype(np.uint8) * 255
    skel = np.zeros_like(img)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))

    # Sicherheitsgrenze; bei den RoadAI-Draufsichten reicht das deutlich.
    for _ in range(max(img.shape) + 50):
        opened = cv2.morphologyEx(img, cv2.MORPH_OPEN, element)
        temp = cv2.subtract(img, opened)
        skel = cv2.bitwise_or(skel, temp)
        img = cv2.erode(img, element)
        if cv2.countNonZero(img) == 0:
            break

    return skel.astype(bool)


def binary_fill_holes(mask):
    """Füllt vollständig umschlossene Löcher in einer Binärmaske."""
    m = (np.asarray(mask) > 0).astype(np.uint8) * 255
    h, w = m.shape[:2]

    # Außenhintergrund robust bestimmen: Bild um 1 Pixel mit 0 auffüllen,
    # von dort flood-fillen und anschließend wieder zuschneiden.
    padded = cv2.copyMakeBorder(
        m, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0
    )
    flood = padded.copy()
    flood_mask = np.zeros((h + 4, w + 4), np.uint8)
    cv2.floodFill(flood, flood_mask, (0, 0), 255)

    outside = flood[1:-1, 1:-1]
    holes = cv2.bitwise_not(outside)
    filled = cv2.bitwise_or(m, holes)
    return filled.astype(bool)


def apply_hysteresis_threshold(image, low, high):
    """
    Hysterese ohne scikit-image:
    schwache Pixel >= low bleiben nur erhalten, wenn ihre zusammenhängende
    Komponente mindestens einen starken Pixel >= high enthält.
    """
    a = np.asarray(image)
    weak = a >= low
    strong = a >= high

    if not np.any(weak) or not np.any(strong):
        return np.zeros_like(weak, dtype=bool)

    n, labels = cv2.connectedComponents(weak.astype(np.uint8), connectivity=8)
    strong_labels = np.unique(labels[strong])
    strong_labels = strong_labels[strong_labels != 0]

    if len(strong_labels) == 0:
        return np.zeros_like(weak, dtype=bool)

    return np.isin(labels, strong_labels)


def sato(image, sigmas=(1.5, 2.5, 3.5), black_ridges=True):
    """
    Leichter OpenCV-Ersatz für den Sato-Linienfilter.

    Ermittelt über mehrere Gauß-Skalen die Eigenwerte der Hesse-Matrix.
    Dunkle, längliche Strukturen (Risse) erhalten eine hohe Antwort.
    Die Schnittstelle entspricht der in RoadAI verwendeten sato()-Funktion.
    """
    img = np.asarray(image, dtype=np.float32)

    # Für dunkle Risse wird die Helligkeit invertiert. Damit erscheinen sie
    # für die Hesse-Auswertung als helle Linien.
    work = -img if black_ridges else img

    response = np.zeros_like(work, dtype=np.float32)
    eps = 1e-6

    for sigma in sigmas:
        g = cv2.GaussianBlur(
            work, (0, 0), sigmaX=float(sigma), sigmaY=float(sigma),
            borderType=cv2.BORDER_REPLICATE
        )

        dxx = cv2.Sobel(g, cv2.CV_32F, 2, 0, ksize=3)
        dyy = cv2.Sobel(g, cv2.CV_32F, 0, 2, ksize=3)
        dxy = cv2.Sobel(g, cv2.CV_32F, 1, 1, ksize=3)

        # Skalen-Normalisierung der zweiten Ableitung.
        scale = float(sigma) ** 2
        dxx *= scale
        dyy *= scale
        dxy *= scale

        tr = dxx + dyy
        disc = np.sqrt(np.maximum((dxx - dyy) ** 2 + 4.0 * dxy ** 2, 0.0))
        l1 = 0.5 * (tr - disc)
        l2 = 0.5 * (tr + disc)

        # Eigenwert mit größerem Betrag = Krümmung quer zur Linie.
        swap = np.abs(l1) > np.abs(l2)
        small = np.where(swap, l2, l1)
        large = np.where(swap, l1, l2)

        # Nach Invertierung sollen helle Linien eine negative starke
        # Querkrümmung besitzen.
        strength = np.maximum(-large, 0.0)

        # Linienförmigkeit: eine Hauptkrümmung groß, die andere klein.
        ratio = np.abs(small) / (np.abs(large) + eps)
        line_like = np.exp(-(ratio ** 2) / (2.0 * 0.5 ** 2))

        r = strength * line_like
        response = np.maximum(response, r.astype(np.float32))

    return response


VERSION = '2.2'
RES = 0.005            # 5 mm je Pixel in der Draufsicht
MARGIN = 0.40          # Rand links/rechts in der Draufsicht (nur Anzeige) [m]
CRACK_W = 0.50         # Einflussbreite Längs-/Querriss [m]
EDGE_KEEP = 0.10       # nur 10 cm Schutzstreifen gegen Randartefakte [m]
ROAD_MODEL = 'nvidia/segformer-b0-finetuned-cityscapes-1024-1024'
AUTO_SHIFT_STEP = 0.25   # bei abgeschnittener Fahrbahn 25-cm-Schritte
AUTO_SHIFT_MAX = 4.00    # maximal 4 m weiter nach vorne
MIN_ROAD_WIDTH = 2.50     # Plausibilitätsgrenze [m]
MAX_ROAD_WIDTH = 7.00     # harte Obergrenze [m]
WHITE_EDGE_SEARCH_M = 0.75 # Suchbereich um erwarteten Fahrbahnrand [m]
SHADOW_VEHICLE_RADIUS_M = 1.20  # nur fahrzeugnahe Schatten werden ausgeschlossen [m]

KLASSE_FARBE = {1: '#1b7f3b', 2: '#7ccf4f', 3: '#f2d23a', 4: '#f08a24', 5: '#e0312b'}
KLASSE_TEXT = {1: '#ffffff', 2: '#13240c', 3: '#2b2400', 4: '#2b1400', 5: '#ffffff'}
KLASSE_NAME = {1: '0–10 %', 2: '10–20 %', 3: '20–30 %', 4: '30–40 %', 5: '40 % und mehr'}
COL = {'Längsriss': (40, 40, 230), 'Querriss': (0, 140, 255), 'Netzriss': (0, 0, 150)}   # BGR
PATCH = (230, 120, 30)
NETZ = (60, 30, 170)


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
# HEIC unter Windows ohne pillow-heif
# ---------------------------------------------------------------------------
def _heic_windows(ordner, heic_paths, cache_dir):
    """Konvertiert HEIC/HEIF mit Windows/WIC in JPG-Arbeitskopien.
    Keine pillow-heif-DLL nötig. GPS, Aufnahmezeit und Kameramodell werden
    zusätzlich über Windows.Storage ausgelesen.
    """
    if not heic_paths:
        return [], {}
    if os.name != 'nt':
        print('  ! HEIC-Automatik ist derzeit für Windows ausgelegt; HEIC wird übersprungen.')
        return [], {}

    os.makedirs(cache_dir, exist_ok=True)
    meta_json = os.path.join(cache_dir, '_heic_meta.json')

    ps = r"""
param(
 [Parameter(Mandatory=$true)][string]$SourceDir,
 [Parameter(Mandatory=$true)][string]$DestDir,
 [Parameter(Mandatory=$true)][string]$MetaJson
)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$runtimeMethods=[System.WindowsRuntimeSystemExtensions].GetMethods()
$asTaskGeneric=($runtimeMethods | Where-Object {
 $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
 $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function AwaitOperation($WinRtTask,$ResultType){
 $specific=$asTaskGeneric.MakeGenericMethod($ResultType)
 $netTask=$specific.Invoke($null,@($WinRtTask))
 $netTask.Wait() | Out-Null
 return $netTask.Result
}

[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.FileProperties.ImageProperties,Windows.Storage.FileProperties,ContentType=WindowsRuntime] | Out-Null

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
$result=@()

Get-ChildItem -LiteralPath $SourceDir -File | Where-Object {
 $_.Extension.ToLower() -in @('.heic','.heif')
} | ForEach-Object {
 $src=$_.FullName
 $base=[IO.Path]::GetFileNameWithoutExtension($src)
 $dst=Join-Path $DestDir ($base+'.jpg')

 $lat=$null; $lon=$null; $dateTaken=''; $camera=''
 try {
   $sf=AwaitOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($src)) ([Windows.Storage.StorageFile])
   $props=AwaitOperation ($sf.Properties.GetImagePropertiesAsync()) ([Windows.Storage.FileProperties.ImageProperties])
   $lat=$props.Latitude; $lon=$props.Longitude
   if($props.DateTaken){$dateTaken=$props.DateTaken.ToString('yyyy:MM:dd HH:mm:ss')}
   $camera=$props.CameraModel
 } catch {}

 $need=$true
 if(Test-Path -LiteralPath $dst){
   try {$need=((Get-Item -LiteralPath $dst).LastWriteTime -lt (Get-Item -LiteralPath $src).LastWriteTime)} catch {}
 }

 if($need){
   Write-Host ('  HEIC -> JPG: '+$base)
   $uri=New-Object System.Uri($src)
   $dec=[System.Windows.Media.Imaging.BitmapDecoder]::Create(
     $uri,
     [System.Windows.Media.Imaging.BitmapCreateOptions]::PreservePixelFormat,
     [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
   $frame=$dec.Frames[0]
   $enc=New-Object System.Windows.Media.Imaging.JpegBitmapEncoder
   $enc.QualityLevel=96
   $enc.Frames.Add($frame)
   $stream=[IO.File]::Open($dst,[IO.FileMode]::Create,[IO.FileAccess]::Write)
   try {$enc.Save($stream)} finally {$stream.Dispose()}
 }

 $result += [PSCustomObject]@{
   stem=$base; jpg=$dst; latitude=$lat; longitude=$lon;
   datetime=$dateTaken; camera=$camera
 }
}

$result | ConvertTo-Json -Depth 4 | Out-File -LiteralPath $MetaJson -Encoding utf8
"""

    with tempfile.TemporaryDirectory() as td:
        ps_path = os.path.join(td, 'road_ai_heic.ps1')
        with open(ps_path, 'w', encoding='utf-8-sig') as fh:
            fh.write(ps)
        cmd = [
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', ps_path,
            '-SourceDir', os.path.abspath(ordner),
            '-DestDir', os.path.abspath(cache_dir),
            '-MetaJson', os.path.abspath(meta_json)
        ]
        try:
            cp = subprocess.run(cmd, text=True)
        except Exception as e:
            print(f'  ! HEIC-Konvertierung konnte nicht gestartet werden: {e}')
            return [], {}
        if cp.returncode != 0:
            print('  ! HEIC-Konvertierung über Windows fehlgeschlagen. HEIC-Dateien werden übersprungen.')
            return [], {}

    meta = {}
    try:
        with open(meta_json, 'r', encoding='utf-8-sig') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = [data]
        for d in data or []:
            stem = str(d.get('stem', ''))
            if not stem:
                continue
            meta[stem.lower()] = {
                'zeit': str(d.get('datetime') or ''),
                'lat': float(d['latitude']) if d.get('latitude') is not None else None,
                'lon': float(d['longitude']) if d.get('longitude') is not None else None,
                'modell': str(d.get('camera') or ''),
            }
    except Exception as e:
        print(f'  ! HEIC-Metadaten konnten nicht gelesen werden: {e}')

    jpgs = sorted(glob.glob(os.path.join(cache_dir, '*.jpg')))
    return jpgs, meta


# ---------------------------------------------------------------------------
# Optionale semantische Fahrbahnerkennung
# ---------------------------------------------------------------------------
class RoadMaskAI:
    """SegFormer Cityscapes für Fahrbahn/Nebenanlage und Fahrzeugmasken.

    Die KI definiert NICHT die Straßenschäden. Sie unterstützt nur:
      • Fahrbahngrenze road vs. sidewalk/Nebenanlage
      • Fahrzeugpixel als Anker für die Schattenunterdrückung
    """
    def __init__(self):
        if not HAVE_ROAD_AI:
            raise RuntimeError('torch/transformers nicht verfügbar')
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print(f'  Fahrbahn-KI: {ROAD_MODEL} ({self.device})')
        self.processor = AutoImageProcessor.from_pretrained(ROAD_MODEL)
        self.model = AutoModelForSemanticSegmentation.from_pretrained(ROAD_MODEL).to(self.device)
        self.model.eval()

        labels = {int(k): str(v).strip().lower() for k, v in self.model.config.id2label.items()}
        self.road_ids = [k for k, v in labels.items() if v == 'road']
        self.vehicle_ids = [
            k for k, v in labels.items()
            if v in {'car', 'truck', 'bus', 'train', 'motorcycle'}
        ]
        if not self.road_ids:
            raise RuntimeError('Klasse "road" im Segmentierungsmodell nicht gefunden')
        self.last_vehicle_mask = None

    def maske(self, bgr):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        inputs = self.processor(images=rgb, return_tensors='pt')
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = self.model(**inputs).logits
        logits = F.interpolate(
            logits, size=bgr.shape[:2], mode='bilinear', align_corners=False
        )
        pred = logits.argmax(1)[0].detach().cpu().numpy()

        self.last_vehicle_mask = np.isin(pred, self.vehicle_ids).astype(bool)

        mask = np.isin(pred, self.road_ids).astype(np.uint8)
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        )

        n, lab, st, cen = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return mask.astype(bool)

        H, W = mask.shape
        best, best_score = None, -1
        for i in range(1, n):
            area = st[i, cv2.CC_STAT_AREA]
            comp = lab == i
            lower = comp[int(H * 0.48):].sum()
            center = comp[int(H * 0.45):int(H * 0.95),
                          max(0, W // 2 - W // 10):min(W, W // 2 + W // 10)].sum()
            score = area + 2.0 * lower + 6.0 * center
            if score > best_score:
                best_score, best = score, i
        return lab == best


# ---------------------------------------------------------------------------
# Geometrie: Fahrbahnkanten, Fluchtpunkt, Kamera, Entzerrung
# ---------------------------------------------------------------------------
class Foto:
    def __init__(self, path, img, meta, args, road_ai=None):
        self.path, self.img, self.meta, self.a = path, img, meta, args
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.H, self.W = img.shape[:2]
        f35 = meta['f35'] or 26
        self.F = float(f35) / 43.267 * math.hypot(self.W, self.H)
        self.d = None
        self.road_ai = road_ai
        self.ai_mask = None
        self.vehicle_mask = None
        self.randmethode = 'klassisch'
        self.von_eff = float(args.von)
        self.bis_eff = float(args.bis)
        # Breite wird pro Foto bestimmt. args.breite ist nur die Referenzbreite.
        self.breite_eff = float(args.breite)
        self.breite_roh = float(args.breite)
        self.breite_methode = 'Referenz/Fallback'

    def _fit_kanten(self, pl, pr):
        if len(pl) < 4 or len(pr) < 4:
            raise ValueError('Fahrbahnkanten nicht gefunden (kein Straßenbild oder Fahrbahn nicht mittig?)')

        def fit(p):
            p = np.array(p, float)
            a, b = np.polyfit(p[:, 1], p[:, 0], 1)
            for _ in range(3):
                res = np.abs(p[:, 0] - (a * p[:, 1] + b))
                keep = res < np.percentile(res, 80) + 5
                if keep.sum() < 3:
                    break
                a, b = np.polyfit(p[keep, 1], p[keep, 0], 1)
            return a, b

        (al, bl), (ar, br) = fit(pl), fit(pr)
        self._setze(al, bl, ar, br)

    @staticmethod
    def _run_um_mitte(row, x0, min_w=20):
        xs = np.flatnonzero(row)
        if xs.size < min_w:
            return None
        cuts = np.where(np.diff(xs) > 1)[0] + 1
        runs = [r for r in np.split(xs, cuts) if len(r) >= min_w]
        if not runs:
            return None
        for r in runs:
            if r[0] <= x0 <= r[-1]:
                return int(r[0]), int(r[-1])
        r = min(runs, key=lambda z: abs((z[0] + z[-1]) / 2 - x0))
        return int(r[0]), int(r[-1])

    def _kanten_aus_ki(self):
        self.ai_mask = self.road_ai.maske(self.img)
        self.vehicle_mask = getattr(self.road_ai, 'last_vehicle_mask', None)
        m = cv2.morphologyEx(
            self.ai_mask.astype(np.uint8),
            cv2.MORPH_CLOSE,
            np.ones((5, 31), np.uint8)
        )
        step = max(8, self.H // 110)
        pl, pr = [], []
        x0 = self.W // 2
        for y in range(int(self.H * 0.34), self.H - 15, step):
            run = self._run_um_mitte(m[y].astype(bool), x0, max(20, self.W // 80))
            if run is None:
                continue
            l, r = run
            if l > 4 and r < self.W - 5:
                pl.append((l, y))
                pr.append((r, y))
        self._fit_kanten(pl, pr)
        self.randmethode = 'SegFormer road/sidewalk'

    def kanten(self):
        if self.road_ai is not None:
            try:
                self._kanten_aus_ki()
                return
            except Exception as e:
                print(f'[KI-Fahrbahn Fallback: {e}] ', end='', flush=True)
                self.ai_mask = None

        hsv = cv2.cvtColor(self.img, cv2.COLOR_BGR2HSV).astype(int)
        asphalt = (hsv[..., 1] < 65) & (hsv[..., 2] > 50)
        step = max(8, self.H // 107)
        miss_max = max(15, self.W // 95)
        pl, pr = [], []

        for y in range(int(self.H * 0.40), self.H - 20, step):
            row = asphalt[y]
            x0 = self.W // 2
            if row[max(0, x0 - 50):min(self.W, x0 + 50)].mean() <= 0.45:
                continue

            def walk(dx):
                x = x0
                miss = 0
                while 0 < x < self.W - 1:
                    if row[x]:
                        miss = 0
                    else:
                        miss += 1
                        if miss > miss_max:
                            return x - dx * miss_max
                    x += dx
                return None

            l, r = walk(-1), walk(1)
            if l is not None and l > 5:
                pl.append((l, y))
            if r is not None and r < self.W - 6:
                pr.append((r, y))

        self._fit_kanten(pl, pr)
        self.randmethode = 'Material/Vegetation'

    def _setze(self, al, bl, ar, br):
        if abs(al - ar) < 1e-6:
            raise ValueError('Fahrbahnkanten laufen nicht zusammen')
        yh = (br - bl) / (al - ar)
        pitch = math.degrees(math.atan((self.H / 2 - yh) / self.F))
        self.d = dict(al=al, bl=bl, ar=ar, br=br, yh=yh, xh=al * yh + bl, pitch=pitch)
        self._waehle_naechsten_sichtbaren_bereich()

    def cam(self):
        th = math.radians(self.d['pitch'])
        den = abs(self.d['ar'] - self.d['al'])
        if den < 1e-9:
            raise ValueError('Fahrbahnkanten sind geometrisch nicht auswertbar')
        h = self.breite_eff * math.cos(th) / den
        return th, h

    def breite_aus_kamerahoehe(self, kamerahoehe):
        """Ermittelt die reale Fahrbahnbreite aus Pixelgeometrie + kalibrierter
        Kamerahöhe. Ohne mindestens eine bekannte Referenzbreite wäre die
        metrische Skalierung aus einem einzelnen Foto nicht eindeutig.
        """
        th = math.radians(self.d['pitch'])
        den = abs(self.d['ar'] - self.d['al'])
        b = float(kamerahoehe) * den / max(math.cos(th), 1e-6)
        return float(np.clip(b, self.a.min_breite, self.a.max_breite))

    def setze_breite(self, breite, methode='Pixel + Referenzhöhe'):
        self.breite_roh = float(breite)
        self.breite_eff = float(np.clip(breite, self.a.min_breite, self.a.max_breite))
        self.breite_methode = methode
        self._waehle_naechsten_sichtbaren_bereich()

    def zeile(self, D):
        th, h = self.cam()
        zc = D * math.cos(th) + h * math.sin(th)
        return self.d['yh'] + self.F * h / (zc * math.cos(th))

    def _bereich_sichtbar(self, von, bis, rand=8):
        for D in (von, bis):
            y = self.zeile(D)
            if not (rand <= y <= self.H - 1 - rand):
                return False
            xl = self.d['al'] * y + self.d['bl']
            xr = self.d['ar'] * y + self.d['br']
            if xl < rand or xr > self.W - 1 - rand or xr <= xl:
                return False
        return True

    def _waehle_naechsten_sichtbaren_bereich(self):
        ziel_von = float(self.a.von)
        laenge = float(self.a.bis - self.a.von)

        if getattr(self.a, 'kein_auto_nahe', False):
            self.von_eff = ziel_von
            self.bis_eff = ziel_von + laenge
            return

        n = int(round(AUTO_SHIFT_MAX / AUTO_SHIFT_STEP))
        for i in range(n + 1):
            v = ziel_von + i * AUTO_SHIFT_STEP
            b = v + laenge
            if self._bereich_sichtbar(v, b):
                self.von_eff, self.bis_eff = v, b
                return

        self.von_eff = ziel_von
        self.bis_eff = ziel_von + laenge

    def homographie(self):
        src, dst = [], []
        for D in (self.von_eff, self.bis_eff):
            y = self.zeile(D)
            xl = self.d['al'] * y + self.d['bl']
            xr = self.d['ar'] * y + self.d['br']
            if xl < -2 or xr > self.W + 2 or y < -2 or y > self.H + 2:
                raise ValueError(
                    f'volle Fahrbahnbreite im Bereich {self.von_eff:g}–{self.bis_eff:g} m '
                    'nicht vollständig sichtbar'
                )
            v = (self.bis_eff - D) / RES
            src += [(xl, y), (xr, y)]
            dst += [(MARGIN / RES, v), ((MARGIN + self.breite_eff) / RES, v)]
        return cv2.getPerspectiveTransform(np.float32(src), np.float32(dst))

    def entzerren(self):
        Hm = self.homographie()
        w = int(round((self.breite_eff + 2 * MARGIN) / RES))
        hgt = int(round((self.bis_eff - self.von_eff) / RES))
        top = cv2.warpPerspective(self.img, Hm, (w, hgt), flags=cv2.INTER_AREA)
        road = np.zeros((hgt, w), bool)
        road[:, int(MARGIN / RES):int((MARGIN + self.breite_eff) / RES)] = True
        return top, road, Hm

    @staticmethod
    def _persistente_grenze(flag, mid, richtung, min_start_px, persist_px):
        n = len(flag)
        x = int(np.clip(mid + richtung * min_start_px, 0, n - 1))
        while 0 <= x < n:
            if flag[x]:
                if richtung < 0:
                    a, b = max(0, x - persist_px + 1), x + 1
                else:
                    a, b = x, min(n, x + persist_px)
                if b - a >= max(3, persist_px // 2) and flag[a:b].mean() >= 0.72:
                    return x
            x += richtung
        return None

    def _materialgrenzen(self, top):
        """Fallback: erkennt Grün/Bankett sowie Pflaster/Gehsteig über einen
        dauerhaften Materialwechsel relativ zum Asphalt in der Fahrbahnmitte.
        """
        H, W = top.shape[:2]
        valid = top.sum(2) > 0
        mid = W // 2

        lab = cv2.cvtColor(top, cv2.COLOR_BGR2LAB).astype(np.float32)
        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV).astype(np.float32)
        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)

        ref_half = max(20, int(0.55 / RES))
        x0, x1 = max(0, mid-ref_half), min(W, mid+ref_half)
        ref = valid[:, x0:x1]
        ref_lab_vals = lab[:, x0:x1][ref]
        if len(ref_lab_vals) < 100:
            return None

        ref_lab = np.median(ref_lab_vals, axis=0)
        dl = (lab[..., 0] - ref_lab[0]) * 0.35
        da = lab[..., 1] - ref_lab[1]
        db = lab[..., 2] - ref_lab[2]
        color_dist = np.sqrt(dl*dl + da*da + db*db)

        blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
        texture = cv2.GaussianBlur(np.abs(gray - blur), (0, 0), 2.0)

        b, g, r = [top[..., i].astype(np.int16) for i in range(3)]
        veg = ((g > r + 7) & (g > b + 3)) | (hsv[..., 1] > 85)

        den = np.maximum(valid.sum(0), 1)
        ccol = (color_dist * valid).sum(0) / den
        tcol = (texture * valid).sum(0) / den
        vcol = (veg & valid).sum(0) / den

        tref = float(np.median(tcol[x0:x1])) + 0.2
        cref = float(np.median(ccol[x0:x1])) + 0.2

        material = (
            (vcol > 0.24) |
            ((ccol > max(10.0, cref + 7.0)) & (tcol > 1.18 * tref)) |
            (ccol > max(18.0, cref + 14.0)) |
            (tcol > 1.75 * tref)
        )

        win = max(5, int(round(0.12 / RES)))
        if win % 2 == 0:
            win += 1
        sm = np.convolve(material.astype(float), np.ones(win)/win, mode='same')
        flag = sm > 0.55

        min_start = int(round(min(1.55, max(0.85, 0.34 * self.breite_eff)) / RES))
        persist = int(round(0.22 / RES))
        xa = self._persistente_grenze(flag, mid, -1, min_start, persist)
        xb = self._persistente_grenze(flag, mid, +1, min_start, persist)

        if xa is None or xb is None or xb - xa < int(2.5 / RES):
            return None
        return xa, xb

    def _weissmarkierungsgrenzen(self, top):
        """Erkennt äußere weiße Randmarkierungen in der entzerrten Draufsicht.

        Gesucht wird nur in der Nähe der erwarteten linken/rechten Fahrbahnkante.
        Dadurch wird eine Mittellinie nicht als Fahrbahnrand verwendet.
        Rückgabe: (links_x oder None, rechts_x oder None)
        """
        H, W = top.shape[:2]
        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)

        # Weiß: hell und wenig gesättigt; Schwelle zusätzlich relativ zur Fahrbahn.
        road_x0 = int(round(MARGIN / RES))
        road_x1 = int(round((MARGIN + self.breite_eff) / RES))
        road_x0 = int(np.clip(road_x0, 0, W - 1))
        road_x1 = int(np.clip(road_x1, road_x0 + 1, W))

        vals = gray[:, road_x0:road_x1]
        med = float(np.median(vals)) if vals.size else 120.0
        white = (
            (gray >= max(145.0, med + 24.0)) &
            (hsv[..., 1] <= 78) &
            (hsv[..., 2] >= 145)
        ).astype(np.uint8)

        # Randmarkierung soll über einen relevanten Anteil der 5 m persistieren.
        vert = max(9, int(round(0.30 / RES)))
        white = cv2.morphologyEx(
            white, cv2.MORPH_CLOSE,
            np.ones((vert, 3), np.uint8)
        )
        score = white.mean(axis=0)
        win = max(5, int(round(0.08 / RES)))
        if win % 2 == 0:
            win += 1
        score = np.convolve(score, np.ones(win) / win, mode='same')

        search = int(round(WHITE_EDGE_SEARCH_M / RES))
        mid = W // 2

        def pick(a, b, side):
            a = max(0, a); b = min(W, b)
            if b <= a:
                return None
            cand = np.where(score[a:b] >= 0.22)[0]
            if cand.size == 0:
                return None
            xs = cand + a

            # Läufe bilden und den stabilsten Lauf wählen.
            cuts = np.where(np.diff(xs) > 1)[0] + 1
            runs = [r for r in np.split(xs, cuts) if len(r) >= 2]
            if not runs:
                return None

            best = None
            best_s = -1
            for r in runs:
                xc = int(round(np.mean(r)))
                # Mittellinie ausschließen: mindestens 1,0 m Abstand von der Bildmitte.
                if abs(xc - mid) < int(round(1.0 / RES)):
                    continue
                s = float(score[r].mean()) * len(r)
                if s > best_s:
                    best_s, best = s, xc
            return best

        lx = pick(road_x0 - search, road_x0 + search, 'left')
        rx = pick(road_x1 - search, road_x1 + search, 'right')
        return lx, rx

    def _schatten_und_fahrzeuge(self, top, road, Hm):
        """Schließt Fahrzeugpixel und direkt fahrzeugnahe, weiche Schatten aus.

        Wichtig: Eine allgemeine 'dunkle Fläche = Schatten'-Regel wäre zu riskant,
        weil dunkle Flickstellen sonst verschwinden könnten. Deshalb wird nur dort
        ausgeschlossen, wo SegFormer tatsächlich ein Fahrzeug erkennt.
        """
        if self.vehicle_mask is None or not np.any(self.vehicle_mask):
            return np.zeros(road.shape, dtype=bool)

        veh = cv2.warpPerspective(
            self.vehicle_mask.astype(np.uint8) * 255,
            Hm, (top.shape[1], top.shape[0]),
            flags=cv2.INTER_NEAREST
        ) > 127

        # Fahrzeugbereich leicht erweitern.
        r_small = max(3, int(round(0.18 / RES)))
        vehicle_excl = cv2.dilate(
            veh.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*r_small+1, 2*r_small+1))
        ).astype(bool)

        # Nur in der Umgebung des Fahrzeugs nach Schatten suchen.
        rz = max(5, int(round(SHADOW_VEHICLE_RADIUS_M / RES)))
        zone = cv2.dilate(
            veh.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*rz+1, 2*rz+1))
        ).astype(bool)

        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)
        bg = cv2.GaussianBlur(gray, (0, 0), max(8.0, 0.35 / RES))
        local = cv2.GaussianBlur(gray, (0, 0), max(2.0, 0.04 / RES))
        tex = cv2.GaussianBlur(
            np.abs(gray - cv2.GaussianBlur(gray, (0, 0), 0.02 / RES)),
            (0, 0), max(1.0, 0.03 / RES)
        )

        # Schatten: deutlich dunkler als sein lokales Umfeld, dabei relativ weich.
        dark = (gray < bg - 20.0) & (gray < 0.82 * np.maximum(bg, 1.0))
        tmed = np.median(tex[road]) if np.any(road) else np.median(tex)
        smooth = tex < max(7.0, 1.5 * tmed)

        sh = dark & smooth & zone & road
        sh = cv2.morphologyEx(
            sh.astype(np.uint8), cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        ).astype(bool)

        # Kleine dunkle Flecken sind eher Textur/Riss als Fahrzeugschatten.
        n, lab, st, _ = cv2.connectedComponentsWithStats(sh.astype(np.uint8), 8)
        keep = np.zeros(n, dtype=bool)
        if n > 1:
            keep[1:] = st[1:, cv2.CC_STAT_AREA] * RES**2 >= 0.06
        sh = keep[lab]

        return (vehicle_excl | sh) & road

    def verfeinern(self, iters=2):
        for _ in range(iters):
            top, road, Hm = self.entzerren()
            xa = xb = None

            # 1) Semantische Fahrbahnfläche
            if self.ai_mask is not None:
                ai_top = cv2.warpPerspective(
                    self.ai_mask.astype(np.uint8) * 255,
                    Hm,
                    (top.shape[1], top.shape[0]),
                    flags=cv2.INTER_NEAREST
                ) > 127
                frac = ai_top.mean(0)
                frac = np.convolve(frac, np.ones(17)/17, mode='same')
                col = frac > 0.48
                close_w = max(9, int(0.12 / RES))
                col = cv2.morphologyEx(
                    col[None, :].astype(np.uint8),
                    cv2.MORPH_CLOSE,
                    np.ones((1, close_w), np.uint8)
                )[0].astype(bool)

                mid = top.shape[1] // 2
                if col[mid]:
                    xa = xb = mid
                    while xa > 1 and col[xa-1]:
                        xa -= 1
                    while xb < len(col)-2 and col[xb+1]:
                        xb += 1
                    if xb - xa < int(self.a.min_breite / RES):
                        xa = xb = None

            # 2) Materialwechsel: Grün, Bankett, Pflaster, Gehsteig
            if xa is None or xb is None:
                mg = self._materialgrenzen(top)
                if mg is not None:
                    xa, xb = mg

            # 3) Äußere weiße Randmarkierung begrenzt ebenfalls die Fahrbahn.
            #    Eine gefundene Randmarkierung darf eine weiter außen liegende
            #    Asphalt-/Materialkante nach innen korrigieren.
            wl, wr = self._weissmarkierungsgrenzen(top)
            if wl is not None:
                xa = wl if xa is None else max(xa, wl)
                self.randmethode += '+weiße Randmarkierung'
            if wr is not None:
                xb = wr if xb is None else min(xb, wr)
                if '+weiße Randmarkierung' not in self.randmethode:
                    self.randmethode += '+weiße Randmarkierung'

            if xa is None or xb is None:
                break

            # Plausibilität: Fahrbahn muss im aktuellen metrischen Entwurf
            # mindestens min_breite breit bleiben.
            if xb - xa < int(self.a.min_breite / RES):
                break

            Hinv = np.linalg.inv(Hm)
            pts = []
            for x in (xa, xb):
                for v in (0, top.shape[0]-1):
                    p = Hinv @ np.array([x, v, 1.0])
                    pts.append(p[:2] / p[2])

            (lx0, ly0), (lx1, ly1), (rx0, ry0), (rx1, ry1) = pts
            if abs(ly1 - ly0) < 1e-6 or abs(ry1 - ry0) < 1e-6:
                break
            al = (lx1 - lx0) / (ly1 - ly0)
            ar = (rx1 - rx0) / (ry1 - ry0)
            self._setze(al, lx0 - al * ly0, ar, rx0 - ar * ry0)



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


def _linien(norm, road, lo=85, hi=97, minlen=0.6):
    """Stufe 2: lange, feine Risse über Linienfilter (Sato) mit Hysterese."""
    r = sato(norm, sigmas=[1.5, 2.5, 3.5], black_ridges=True)
    loc = cv2.GaussianBlur(r, (0, 0), 40)
    rn = r / (loc + np.percentile(r[road], 50))
    kern = int(EDGE_KEEP / RES) * 2 + 1
    inner = cv2.erode(road.astype(np.uint8), np.ones((1, kern), np.uint8)).astype(bool)
    if inner.sum() < 100:
        return np.zeros_like(road, dtype=bool)
    m = apply_hysteresis_threshold(np.where(inner, rn, 0), np.percentile(rn[inner], lo), np.percentile(rn[inner], hi))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8))
    out = np.zeros_like(m)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) * RES < minlen * 0.7: continue
        sl = (slice(y, y + h), slice(x, x + w)); comp = lab[sl] == i
        if skeletonize(comp).sum() * RES * 1.12 < minlen: continue
        out[sl] |= comp
    return out


def erkennen(top, road, exclude=None):
    eval_road = road.copy()
    if exclude is not None:
        eval_road &= ~exclude.astype(bool)
    if eval_road.sum() < 1000:
        eval_road = road.copy()

    gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bg = cv2.GaussianBlur(cv2.medianBlur(gray.astype(np.uint8), 5), (0, 0), 200)
    norm = gray - bg
    med = np.median(norm[eval_road]); mad = np.median(np.abs(norm[eval_road] - med)) + 1e-3

    # Flickstellen: großflächig dunkler, glatt, mit Abstand zur Fahrbahnkante
    smooth = cv2.GaussianBlur(norm, (0, 0), 6)
    tex = cv2.GaussianBlur(np.abs(norm - cv2.GaussianBlur(norm, (0, 0), 3)), (0, 0), 8)
    tmed = np.median(tex[eval_road])
    kern = int(EDGE_KEEP / RES) * 2 + 1
    inner = cv2.erode(eval_road.astype(np.uint8), np.ones((1, kern), np.uint8)).astype(bool)
    patch = (smooth < med - 2.2 * mad) & (tex < 1.3 * tmed) & inner
    patch = cv2.morphologyEx(patch.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    patch = cv2.morphologyEx(patch, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(patch)
    keep = st[:, cv2.CC_STAT_AREA] * RES ** 2 >= 0.04; keep[0] = False
    pmask = keep[lab]

    # Stufe 1: dunkle, schmale Strukturen (Black-Hat) → Längs-, Quer-, Netzrisse
    g8 = np.clip(norm - norm.min(), 0, 255).astype(np.uint8)
    bh = cv2.morphologyEx(g8, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)))
    bh = cv2.GaussianBlur(bh, (0, 0), 1.2).astype(np.float32)
    thr = np.percentile(bh[eval_road], 96.5)
    cand = (bh > thr) & eval_road & ~cv2.dilate(pmask.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    cand = cv2.morphologyEx(cand.astype(np.uint8), cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(cand)
    risse = []; cmask = np.zeros_like(road)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) * RES < 0.25: continue
        sl = (slice(y, y + h), slice(x, x + w)); comp = lab[sl] == i
        skc = _prune(skeletonize(comp))
        if skc.sum() == 0: continue
        L = skc.sum() * RES * 1.12
        if L < 0.40 or a * RES ** 2 / L > 0.035: continue
        ang, elong = _richtung(skc)
        k = cv2.filter2D(skc.astype(np.uint8), -1, np.ones((3, 3), np.float32))
        verzw = int(((k >= 4) & skc).sum())
        typ = 'Netzriss' if (w * RES > 0.5 and h * RES > 0.5 and verzw >= 6 and elong < 4) else ('Längsriss' if ang <= 45 else 'Querriss')
        sk = np.zeros_like(road); sk[sl] = skc
        c = dict(typ=typ, laenge=L, sk=sk)
        if typ == 'Netzriss':
            pad = 40
            y0, x0 = max(y - pad, 0), max(x - pad, 0)
            y1, x1 = min(y + h + pad, road.shape[0]), min(x + w + pad, road.shape[1])
            lc = (lab[y0:y1, x0:x1] == i).astype(np.uint8)
            lc = cv2.morphologyEx(lc, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
            area = np.zeros_like(road); area[y0:y1, x0:x1] = binary_fill_holes(lc); area &= eval_road
            c['flaeche_mask'] = area
        risse.append(c); cmask[sl] |= comp

    # Stufe 2: lange feine Risse nur dort ergänzen, wo noch nichts erkannt wurde
    taken = cv2.dilate((cmask | pmask).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))).astype(bool)
    for c in risse:
        if c['typ'] == 'Netzriss': taken |= c['flaeche_mask']
    extra = _linien(norm, eval_road) & ~taken
    n, lab, st, _ = cv2.connectedComponentsWithStats(extra.astype(np.uint8))
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) * RES < 0.3: continue
        sl = (slice(y, y + h), slice(x, x + w))
        skc = _prune(skeletonize(lab[sl] == i)); L = skc.sum() * RES * 1.12
        if L < 0.40: continue
        ang, _ = _richtung(skc)
        sk = np.zeros_like(road); sk[sl] = skc
        risse.append(dict(typ='Längsriss' if ang <= 45 else 'Querriss', laenge=L, sk=sk))
    return pmask, risse


def kalibriere_kamerahoehe(geladen, args, road_ai, cache_path=None):
    """Bestimmt einmalig den metrischen Maßstab.

    Pixel alleine liefern keine absolute Breite. Deshalb wird aus einem Foto
    mit bekannter Fahrbahnbreite (Standard IMG_1663 = --breite 5,80 m)
    die Kamerahöhe bestimmt. Solange Smartphone/Linse/Aufnahmehöhe gleich
    bleiben, kann daraus die Breite der folgenden Fotos aus deren Pixelgeometrie
    berechnet werden.
    """
    ziel = str(args.referenzfoto).lower()
    kandidat = None
    for p, img, meta in geladen:
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        if stem == ziel:
            kandidat = (p, img, meta)
            break

    if kandidat is None:
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as fh:
                    c = json.load(fh)
                h = float(c['kamerahoehe_m'])
                if 0.8 <= h <= 2.5:
                    print(
                        f'  Breitenkalibrierung aus Cache: Kamerahöhe {h:.2f} m '
                        f'({os.path.basename(cache_path)})'
                    )
                    return h
            except Exception:
                pass
        print(f'  ! Referenzfoto {args.referenzfoto} nicht gefunden – Fahrbahnbreite bleibt Fallback {args.breite:.2f} m.')
        return None

    p, img, meta = kandidat
    try:
        f = Foto(p, img, meta, args, road_ai=road_ai)
        f.breite_eff = float(args.breite)
        f.kanten()
        f.verfeinern(iters=2)
        th, h = f.cam()
        if not (0.8 <= h <= 2.5):
            raise ValueError(f'unplausible Kamerahöhe {h:.2f} m')
        print(
            f'  Breitenkalibrierung: {f.name} = {args.breite:.2f} m '
            f'→ Kamerahöhe {h:.2f} m'
        )
        if cache_path:
            try:
                with open(cache_path, 'w', encoding='utf-8') as fh:
                    json.dump({
                        'referenzfoto': f.name,
                        'referenzbreite_m': float(args.breite),
                        'kamerahoehe_m': float(h),
                        'hinweis': 'Gültig nur bei gleicher Kamera/Linse/Aufnahmehöhe.'
                    }, fh, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f'  ! Kalibrierung konnte nicht gespeichert werden: {e}')
        return h
    except Exception as e:
        print(f'  ! Breitenkalibrierung fehlgeschlagen ({e}) – Breite bleibt Fallback {args.breite:.2f} m.')
        return None


def auswerten(foto, args, kamera_ref_h=None, breite_hist=None):
    foto.kanten()
    foto.verfeinern()

    # ------------------------------------------------------
    # Variable Fahrbahnbreite aus Pixelgeometrie
    # ------------------------------------------------------
    if not args.keine_breite_auto and kamera_ref_h is not None:
        b1 = foto.breite_aus_kamerahoehe(kamera_ref_h)

        # Mit der neuen metrischen Breite nochmals entzerren und Rand schärfen.
        foto.setze_breite(b1)
        foto.verfeinern(iters=1)
        b2 = foto.breite_aus_kamerahoehe(kamera_ref_h)

        # Leichte robuste Glättung gegen einzelne fehlerhafte Randpixel.
        if breite_hist is not None:
            probe = list(breite_hist[-2:]) + [b2]
            b = float(np.median(probe))
            breite_hist.append(b2)
        else:
            b = b2

        foto.setze_breite(
            float(np.clip(b, args.min_breite, args.max_breite)),
            methode=f'Pixel + {args.referenzfoto} ({args.breite:.2f} m)'
        )
    else:
        foto.setze_breite(
            float(np.clip(args.breite, args.min_breite, args.max_breite)),
            methode='feste Referenzbreite'
        )

    top, road, Hm = foto.entzerren()

    # ------------------------------------------------------
    # Fahrzeuge und deren Schatten aus der Schadensauswertung ausschließen
    # ------------------------------------------------------
    exclude = foto._schatten_und_fahrzeuge(top, road, Hm)
    valid_road = road & ~exclude

    pmask, risse = erkennen(top, road, exclude=exclude)

    einzel = [c for c in risse if c['typ'] != 'Netzriss']
    netz = [c for c in risse if c['typ'] == 'Netzriss']

    sk = np.zeros_like(road)
    for c in einzel:
        sk |= c['sk']

    r = int(round(CRACK_W / 2 / RES))
    buf = cv2.dilate(
        sk.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    ).astype(bool) & valid_road

    nmask = np.zeros_like(road)
    for c in netz:
        nmask |= c['flaeche_mask']
    nmask &= valid_road
    pmask &= valid_road

    union = (buf | nmask | pmask) & valid_road

    A = foto.breite_eff * (foto.bis_eff - foto.von_eff)
    lr = sum(c['laenge'] for c in einzel if c['typ'] == 'Längsriss')
    qr = sum(c['laenge'] for c in einzel if c['typ'] == 'Querriss')

    th, h = foto.cam()

    res = dict(
        datei=foto.name,
        zeit=foto.meta['zeit'],
        lat=foto.meta['lat'],
        lon=foto.meta['lon'],
        gps_genauigkeit=foto.meta['gps_genauigkeit'],
        fahrbahnbreite=foto.breite_eff,
        fahrbahnbreite_roh=foto.breite_roh,
        breitenmethode=foto.breite_methode,
        bezugsflaeche=A,
        bereich_von=foto.von_eff,
        bereich_bis=foto.bis_eff,
        randmethode=foto.randmethode,
        schatten_ausgeschlossen_m2=exclude.sum() * RES ** 2,
        laengsriss=lr,
        querriss=qr,
        einzelriss_laenge=lr + qr,
        einzelriss_flaeche_regel=(lr + qr) * CRACK_W,
        einzelriss_flaeche=buf.sum() * RES ** 2,
        netzriss_flaeche=nmask.sum() * RES ** 2,
        flickflaeche=pmask.sum() * RES ** 2,
        schlagloecher=None,
        schadflaeche=union.sum() * RES ** 2,
        schadanteil=union.sum() * RES ** 2 / A * 100 if A else 0,
        kamerahoehe=h,
        neigung=math.degrees(th)
    )
    res['klasse'] = klasse(res['schadanteil'])
    return res, top, road, pmask, risse, buf, nmask, Hm



def overlay_draufsicht(top, road, pmask, risse, buf, nmask):
    out = top.copy(); out[~road] = (out[~road] * 0.35).astype(np.uint8)
    tint = out.copy(); tint[buf] = (0.55 * tint[buf] + 0.45 * np.array((80, 80, 255))).astype(np.uint8)
    out = cv2.addWeighted(out, 0.6, tint, 0.4, 0)
    nm = out.copy(); nm[nmask] = NETZ; out = cv2.addWeighted(out, 0.6, nm, 0.4, 0)
    cnt, _ = cv2.findContours(nmask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(out, cnt, -1, NETZ, 4)
    pm = out.copy(); pm[pmask] = PATCH; out = cv2.addWeighted(out, 0.5, pm, 0.5, 0)
    for c in risse:
        out[cv2.dilate(c['sk'].astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)] = COL[c['typ']]
    return out


def overlay_foto(img, Hm, top, road, pmask, risse, buf, nmask):
    Hi = np.linalg.inv(Hm); hh, ww = img.shape[:2]
    lay = np.zeros_like(top); a = np.zeros(road.shape, np.uint8)
    lay[road] = (180, 60, 160); a[road] = 45
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
    polys = [(r, ax.streifen(r['von'], r['bis'], r.get('fahrbahnbreite', breite))) for r in S]
    s_end = S[-1]['bis'] + 10
    max_b = max([r.get('fahrbahnbreite', breite) for r in S] + [breite])
    ctx = ax.streifen(-10, s_end, max_b, n=max(6, int(s_end / 2)))
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
           f'<polygon points="{pp(ax.streifen(0, S[0]["von"], S[0].get("fahrbahnbreite", breite)))}" fill="url(#hatch)" stroke="var(--line)"/>']
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
        p, d = ax.punkt(s); p = p - np.array([-d[1], d[0]]) * (max_b / 2 + 1.2)
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
        rb = r.get('fahrbahnbreite', breite)
        ps = ax.streifen(r['von'], r['bis'], rb); ps = ps + [ps[0]]
        ring = [[round(ax.ll(p)[1], 7), round(ax.ll(p)[0], 7)] for p in ps]
        feats.append({'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
                      'properties': {'art': 'abschnitt', 'abschnitt': f"{r['nr']:02d}", 'station_von_m': r['von'], 'station_bis_m': r['bis'],
                                     'foto': r['datei'], 'aufnahme': r['zeit'], 'zustandsklasse': r['klasse'],
                                     'schadanteil_prozent': round(r['schadanteil'], 1), 'schadflaeche_m2': round(r['schadflaeche'], 2),
                                     'laengsrisse_m': round(r['laengsriss'], 1), 'querrisse_m': round(r['querriss'], 1),
                                     'netzrisse_m2': round(r['netzriss_flaeche'], 2), 'flickstellen_m2': round(r['flickflaeche'], 2),
                                     'bezugsflaeche_m2': r['bezugsflaeche'], 'fahrbahnbreite_m': round(r.get('fahrbahnbreite', breite), 2)}})
    for r in S:
        if r['lat'] is not None:
            feats.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [round(r['lon'], 7), round(r['lat'], 7)]},
                          'properties': {'art': 'foto', 'foto': r['datei']}})
    return {'type': 'FeatureCollection', 'name': titel, 'features': feats}


def kml(ax, S, breite, titel):
    k = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
         f'<name>{html.escape(titel)} – Zustandsklassen</name>',
         '<description>RoadAI-Prototyp. Zustandsklasse nach Schadanteil: 1 = 0–10 %, 2 = 10–20 %, 3 = 20–30 %, 4 = 30–40 %, 5 = ab 40 %. Keine Bewertung nach RVS.</description>']
    for c in range(1, 6):
        h = KLASSE_FARBE[c]; bgr = h[5:7] + h[3:5] + h[1:3]
        k.append(f'<Style id="k{c}"><LineStyle><color>ff{bgr}</color><width>1</width></LineStyle><PolyStyle><color>d0{bgr}</color></PolyStyle></Style>')
    k.append('<Folder><name>Abschnitte</name>')
    for r in S:
        rb = r.get('fahrbahnbreite', breite)
        ps = ax.streifen(r['von'], r['bis'], rb); ps = ps + [ps[0]]
        coords = ' '.join(f"{ax.ll(p)[1]:.7f},{ax.ll(p)[0]:.7f},0" for p in ps)
        k.append(f'<Placemark><name>Abschnitt {r["nr"]:02d} · Klasse {r["klasse"]}</name><styleUrl>#k{r["klasse"]}</styleUrl>'
                 f'<description>Station {r["von"]:g}–{r["bis"]:g} m, Foto {r["datei"]}. Fahrbahnbreite {de(r.get("fahrbahnbreite", breite), 2)} m. Schadanteil {de(r["schadanteil"])} %, Schadfläche {de(r["schadflaeche"], 2)} m².</description>'
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
var abschnitte=L.geoJSON(DATEN,{filter:function(f){return f.properties.art==='abschnitt';},
  style:function(f){return {color:'#fff',weight:1.5,fillColor:FARBE[f.properties.zustandsklasse],fillOpacity:0.72};},
  onEachFeature:function(f,layer){var p=f.properties,k=p.zustandsklasse;
    layer.bindPopup('<div class="popup"><b>Abschnitt '+p.abschnitt+' · Station '+p.station_von_m+'–'+p.station_bis_m+' m</b><br>'+
      '<span class="badge" style="background:'+FARBE[k]+';color:'+TEXT[k]+'">Klasse '+k+' · '+de(p.schadanteil_prozent,1)+' %</span><table>'+
      '<tr><td>Längsrisse</td><td>'+de(p.laengsrisse_m,1)+' m</td></tr><tr><td>Querrisse</td><td>'+de(p.querrisse_m,1)+' m</td></tr>'+
      '<tr><td>Netzrisse</td><td>'+de(p.netzrisse_m2,2)+' m²</td></tr><tr><td>Flickstellen</td><td>'+de(p.flickstellen_m2,2)+' m²</td></tr>'+
      '<tr><td>Schadfläche</td><td>'+de(p.schadflaeche_m2,2)+' m²</td></tr><tr><td>Foto</td><td>'+p.foto+'</td></tr></table></div>');
    layer.bindTooltip(p.abschnitt,{permanent:true,direction:'center',className:'seglabel'});
    layer.on('mouseover',function(){layer.setStyle({weight:3,color:'#111'});}); layer.on('mouseout',function(){abschnitte.resetStyle(layer);});}}).addTo(map);
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


def bericht_html(args, S, extra, fehler, ax, plan, prefix, n_ohne_gps):
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
    breiten = [float(r.get('fahrbahnbreite', args.breite)) for r in S]
    if max(breiten) - min(breiten) < 0.05:
        breite_txt = f"Fahrbahnbreite {de(np.mean(breiten), 2)} m"
    else:
        breite_txt = f"Fahrbahnbreite variabel {de(min(breiten), 2)}–{de(max(breiten), 2)} m"
    lz = lambda p: f"z{klasse(p)}"

    rows = ''.join(f"<tr><td class='mono'>{r['nr']:02d}</td><td class='mono'>{r['von']:g}–{r['bis']:g} m</td><td class='num'>{de(r.get('fahrbahnbreite', args.breite), 2)}</td><td class='num'>{de(r['laengsriss'])}</td>"
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
                f"<div class='pics'><figure><img src='bilder/{esc(r['datei'])}_foto.jpg' alt='Foto {esc(r['datei'])}' loading='lazy'><figcaption>Foto mit Auswertefläche {r.get('bereich_von', args.von):g}–{r.get('bereich_bis', args.bis):g} m</figcaption></figure>"
                f"<figure><button class='zoom' type='button' data-n='{esc(r['datei'])}' data-t='{esc(titel)}'><img src='bilder/{esc(r['datei'])}_draufsicht_klein.jpg' alt='Draufsicht {esc(r['datei'])}' loading='lazy'><span>⤢ Groß</span></button>"
                f"<figcaption>Draufsicht {de(r.get('fahrbahnbreite', args.breite), 2)} × {de(r.get('bereich_bis', args.bis) - r.get('bereich_von', args.von), 2)} m · antippen zum Vergrößern</figcaption></figure></div>"
                f"<dl class='kv'><div><dt>Längsrisse</dt><dd>{de(r['laengsriss'])} m</dd></div><div><dt>Querrisse</dt><dd>{de(r['querriss'])} m</dd></div>"
                f"<div><dt>Netzrisse (Fläche)</dt><dd>{de(r['netzriss_flaeche'], 2)} m²</dd></div><div><dt>Einzelrisse L × 0,5 m</dt><dd>{de(r['einzelriss_flaeche_regel'], 2)} m²</dd></div>"
                f"<div><dt>Einzelrisse ohne Überlappung</dt><dd>{de(r['einzelriss_flaeche'], 2)} m²</dd></div><div><dt>Flickstellen</dt><dd>{de(r['flickflaeche'], 2)} m²</dd></div>"
                f"<div><dt>Schlaglöcher</dt><dd>nicht bestimmt</dd></div><div><dt>Schadfläche (Union)</dt><dd>{de(r['schadflaeche'], 2)} m²</dd></div>"
                f"<div><dt>Fahrbahnbreite</dt><dd>{de(r.get('fahrbahnbreite', args.breite), 2)} m</dd></div>"
                f"<div><dt>Fahrzeug/Schatten ausgeschlossen</dt><dd>{de(r.get('schatten_ausgeschlossen_m2', 0), 2)} m²</dd></div>"
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
<p class="lead" style="margin:0 0 18px">Jeder Abschnitt ist nach seinem Schadanteil eingefärbt. Die Abschnitte liegen auf der Fahrbahnachse aus den GPS-Standorten der Fotos. Ein Klick springt zur Detailansicht. Das Luftbild dazu zeigt die Datei <b>{esc(prefix)}Karte.html</b> (basemap.at).</p>
<div class="planwrap">{plan}</div><div class="classes">{classes}</div>
<p class="scale">Klassen nach Projektvorgabe: 1 = 0–10 %, 2 = 10–20 %, 3 = 20–30 %, 4 = 30–40 %, 5 = ab 40 % Schadanteil. Keine Note nach RVS 13.01.15.</p></section>"""

    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(args.titel)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{BERICHT_CSS}</style></head><body><div class="wrap">
<header><div class="eyebrow">RoadAI · Prototyp v{VERSION} · Hybrid: Fahrbahn-KI + klassische Schadensanalyse</div>
<h1>{esc(args.titel)}</h1>
<p class="lead">Automatische Auswertung von {n} Fotos{(' in ' + esc(args.ort)) if args.ort else ''}. Jedes Foto wird einzeln kalibriert. Ziel ist der möglichst nahe Bereich {args.von:g}–{args.bis:g} m vor der Kamera; wenn die volle Fahrbahnbreite dort nicht sichtbar ist, wird der 5-m-Bereich automatisch nur so weit wie nötig nach vorne verschoben.</p>
<div class="meta"><span>{esc(datum)}</span><span>{esc(S[0].get('modell', '') or '')}</span><span>{breite_txt}</span><span>{pos}</span><span>Erstellt {datetime.datetime.now():%d.%m.%Y %H:%M}</span></div>
<div class="stats"><div><b>{de(anteil)} %</b><span>Schadanteil gesamt · Klasse {klasse(anteil)}</span></div>
<div><b>{de(tot_a)} m²</b><span>Schadfläche ohne Doppelzählung</span></div>
<div><b>{de(tl + tq, 0)} m</b><span>Längs- und Querrisse</span></div>
<div><b>{de(tot_n, 1)} m²</b><span>Netzrisse · dazu {de(tot_p, 2)} m² Flickstellen</span></div></div></header>
<p class="note"><b>Prototyp, Zustandsklassen nach Projektvorgabe, nicht nach RVS.</b> Die Werte stammen aus klassischer Bildverarbeitung und sind nicht gegen Messungen vor Ort geprüft. Grobe Oberflächentextur wird teils als Riss erkannt, feine Risse in größerer Entfernung (über ca. 6 m) teils übersehen.</p>
{hinweise}
{karte}
<section><h2>Vogelperspektive der Strecke</h2>
<p class="lead" style="margin:0 0 18px">Die entzerrten Abschnitte aneinandergereiht, Fahrtrichtung nach oben. Station = Meter ab dem ersten Foto.</p>
<ul class="legend"><li><i style="background:#e62828"></i>Längsriss</li><li><i style="background:#ff8c00"></i>Querriss</li><li><i style="background:#aa1e3c"></i>Netzriss (Fläche)</li><li><i style="background:#1e78e6"></i>Flickstelle</li><li><i style="background:#ff8080"></i>Einflussfläche Längs-/Querriss (2 × 0,25 m)</li></ul>
<div class="overview"><div><div class="strip"><img src="bilder/strecke_vogelperspektive_klein.jpg" alt="Draufsicht der gesamten Strecke"><div class="labels">{labels}</div></div>
<p class="dir">↑ Fahrtrichtung · Abschnittslänge {de(lab_len, 2)} m · Fahrbahnbreite je Foto automatisch</p></div>
<div style="min-width:0"><div class="tablewrap"><table><thead><tr><th>Abschn.</th><th>Station</th><th class="num">Breite m</th><th class="num">Längsrisse m</th><th class="num">Querrisse m</th><th class="num">Netzrisse m²</th><th class="num">Flicken m²</th><th class="num">Schaden m²</th><th class="num">Anteil</th><th class="num">Zustand</th></tr></thead>
<tbody>{rows}</tbody><tfoot><tr><td colspan="3">Summe {S[0]['von']:g}–{S[-1]['bis']:g} m</td><td class="num">{de(tl)}</td><td class="num">{de(tq)}</td><td class="num">{de(tot_n, 2)}</td><td class="num">{de(tot_p, 2)}</td><td class="num">{de(tot_a, 2)}</td><td class="num">{de(anteil)} %</td><td class="num"><span class="pct {lz(anteil)}">Klasse {klasse(anteil)}</span></td></tr></tfoot></table></div>
<p class="scale">Die Tabelle gibt es als {esc(prefix)}Schaeden.csv für Excel oder Google Sheets.</p></div></div></section>
<section><h2>Abschnitte im Detail</h2><p class="lead" style="margin:0 0 18px">Links das Originalfoto mit der zurückprojizierten Auswertung, rechts die entzerrte Draufsicht.</p><div class="cards">{cards}</div></section>
<div class="cols"><section><h2>Methode</h2><ul>
<li><b>Variable Fahrbahnbreite:</b> Das Referenzfoto <b>{esc(args.referenzfoto)}</b> liefert mit {de(args.breite, 2)} m die metrische Skalierung/Kamerahöhe. In den Folgebildern wird die Breite aus der Pixelgeometrie der beiden Fahrbahnränder neu berechnet und auf maximal {de(args.max_breite, 1)} m begrenzt.</li>
<li><b>Auswertebereich:</b> Ziel {args.von:g}–{args.bis:g} m vor der Kamera; automatische Verschiebung nach vorne, falls die volle Fahrbahnbreite im Nahbereich abgeschnitten wäre. Entzerrung auf 5 mm je Pixel.</li>
<li><b>Schadensarten</b> (Begriffe nach RVS 13.01.11): Längsriss, Querriss (Richtung bis/über 45° zur Fahrbahnachse), Netzriss, Flickstelle.</li>
<li><b>Risserkennung in zwei Stufen:</b> dunkle schmale Strukturen und Rissnetze, ergänzt durch einen Linienfilter für lange, feine Risse.</li>
<li><b>Flächen:</b> Einzelrisse = Länge × 0,50 m (Streifen 0,25 m je Seite); Netzrisse = umschlossene Fläche; Flickstellen = Fläche; Gesamtschaden ohne Doppelzählung.</li></ul></section>
<section><h2>Grenzen</h2><ul>
<li><b>Zustandsklassen</b> in 10-%-Stufen nach Projektvorgabe, keine RVS-Note.</li>
<li><b>Schlaglöcher</b> sind aus Fotos allein nicht sicher von dunklen Flickstellen zu trennen (Tiefeninformation/LiDAR nötig).</li>
<li><b>Fahrbahnrand:</b> Hybrid aus semantischer Fahrbahnerkennung, Materialwechsel und äußeren weißen Randmarkierungen. Neben Grün/Bankett können damit auch Pflaster, Gehsteige/Borde und weiße Außenmarkierungen die Fahrbahn begrenzen. Nur ein {EDGE_KEEP * 100:.0f}-cm-Schutzstreifen bleibt gegen Randartefakte.</li>
<li><b>Fahrzeugschatten:</b> Erkannte Fahrzeuge werden semantisch maskiert; nur direkt fahrzeugnahe, großflächige weiche Schatten werden aus der Schadensdetektion ausgeschlossen, damit dunkle Flickstellen nicht pauschal als Schatten entfernt werden.</li>
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
def main():
    ap = argparse.ArgumentParser(description='RoadAI – Straßenschäden aus Fotos auswerten, Bericht und Luftbildkarte erzeugen.')
    hier = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument('fotos', nargs='?', default=os.path.join(hier, 'bilder'),
                    help='Ordner mit den JPG-Fotos (Standard: Unterordner "bilder" neben diesem Programm)')
    ap.add_argument('--ausgabe', default=os.path.join(hier, 'Ergebnis'),
                    help='Zielordner (Standard: Unterordner "Ergebnis" neben diesem Programm)')
    ap.add_argument('--titel', default=None, help='Titel des Berichts')
    ap.add_argument('--ort', default='', help='Ort, erscheint im Bericht')
    ap.add_argument('--breite', type=float, default=5.80,
                    help='Referenzbreite am Kalibrierfoto in m (Standard 5,80)')
    ap.add_argument('--referenzfoto', default='IMG_1663',
                    help='Foto mit bekannter Referenzbreite (Standard IMG_1663)')
    ap.add_argument('--min-breite', type=float, default=MIN_ROAD_WIDTH,
                    help=f'Plausibilitäts-Untergrenze für automatische Breite (Standard {MIN_ROAD_WIDTH:.2f} m)')
    ap.add_argument('--max-breite', type=float, default=MAX_ROAD_WIDTH,
                    help=f'Harte Obergrenze für automatische Breite (Standard {MAX_ROAD_WIDTH:.2f} m)')
    ap.add_argument('--keine-breite-auto', action='store_true',
                    help='Fahrbahnbreite nicht aus Pixelgeometrie ableiten; --breite fix verwenden')
    ap.add_argument('--von', type=float, default=2.0, help='Ziel-Beginn des Auswertebereichs vor der Kamera in m (Standard 2,0)')
    ap.add_argument('--bis', type=float, default=7.0, help='Ziel-Ende des Auswertebereichs vor der Kamera in m (Standard 7,0)')
    ap.add_argument('--abstand', type=float, default=None, help='Fotoabstand in m (Standard = bis − von)')
    ap.add_argument('--ohne', nargs='*', default=[], help='Fotos, die nicht zur Strecke gehören (z. B. Kalibrierfoto IMG_1663)')
    ap.add_argument('--webhook', default=None, help='URL, an die die Ergebnisse als JSON gesendet werden (z. B. Zapier Catch Hook)')
    ap.add_argument('--kein-auto-nahe', action='store_true',
                    help='Nahbereich nicht automatisch verschieben, wenn die volle Fahrbahnbreite abgeschnitten ist')
    ap.add_argument('--ohne-ki-fahrbahn', action='store_true',
                    help='SegFormer-Fahrbahnerkennung deaktivieren; nur klassische Materialerkennung verwenden')
    args = ap.parse_args()
    args.max_breite = min(float(args.max_breite), MAX_ROAD_WIDTH)
    if args.min_breite <= 0 or args.min_breite >= args.max_breite:
        sys.exit('Ungültige Breiten-Grenzen: --min-breite muss > 0 und < --max-breite sein.')
    args.breite = float(np.clip(args.breite, args.min_breite, args.max_breite))
    if args.abstand is None:
        args.abstand = args.bis - args.von
    ordner = os.path.abspath(args.fotos)
    if args.titel is None:
        args.titel = 'Lebende Straßen – Auswertung'
    aus = os.path.abspath(args.ausgabe)
    bild_dir = os.path.join(aus, 'bilder'); os.makedirs(bild_dir, exist_ok=True)
    prefix = ''.join(c if c.isalnum() or c in '-_' else '_' for c in args.titel).strip('_') + '_'

    alle_dateien = glob.glob(os.path.join(ordner, '*'))
    heic = [p for p in alle_dateien if p.lower().endswith(('.heic', '.heif'))]
    pfade = sorted(p for p in alle_dateien if p.lower().endswith(('.jpg', '.jpeg', '.png')))
    heic_meta = {}

    if heic:
        print(f'HEIC: {len(heic)} Datei(en) gefunden – Windows erzeugt JPG-Arbeitskopien.')
        heic_cache = os.path.join(aus, '_heic_jpg_cache')
        heic_jpg, heic_meta = _heic_windows(ordner, heic, heic_cache)
        stems = {os.path.splitext(os.path.basename(x))[0].lower() for x in pfade}
        for p_ in heic_jpg:
            if os.path.splitext(os.path.basename(p_))[0].lower() not in stems:
                pfade.append(p_)
        pfade = sorted(pfade)

    if not pfade:
        sys.exit(f'Keine lesbaren Fotos gefunden in: {ordner}')

    road_ai = None
    if not args.ohne_ki_fahrbahn:
        if HAVE_ROAD_AI:
            try:
                road_ai = RoadMaskAI()
            except Exception as e:
                print(f'  ! Fahrbahn-KI nicht verfügbar ({e}) – Fallback auf klassische Randdetektion.')
        else:
            print('  ! torch/transformers nicht installiert – Fallback auf klassische Randdetektion.')

    print(f'RoadAI v{VERSION}: {len(pfade)} Fotos in {ordner}')

    geladen = []
    for p in pfade:
        try:
            img, ex = lade_foto(p)
        except Exception as e:
            print(f'  ! {os.path.basename(p)}: kann nicht gelesen werden ({e})'); continue
        meta = exif_daten(ex)
        ov = heic_meta.get(os.path.splitext(os.path.basename(p))[0].lower())
        if ov:
            if ov.get('zeit'): meta['zeit'] = ov['zeit']
            if ov.get('lat') is not None: meta['lat'] = ov['lat']
            if ov.get('lon') is not None: meta['lon'] = ov['lon']
            if ov.get('modell'): meta['modell'] = ov['modell']
        geladen.append((p, img, meta))
    geladen.sort(key=lambda t: (t[2]['zeit'] or '', os.path.basename(t[0])))   # nach Aufnahmezeit

    kamera_ref_h = None
    if not args.keine_breite_auto:
        kamera_ref_h = kalibriere_kamerahoehe(geladen, args, road_ai, os.path.join(hier, 'RoadAI_Kamerakalibrierung.json'))

    S, extra, fehler, alle = [], [], [], []
    breite_hist = []
    ohne = {o.lower() for o in args.ohne}
    for p, img, meta in geladen:
        foto = Foto(p, img, meta, args, road_ai=road_ai)
        print(f'  · {foto.name} …', end=' ', flush=True)
        try:
            res, top, road, pm, risse, buf, nm, Hm = auswerten(foto, args, kamera_ref_h, breite_hist)
        except Exception as e:
            print(f'übersprungen: {e}'); fehler.append((foto.name, str(e))); continue
        res['modell'] = meta['modell']
        ov = overlay_draufsicht(top, road, pm, risse, buf, nm)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_markiert.jpg'), ov)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_roh.jpg'), top, 88)
        imwrite(os.path.join(bild_dir, f'{foto.name}_draufsicht_klein.jpg'), cv2.resize(ov, (ov.shape[1] // 2, ov.shape[0] // 2), interpolation=cv2.INTER_AREA), 80)
        of = overlay_foto_v3(img, Hm, top, road, pm, risse, buf, nm)
        imwrite(os.path.join(bild_dir, f'{foto.name}_foto.jpg'), cv2.resize(of, (960, int(960 * of.shape[0] / of.shape[1])), interpolation=cv2.INTER_AREA), 78)
        if foto.name.lower() in ohne:
            extra.append(res); print(f'{res["schadanteil"]:.1f} % · Breite {res["fahrbahnbreite"]:.2f} m (übersprungen per --ohne)')
        else:
            S.append(res); alle.append(ov); print(f'{res["schadanteil"]:.1f} % · Breite {res["fahrbahnbreite"]:.2f} m · Klasse {res["klasse"]}')
    if not S:
        sys.exit('Kein Foto konnte ausgewertet werden.')

    lab_len = args.bis - args.von
    for k, r in enumerate(S):
        r['nr'] = k + 1; cam_s = args.abstand * k; r['von'] = round(cam_s + r.get('bereich_von', args.von), 2); r['bis'] = round(cam_s + r.get('bereich_bis', args.bis), 2)

    # Vogelperspektive der Strecke (erstes Foto unten).
    # Bei variabler Fahrbahnbreite haben die Draufsichten unterschiedliche Breiten;
    # deshalb werden sie zentriert auf die größte Bildbreite gepolstert.
    max_w = max(im.shape[1] for im in alle)
    alle_pad = []
    for im in alle:
        d = max_w - im.shape[1]
        l = d // 2; r = d - l
        alle_pad.append(cv2.copyMakeBorder(im, 0, 0, l, r, cv2.BORDER_CONSTANT, value=(235, 235, 235)))
    mos = np.vstack(alle_pad[::-1])
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
        with open(os.path.join(aus, prefix + 'Abschnitte.geojson'), 'w', encoding='utf-8') as fh:
            json.dump(gj, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(aus, prefix + 'Karte.html'), 'w', encoding='utf-8') as fh:
            fh.write(KARTE_HTML.replace('__GEOJSON__', json.dumps(gj, ensure_ascii=False)).replace('__TITEL__', html.escape(args.titel)))
        with open(os.path.join(aus, prefix + 'Zustandsklassen.kml'), 'w', encoding='utf-8') as fh:
            fh.write(kml(ax, S, args.breite, args.titel))
    else:
        print('  ! Zu wenige Fotos mit GPS – Lageplan und Luftbildkarte werden nicht erzeugt.')

    # Tabelle
    with open(os.path.join(aus, prefix + 'Schaeden.csv'), 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['Abschnitt', 'Station von (m)', 'Station bis (m)', 'Foto', 'Aufnahmezeit', 'Breite', 'Länge',
                    'Fahrbahnbreite (m)', 'Breitenmethode', 'Bezugsfläche (m²)', 'ausgeschlossener Fahrzeug/Schattenbereich (m²)',
                    'Längsrisse (m)', 'Querrisse (m)', 'Einzelrissfläche L×0,5 (m²)', 'Einzelrissfläche ohne Überlappung (m²)', 'Netzrissfläche (m²)',
                    'Flickfläche (m²)', 'Schadfläche (m²)', 'Schadanteil (%)', 'Zustandsklasse', 'Google Maps'])
        for r in S:
            w.writerow([f"{r['nr']:02d}", de(r['von'], 1), de(r['bis'], 1), r['datei'], r['zeit'],
                        de(r['lat'], 6) if r['lat'] is not None else '', de(r['lon'], 6) if r['lon'] is not None else '',
                        de(r['fahrbahnbreite'], 2), r.get('breitenmethode', ''), de(r['bezugsflaeche'], 1),
                        de(r.get('schatten_ausgeschlossen_m2', 0), 2), de(r['laengsriss']), de(r['querriss']),
                        de(r['einzelriss_flaeche_regel'], 2), de(r['einzelriss_flaeche'], 2), de(r['netzriss_flaeche'], 2),
                        de(r['flickflaeche'], 2), de(r['schadflaeche'], 2), de(r['schadanteil']), r['klasse'], maps_link(r)])
    with open(os.path.join(aus, 'ergebnisse.json'), 'w', encoding='utf-8') as fh:
        json.dump({'titel': args.titel, 'ort': args.ort, 'version': VERSION, 'abschnitte': S, 'uebersprungen': extra,
                   'nicht_ausgewertet': fehler}, fh, ensure_ascii=False, indent=1, default=float)

    # Bericht
    n_ohne_gps = sum(1 for r in S if r['lat'] is None)
    with open(os.path.join(aus, prefix + 'Bericht.html'), 'w', encoding='utf-8') as fh:
        fh.write(bericht_html(args, S, extra, fehler, ax, plan, prefix, n_ohne_gps))

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

    print(f'\nFertig. Ergebnisse in: {aus}')
    print(f'  Bericht: {prefix}Bericht.html' + (f'   ·   Luftbildkarte: {prefix}Karte.html' if ax else ''))




# ===========================================================================
# RoadAI v3.0 – robuste Prozentbewertung
# ===========================================================================
# Änderungen gegenüber v2.2:
#   • keine automatische reale Fahrbahnbreite mehr nötig
#   • kein Foto wird nur wegen abgeschnittener voller Breite verworfen
#   • Ergebnis primär als prozentual geschädigte Fahrbahnfläche
#   • Fahrbahnrand: road-KI + Grün + Bankett + Pflaster/Gehsteig/Bord
#     + äußere weiße Randmarkierung
#   • Schatten werden über Beleuchtungsnormalisierung unterdrückt;
#     echte Risslinien können im Schatten weiter erkannt werden
#   • getrennte Befahrungen bei großen GPS-/Zeitsprüngen
#   • Karte zeigt farbige Straßenabschnitte statt vermeintlich exakter
#     Fahrbahnbreiten und enthält anklickbare Ergebnisbilder
#
# WICHTIG:
# Die interne Normbreite von 5,80 ist NUR eine Rechengröße für die
# perspektivische Normalisierung und wird NICHT als gemessene reale Breite
# ausgegeben.
# ===========================================================================

VERSION = '3.0'
NORM_BREITE = 5.80
NOMINAL_ABSCHNITT_M = 5.0
AUTO_SHIFT_MAX = 12.0
GPS_ROUTE_GAP_M = 60.0
TIME_ROUTE_GAP_MIN = 15.0


def _homographie_v3(self):
    """Robuste perspektivische Entzerrung ohne Breiten-Zwang.

    RoadAI versucht zuerst den gewünschten nahen Bereich. Liegt dort ein Rand
    außerhalb des Fotos, wird der Bereich nach vorne verschoben. Bleibt die
    vollständige Breite trotzdem unsichtbar, wird der tatsächlich sichtbare
    Straßenanteil verwendet statt das Foto zu verwerfen.
    """
    D_near = float(self.von_eff)
    D_far = float(self.bis_eff)

    y_near_raw = float(self.zeile(D_near))
    y_far_raw = float(self.zeile(D_far))

    # Plausibler sichtbarer vertikaler Bereich.
    y_near = float(np.clip(y_near_raw, self.H * 0.52, self.H - 4.0))
    y_far = float(np.clip(y_far_raw, max(4.0, self.H * 0.18), y_near - 35.0))

    # Wenn beide projektiven Zeilen außerhalb des Fotos auf derselben
    # Bildkante landen würden, einen stabilen sichtbaren Trapezbereich wählen.
    min_sep = max(55.0, self.H * 0.16)
    if y_near - y_far < min_sep:
        y_near = min(self.H - 4.0, max(y_near, self.H * 0.76))
        y_far = max(4.0, y_near - max(min_sep, self.H * 0.28))

    def edge_pair(y, far=False):
        xl_raw = float(self.d['al'] * y + self.d['bl'])
        xr_raw = float(self.d['ar'] * y + self.d['br'])
        if xr_raw < xl_raw:
            xl_raw, xr_raw = xr_raw, xl_raw

        raw_span = max(xr_raw - xl_raw, 1.0)
        xl = float(np.clip(xl_raw, 2.0, self.W - 4.0))
        xr = float(np.clip(xr_raw, 3.0, self.W - 3.0))

        # Falls die geschätzten Kanten nach dem Clipping zu eng liegen,
        # zentralen sichtbaren Straßenkorridor verwenden.
        min_span = max(60.0, self.W * (0.16 if far else 0.30))
        if xr - xl < min_span:
            mid = self.W / 2.0
            half = min(self.W * (0.26 if far else 0.46), max(min_span / 2, raw_span / 2))
            xl = float(np.clip(mid - half, 2.0, self.W - min_span - 3.0))
            xr = float(np.clip(mid + half, xl + min_span, self.W - 3.0))

        vis = min(1.0, max(0.0, (xr - xl) / raw_span))
        return xl, xr, vis

    ln, rn, vis_n = edge_pair(y_near, far=False)
    lf, rf, vis_f = edge_pair(y_far, far=True)

    # Trapez muss links/rechts geordnet und nicht kollabiert sein.
    if rn <= ln + 20 or rf <= lf + 20:
        ln, rn = self.W * 0.05, self.W * 0.95
        lf, rf = self.W * 0.28, self.W * 0.72
        vis_n = vis_f = 0.5

    src = np.float32([
        (ln, y_near), (rn, y_near),
        (lf, y_far),  (rf, y_far)
    ])

    out_w = int(round((NORM_BREITE + 2 * MARGIN) / RES))
    out_h = int(round((D_far - D_near) / RES))
    out_h = max(out_h, 300)

    x0 = MARGIN / RES
    x1 = (MARGIN + NORM_BREITE) / RES
    dst = np.float32([
        (x0, out_h - 1), (x1, out_h - 1),
        (x0, 0),         (x1, 0)
    ])

    Hm = cv2.getPerspectiveTransform(src, dst)

    # Letzter Sicherheitsfallback gegen numerisch singuläre Homographien.
    if not np.all(np.isfinite(Hm)) or abs(np.linalg.det(Hm)) < 1e-12:
        src = np.float32([
            (self.W * 0.05, self.H * 0.84),
            (self.W * 0.95, self.H * 0.84),
            (self.W * 0.30, self.H * 0.48),
            (self.W * 0.70, self.H * 0.48),
        ])
        Hm = cv2.getPerspectiveTransform(src, dst)
        vis_n = vis_f = 0.5

    self.sichtanteil = float(min(vis_n, vis_f) * 100.0)
    return Hm


def _materialgrenzen_v3(self, top):
    """Erkennt dauerhafte Asphaltgrenzen.

    Als Außenbereich zählen insbesondere:
      • Grün / Böschung
      • unbefestigtes oder geschottertes Bankett
      • Pflaster / Rinne
      • Gehsteig / Bordbereich

    Die Entscheidung beruht nicht auf "dunkel = Rand", damit Schatten die
    Fahrbahn nicht künstlich verschmälern.
    """
    H, W = top.shape[:2]
    valid = top.sum(2) > 0
    mid = W // 2

    lab = cv2.cvtColor(top, cv2.COLOR_BGR2LAB).astype(np.float32)
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV).astype(np.float32)
    gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # Asphaltreferenz bewusst aus dem zentralen Korridor.
    ref_half = max(20, int(0.50 / RES))
    x0, x1 = max(0, mid-ref_half), min(W, mid+ref_half)
    refmask = valid[:, x0:x1]
    ref_lab_vals = lab[:, x0:x1][refmask]
    if len(ref_lab_vals) < 100:
        return None

    ref_lab = np.median(ref_lab_vals, axis=0)

    dl = (lab[..., 0] - ref_lab[0]) * 0.30
    da = lab[..., 1] - ref_lab[1]
    db = lab[..., 2] - ref_lab[2]
    color_dist = np.sqrt(dl*dl + da*da + db*db)

    # Mehrskalige Textur: Bankett/Pflaster ist häufig körniger/strukturierter
    # als die zusammenhängende Asphaltdecke.
    blur1 = cv2.GaussianBlur(gray, (0, 0), 1.5)
    blur2 = cv2.GaussianBlur(gray, (0, 0), 5.0)
    tex_fine = cv2.GaussianBlur(np.abs(gray - blur1), (0, 0), 2.0)
    tex_coarse = np.abs(blur1 - blur2)
    texture = tex_fine + 0.7 * tex_coarse

    b, g, r = [top[..., i].astype(np.int16) for i in range(3)]
    vegetation = (
        ((g > r + 7) & (g > b + 3)) |
        ((hsv[..., 0] >= 28) & (hsv[..., 0] <= 105) & (hsv[..., 1] > 55))
    )

    den = np.maximum(valid.sum(0), 1)
    ccol = (color_dist * valid).sum(0) / den
    tcol = (texture * valid).sum(0) / den
    vcol = (vegetation & valid).sum(0) / den
    satcol = (hsv[..., 1] * valid).sum(0) / den

    tref = float(np.median(tcol[x0:x1])) + 0.2
    cref = float(np.median(ccol[x0:x1])) + 0.2
    sref = float(np.median(satcol[x0:x1]))

    # Bankett explizit:
    # deutlicher Textur-/Materialwechsel, häufig geringe bis mittlere Sättigung.
    bankett = (
        (tcol > max(1.55 * tref, tref + 2.0)) &
        (ccol > max(7.0, cref + 4.0)) &
        (satcol < max(105.0, sref + 45.0))
    )

    # Pflaster/Gehsteig/Bord: Farbe ODER Textur dauerhaft anders als Asphalt.
    hard_side = (
        ((ccol > max(10.0, cref + 7.0)) & (tcol > 1.12 * tref)) |
        (ccol > max(18.0, cref + 14.0)) |
        (tcol > 1.90 * tref)
    )

    material = (vcol > 0.22) | bankett | hard_side

    # Einzelne Risse, Markierungen und Schatten sollen keinen Rand ergeben.
    win = max(5, int(round(0.14 / RES)))
    if win % 2 == 0:
        win += 1
    sm = np.convolve(material.astype(float), np.ones(win)/win, mode='same')
    flag = sm > 0.54

    min_start = int(round(0.82 / RES))
    persist = int(round(0.28 / RES))
    xa = self._persistente_grenze(flag, mid, -1, min_start, persist)
    xb = self._persistente_grenze(flag, mid, +1, min_start, persist)

    if xa is None or xb is None:
        return None
    if xb - xa < int(2.2 / RES):
        return None
    return xa, xb


# Methoden austauschen
Foto.homographie = _homographie_v3
Foto._materialgrenzen = _materialgrenzen_v3


def _vehicle_only_v3(foto, top, road, Hm):
    """Nur wirklich verdeckte Fahrzeugpixel ausschließen, nicht deren Schatten."""
    if foto.vehicle_mask is None or not np.any(foto.vehicle_mask):
        return np.zeros(road.shape, dtype=bool)

    veh = cv2.warpPerspective(
        foto.vehicle_mask.astype(np.uint8) * 255,
        Hm, (top.shape[1], top.shape[0]),
        flags=cv2.INTER_NEAREST
    ) > 127

    r = max(2, int(round(0.10 / RES)))
    veh = cv2.dilate(
        veh.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*r+1, 2*r+1))
    ).astype(bool)
    return veh & road


def _beleuchtung_normalisieren_v3(top, road):
    """Entfernt großflächige Helligkeitsänderungen (Fahrzeug-/Baumschatten).

    Feine lokale Strukturen wie Risse bleiben weitgehend erhalten.
    """
    gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).astype(np.float32)

    sigma = max(35.0, min(top.shape[:2]) / 9.0)
    illum = cv2.GaussianBlur(gray, (0, 0), sigma)

    med = float(np.median(illum[road])) if np.any(road) else float(np.median(illum))
    norm = gray / np.maximum(illum, 18.0) * max(med, 80.0)
    norm = np.clip(norm, 0, 255)

    # Leichter CLAHE-Ausgleich, damit Risse im Schatten sichtbar bleiben.
    n8 = norm.astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=1.7, tileGridSize=(8, 8))
    n8 = clahe.apply(n8)

    # Großflächige Schattenmaske nur zur Unterdrückung von FLICKSTELLEN.
    ratio = gray / np.maximum(illum, 1.0)
    local_tex = cv2.GaussianBlur(
        np.abs(gray - cv2.GaussianBlur(gray, (0, 0), 2.0)),
        (0, 0), 3.0
    )
    tmed = float(np.median(local_tex[road])) if np.any(road) else float(np.median(local_tex))
    shadow = (ratio < 0.78) & (local_tex < max(10.0, 1.8*tmed)) & road
    shadow = cv2.morphologyEx(
        shadow.astype(np.uint8), cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))
    ).astype(bool)

    # Nur große zusammenhängende Schattenflächen berücksichtigen.
    n, lab, st, _ = cv2.connectedComponentsWithStats(shadow.astype(np.uint8), 8)
    keep = np.zeros(n, dtype=bool)
    min_area = max(500, int(0.015 * max(int(road.sum()), 1)))
    for i in range(1, n):
        area = st[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        x, y, w, h = st[i, :4]
        touches_edge = x <= 5 or (x+w) >= top.shape[1]-5
        big = area >= 0.06 * max(int(road.sum()), 1)
        if touches_edge or big:
            keep[i] = True
    shadow = keep[lab] & road

    return n8.astype(np.float32), shadow


def erkennen_v3(top, road, exclude=None):
    """Schadenserkennung für Prozentmodus.

    Schatten werden über die Beleuchtung normalisiert. Die Schattenmaske
    unterdrückt nur flächenhafte Flickstellen; Risse werden weiterhin gesucht.
    """
    eval_road = road.copy()
    if exclude is not None:
        eval_road &= ~exclude.astype(bool)
    if eval_road.sum() < 1000:
        eval_road = road.copy()

    gray_norm, shadow = _beleuchtung_normalisieren_v3(top, eval_road)

    # Lokaler Hintergrund nach Beleuchtungsnormalisierung.
    bg = cv2.GaussianBlur(
        cv2.medianBlur(gray_norm.astype(np.uint8), 5),
        (0, 0), 85
    ).astype(np.float32)
    norm = gray_norm - bg

    med = np.median(norm[eval_road])
    mad = np.median(np.abs(norm[eval_road] - med)) + 1e-3

    # ------------------------------------------------------
    # Flickstellen
    # ------------------------------------------------------
    smooth = cv2.GaussianBlur(norm, (0, 0), 6)
    tex = cv2.GaussianBlur(
        np.abs(norm - cv2.GaussianBlur(norm, (0, 0), 3)),
        (0, 0), 8
    )
    tmed = np.median(tex[eval_road]) + 1e-3

    kern = max(3, int(EDGE_KEEP / RES) * 2 + 1)
    inner = cv2.erode(
        eval_road.astype(np.uint8),
        np.ones((1, kern), np.uint8)
    ).astype(bool)

    patch = (
        (smooth < med - 2.4 * mad) &
        (tex < 1.25 * tmed) &
        inner &
        ~shadow
    )

    patch = cv2.morphologyEx(
        patch.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8)
    )
    patch = cv2.morphologyEx(
        patch, cv2.MORPH_CLOSE, np.ones((17, 17), np.uint8)
    )

    n, lab, st, _ = cv2.connectedComponentsWithStats(patch)
    keep = np.zeros(n, dtype=bool)
    min_patch_px = max(120, int(0.0008 * max(int(eval_road.sum()), 1)))
    if n > 1:
        keep[1:] = st[1:, cv2.CC_STAT_AREA] >= min_patch_px
    pmask = keep[lab]

    # ------------------------------------------------------
    # Risse Stufe 1: Black-Hat
    # ------------------------------------------------------
    g8 = np.clip(gray_norm, 0, 255).astype(np.uint8)
    bh = cv2.morphologyEx(
        g8, cv2.MORPH_BLACKHAT,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
    )
    bh = cv2.GaussianBlur(bh, (0, 0), 1.2).astype(np.float32)

    vals = bh[eval_road]
    thr = np.percentile(vals, 96.8) if vals.size else 255
    cand = (
        (bh > thr) &
        eval_road &
        ~cv2.dilate(
            pmask.astype(np.uint8), np.ones((7, 7), np.uint8)
        ).astype(bool)
    )
    cand = cv2.morphologyEx(
        cand.astype(np.uint8), cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    )

    n, lab, st, _ = cv2.connectedComponentsWithStats(cand)
    risse = []
    cmask = np.zeros_like(road)

    for i in range(1, n):
        x, y, w, h, a = st[i]
        # Relative statt meterbasierte Mindestgröße.
        if max(w, h) < 18:
            continue
        sl = (slice(y, y+h), slice(x, x+w))
        comp = lab[sl] == i
        skc = _prune(skeletonize(comp))
        Lpx = int(skc.sum())
        if Lpx < 30:
            continue

        # Breite der Komponente relativ zur Skelettlänge: breite Flecken
        # sollen nicht als Riss klassifiziert werden.
        if a / max(Lpx, 1) > 7.5:
            continue

        ang, elong = _richtung(skc)
        k = cv2.filter2D(
            skc.astype(np.uint8), -1, np.ones((3, 3), np.float32)
        )
        branches = int(((k >= 4) & skc).sum())

        netz = (
            w > 80 and h > 80 and
            branches >= 6 and
            elong < 5.0
        )
        typ = 'Netzriss' if netz else ('Längsriss' if ang <= 45 else 'Querriss')

        sk = np.zeros_like(road)
        sk[sl] = skc
        c = dict(typ=typ, laenge_px=Lpx, sk=sk)

        if typ == 'Netzriss':
            pad = 45
            y0, x0 = max(y-pad, 0), max(x-pad, 0)
            y1, x1 = min(y+h+pad, road.shape[0]), min(x+w+pad, road.shape[1])
            lc = (lab[y0:y1, x0:x1] == i).astype(np.uint8)
            lc = cv2.morphologyEx(
                lc, cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))
            )
            area = np.zeros_like(road)
            area[y0:y1, x0:x1] = binary_fill_holes(lc)
            area &= eval_road
            c['flaeche_mask'] = area

        risse.append(c)
        cmask[sl] |= comp

    # ------------------------------------------------------
    # Risse Stufe 2: Hessian/Sato-Ersatz für längere feine Linien
    # ------------------------------------------------------
    taken = cv2.dilate(
        (cmask | pmask).astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    ).astype(bool)

    for c in risse:
        if c['typ'] == 'Netzriss':
            taken |= c['flaeche_mask']

    # _linien arbeitet noch mit der internen Normskala, was hier bewusst
    # nur als einheitliche Bildskala dient.
    extra = _linien_v3(norm, eval_road, lo=88, hi=98, minlen=0.45) & ~taken
    n, lab, st, _ = cv2.connectedComponentsWithStats(extra.astype(np.uint8))

    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) < 20:
            continue
        sl = (slice(y, y+h), slice(x, x+w))
        skc = _prune(skeletonize(lab[sl] == i))
        Lpx = int(skc.sum())
        if Lpx < 35:
            continue
        ang, _ = _richtung(skc)
        sk = np.zeros_like(road)
        sk[sl] = skc
        risse.append(
            dict(
                typ='Längsriss' if ang <= 45 else 'Querriss',
                laenge_px=Lpx,
                sk=sk
            )
        )

    return pmask, risse, shadow



def _linien_v3(norm, road, lo=88, hi=98, minlen=0.45):
    """Numerisch robuster Linienfilter für feine Risse."""
    if not np.any(road):
        return np.zeros_like(road, dtype=bool)

    r = sato(norm, sigmas=[1.5, 2.5, 3.5], black_ridges=True)
    loc = cv2.GaussianBlur(r, (0, 0), 40)
    base = float(np.percentile(r[road], 50)) if np.any(road) else 0.0
    rn = r / np.maximum(loc + base, 1e-6)

    kern = max(3, int(EDGE_KEEP / RES) * 2 + 1)
    inner = cv2.erode(
        road.astype(np.uint8), np.ones((1, kern), np.uint8)
    ).astype(bool)

    vals = rn[inner]
    vals = vals[np.isfinite(vals)]
    if vals.size < 100:
        return np.zeros_like(road, dtype=bool)

    low = float(np.percentile(vals, lo))
    high = float(np.percentile(vals, hi))
    if not np.isfinite(low) or not np.isfinite(high) or high <= 0:
        return np.zeros_like(road, dtype=bool)

    m = apply_hysteresis_threshold(
        np.where(inner, np.nan_to_num(rn, nan=0.0, posinf=0.0, neginf=0.0), 0),
        low, high
    )

    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8))
    out = np.zeros_like(m)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if max(w, h) < 15:
            continue
        sl = (slice(y, y+h), slice(x, x+w))
        comp = lab[sl] == i
        if skeletonize(comp).sum() < 25:
            continue
        out[sl] |= comp
    return out


def overlay_foto_v3(img, Hm, top, road, pmask, risse, buf, nmask):
    """Rückprojektion mit SVD-Inverser; bei extremen Geometrien kein Absturz."""
    hh, ww = img.shape[:2]
    lay = np.zeros_like(top)
    a = np.zeros(road.shape, np.uint8)

    lay[road] = (180, 60, 160); a[road] = 42
    lay[buf] = (80, 80, 255); a[buf] = 68
    lay[nmask] = NETZ; a[nmask] = 110
    lay[pmask] = PATCH; a[pmask] = 165

    for c in risse:
        k = cv2.dilate(
            c['sk'].astype(np.uint8), np.ones((5, 5), np.uint8)
        ).astype(bool)
        lay[k] = COL[c['typ']]
        a[k] = 255

    try:
        ok, Hi = cv2.invert(Hm.astype(np.float64), flags=cv2.DECOMP_SVD)
        if not ok or not np.all(np.isfinite(Hi)):
            return img.copy()
        L = cv2.warpPerspective(lay, Hi, (ww, hh))
        A = cv2.warpPerspective(a, Hi, (ww, hh)).astype(np.float32)[..., None] / 255.0
        return (img * (1 - A) + L * A).astype(np.uint8)
    except Exception:
        return img.copy()


def auswerten_v3(foto, args):
    foto.breite_eff = NORM_BREITE
    foto.breite_roh = NORM_BREITE
    foto.breite_methode = 'interne Normbreite – keine reale Breitenmessung'

    foto.kanten()
    foto.verfeinern(iters=2)

    # Unabhängig von einer behaupteten realen Breite.
    foto.breite_eff = NORM_BREITE

    top, road, Hm = foto.entzerren()

    # Nur tatsächlich verdeckte Fahrzeugflächen nicht bewerten.
    vehicle_excl = _vehicle_only_v3(foto, top, road, Hm)
    valid_road = road & ~vehicle_excl
    if valid_road.sum() < 1000:
        valid_road = road.copy()

    pmask, risse, shadow = erkennen_v3(top, road, exclude=vehicle_excl)

    einzel = [c for c in risse if c['typ'] != 'Netzriss']
    netz = [c for c in risse if c['typ'] == 'Netzriss']

    # Einzelriss-Einflussfläche: weiterhin ein einheitlicher normierter
    # Puffer. Er dient der Flächenquote; es wird keine reale m²-Angabe behauptet.
    sk_all = np.zeros_like(road)
    sk_l = np.zeros_like(road)
    sk_q = np.zeros_like(road)
    for c in einzel:
        sk_all |= c['sk']
        if c['typ'] == 'Längsriss':
            sk_l |= c['sk']
        elif c['typ'] == 'Querriss':
            sk_q |= c['sk']

    rbuf = int(round(CRACK_W / 2 / RES))
    element = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2*rbuf+1, 2*rbuf+1)
    )

    buf = cv2.dilate(sk_all.astype(np.uint8), element).astype(bool) & valid_road
    buf_l = cv2.dilate(sk_l.astype(np.uint8), element).astype(bool) & valid_road
    buf_q = cv2.dilate(sk_q.astype(np.uint8), element).astype(bool) & valid_road

    nmask = np.zeros_like(road)
    for c in netz:
        nmask |= c['flaeche_mask']
    nmask &= valid_road
    pmask &= valid_road

    union = (buf | nmask | pmask) & valid_road

    denom = max(int(valid_road.sum()), 1)
    pct = lambda m: float(np.count_nonzero(m) / denom * 100.0)

    res = dict(
        datei=foto.name,
        zeit=foto.meta['zeit'],
        lat=foto.meta['lat'],
        lon=foto.meta['lon'],
        gps_genauigkeit=foto.meta['gps_genauigkeit'],
        randmethode=foto.randmethode,
        bereich_von=foto.von_eff,
        bereich_bis=foto.bis_eff,
        sichtanteil_prozent=getattr(foto, 'sichtanteil', 100.0),
        einzelriss_anteil=pct(buf),
        laengsriss_anteil=pct(buf_l),
        querriss_anteil=pct(buf_q),
        netzriss_anteil=pct(nmask),
        flick_anteil=pct(pmask),
        schatten_anteil=pct(shadow & valid_road),
        fahrzeug_anteil=pct(vehicle_excl & road),
        schadanteil=pct(union),
        eval_pixels=denom,

        # Kompatibilitätsfelder für bestehende Ausgabefunktionen.
        # Sie sind in v3 KEINE realen Meter/m²-Werte.
        fahrbahnbreite=NORM_BREITE,
        fahrbahnbreite_roh=NORM_BREITE,
        breitenmethode='Normiert; reale Breite nicht benötigt',
        bezugsflaeche=1.0,
        laengsriss=0.0,
        querriss=0.0,
        einzelriss_laenge=0.0,
        einzelriss_flaeche_regel=pct(buf) / 100.0,
        einzelriss_flaeche=pct(buf) / 100.0,
        netzriss_flaeche=pct(nmask) / 100.0,
        flickflaeche=pct(pmask) / 100.0,
        schlagloecher=None,
        schadflaeche=pct(union) / 100.0,
        schatten_ausgeschlossen_m2=0.0,
        kamerahoehe=0.0,
        neigung=float(foto.d.get('pitch', 0.0))
    )
    res['klasse'] = klasse(res['schadanteil'])

    return res, top, road, pmask, risse, buf, nmask, Hm


def _haversine_v3(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(min(1.0, math.sqrt(a)))


def _parse_time_v3(s):
    if not s:
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.datetime.strptime(str(s), fmt)
        except Exception:
            pass
    return None


def _split_routes_v3(S, gps_gap=GPS_ROUTE_GAP_M, time_gap_min=TIME_ROUTE_GAP_MIN):
    """Teilt Langenlois/Krems usw. automatisch in getrennte Befahrungen."""
    routes = []
    cur = []
    prev = None

    for r in S:
        new_route = False
        if prev is not None:
            if (
                r.get('lat') is not None and r.get('lon') is not None and
                prev.get('lat') is not None and prev.get('lon') is not None
            ):
                d = _haversine_v3(
                    prev['lat'], prev['lon'], r['lat'], r['lon']
                )
                if d > gps_gap:
                    new_route = True

            t0 = _parse_time_v3(prev.get('zeit'))
            t1 = _parse_time_v3(r.get('zeit'))
            if t0 and t1:
                dt = abs((t1-t0).total_seconds()) / 60.0
                if dt > time_gap_min:
                    new_route = True

        if new_route and cur:
            routes.append(cur)
            cur = []

        cur.append(r)
        prev = r

    if cur:
        routes.append(cur)

    for rid, route in enumerate(routes, 1):
        for i, r in enumerate(route):
            r['befahrung'] = rid
            r['nr_befahrung'] = i + 1
            r['von'] = round(i * NOMINAL_ABSCHNITT_M, 1)
            r['bis'] = round((i + 1) * NOMINAL_ABSCHNITT_M, 1)

    return routes


def _route_axis_v3(route):
    gps = [r for r in route if r.get('lat') is not None and r.get('lon') is not None]
    if len(gps) < 2:
        return None
    return Achse([(r['lat'], r['lon']) for r in gps])


def geojson_v3(routes, titel):
    feats = []
    global_nr = 1

    for rid, route in enumerate(routes, 1):
        gps_route = [
            r for r in route
            if r.get('lat') is not None and r.get('lon') is not None
        ]
        ax = _route_axis_v3(route)

        if ax is not None:
            for i, r in enumerate(route):
                # Abschnitt als farbige Mittellinie statt scheinexakter
                # Fahrbahnbreitenfläche.
                s0 = i * NOMINAL_ABSCHNITT_M
                s1 = (i + 1) * NOMINAL_ABSCHNITT_M
                pts = []
                for s in np.linspace(s0, s1, 5):
                    p, _ = ax.punkt(s)
                    la, lo = ax.ll(p)
                    pts.append([round(lo, 7), round(la, 7)])

                feats.append({
                    'type': 'Feature',
                    'geometry': {'type': 'LineString', 'coordinates': pts},
                    'properties': {
                        'art': 'abschnitt',
                        'abschnitt': f"{global_nr:02d}",
                        'befahrung': rid,
                        'foto': r['datei'],
                        'aufnahme': r.get('zeit', ''),
                        'zustandsklasse': r['klasse'],
                        'schadanteil_prozent': round(r['schadanteil'], 1),
                        'einzelriss_prozent': round(r.get('einzelriss_anteil', 0), 1),
                        'netzriss_prozent': round(r.get('netzriss_anteil', 0), 1),
                        'flick_prozent': round(r.get('flick_anteil', 0), 1),
                        'sichtanteil_prozent': round(r.get('sichtanteil_prozent', 100), 0),
                        'bild': f"bilder/{r['datei']}_foto.jpg",
                    }
                })
                global_nr += 1

        for r in route:
            if r.get('lat') is not None and r.get('lon') is not None:
                feats.append({
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [
                            round(r['lon'], 7), round(r['lat'], 7)
                        ]
                    },
                    'properties': {
                        'art': 'foto',
                        'befahrung': rid,
                        'foto': r['datei'],
                        'schadanteil_prozent': round(r['schadanteil'], 1),
                        'bild': f"bilder/{r['datei']}_foto.jpg",
                    }
                })

    return {
        'type': 'FeatureCollection',
        'name': titel,
        'features': feats
    }


KARTE_HTML_V3 = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITEL__ – RoadAI Karte</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
html,body,#map{height:100%;margin:0}
body{font-family:Segoe UI,Arial,sans-serif}
.info{background:#fff;padding:8px 10px;border-radius:5px;box-shadow:0 1px 4px #0004}
.popupimg{display:block;width:260px;max-width:100%;margin-top:8px;border-radius:4px}
</style>
</head><body><div id="map"></div>
<script>
const D=__GEOJSON__;
const FARBE={1:'#1b7f3b',2:'#7ccf4f',3:'#f2d23a',4:'#f08a24',5:'#e0312b'};
const basis='https://mapsneu.wien.gv.at/basemap/';
const opt={maxNativeZoom:20,maxZoom:22,attribution:'Datenquelle: basemap.at'};
const ortho=L.tileLayer(basis+'bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg',opt);
const grund=L.tileLayer(basis+'geolandbasemap/normal/google3857/{z}/{y}/{x}.png',opt);
const labels=L.tileLayer(basis+'bmapoverlay/normal/google3857/{z}/{y}/{x}.png',opt);
const map=L.map('map',{layers:[ortho,labels]});
L.control.scale({imperial:false}).addTo(map);

const seg=L.geoJSON(D,{
 filter:f=>f.properties.art==='abschnitt',
 style:f=>({color:FARBE[f.properties.zustandsklasse]||'#555',weight:11,opacity:.88,lineCap:'butt'}),
 onEachFeature:(f,l)=>{
   const p=f.properties;
   l.bindPopup(
     '<b>Befahrung '+p.befahrung+' · Abschnitt '+p.abschnitt+'</b><br>'+
     p.foto+'<br><b>'+Number(p.schadanteil_prozent).toLocaleString('de-AT')+' % Schaden</b><br>'+
     'Einzelrissfläche '+Number(p.einzelriss_prozent).toLocaleString('de-AT')+' % · '+
     'Netzriss '+Number(p.netzriss_prozent).toLocaleString('de-AT')+' % · '+
     'Flick '+Number(p.flick_prozent).toLocaleString('de-AT')+' %'+
     '<a href="'+p.bild+'" target="_blank"><img class="popupimg" src="'+p.bild+'"></a>'
   );
 }
}).addTo(map);

const fotos=L.geoJSON(D,{
 filter:f=>f.properties.art==='foto',
 pointToLayer:(f,ll)=>L.circleMarker(ll,{radius:4,color:'#fff',weight:1.5,fillColor:'#111',fillOpacity:1}),
 onEachFeature:(f,l)=>{
   const p=f.properties;
   l.bindPopup('<b>'+p.foto+'</b><br>Befahrung '+p.befahrung+
     '<a href="'+p.bild+'" target="_blank"><img class="popupimg" src="'+p.bild+'"></a>');
 }
});

L.control.layers(
 {'Luftbild':ortho,'Grundkarte':grund},
 {'Straßenabschnitte':seg,'Fotostandorte':fotos,'Straßennamen':labels},
 {collapsed:false}
).addTo(map);

if(seg.getBounds().isValid()) map.fitBounds(seg.getBounds(),{padding:[35,35],maxZoom:20});
else {
 const all=L.geoJSON(D).getBounds();
 if(all.isValid()) map.fitBounds(all,{padding:[35,35],maxZoom:20});
}
</script></body></html>"""


def bericht_html_v3(args, S, routes, fehler, prefix):
    esc = html.escape

    total_px = sum(max(1, int(r.get('eval_pixels', 1))) for r in S)
    total_damage = sum(
        r['schadanteil'] * max(1, int(r.get('eval_pixels', 1)))
        for r in S
    ) / max(total_px, 1)

    cnt = {c: sum(1 for r in S if r['klasse'] == c) for c in range(1, 6)}

    rows = []
    cards = []
    for r in S:
        rows.append(
            f"<tr><td>{r['nr']:02d}</td><td>{r.get('befahrung',1)}</td>"
            f"<td>{esc(r['datei'])}</td>"
            f"<td>{de(r['schadanteil'])} %</td>"
            f"<td>{de(r.get('einzelriss_anteil',0))} %</td>"
            f"<td>{de(r.get('netzriss_anteil',0))} %</td>"
            f"<td>{de(r.get('flick_anteil',0))} %</td>"
            f"<td>{de(r.get('sichtanteil_prozent',100),0)} %</td>"
            f"<td>Klasse {r['klasse']}</td></tr>"
        )
        cards.append(
            f"""<article class="card" id="a{r['nr']:02d}">
            <div class="cardhead"><div><b>Abschnitt {r['nr']:02d}</b> · Befahrung {r.get('befahrung',1)}<br>
            <span>{esc(r['datei'])}</span></div>
            <strong>{de(r['schadanteil'])} %</strong></div>
            <div class="pics">
              <a href="bilder/{esc(r['datei'])}_foto.jpg" target="_blank"><img src="bilder/{esc(r['datei'])}_foto.jpg"></a>
              <a href="bilder/{esc(r['datei'])}_draufsicht_markiert.jpg" target="_blank"><img src="bilder/{esc(r['datei'])}_draufsicht_markiert.jpg"></a>
            </div>
            <div class="metrics">
              <span>Einzelrissfläche <b>{de(r.get('einzelriss_anteil',0))} %</b></span>
              <span>Netzriss <b>{de(r.get('netzriss_anteil',0))} %</b></span>
              <span>Flickstellen <b>{de(r.get('flick_anteil',0))} %</b></span>
              <span>Sichtbarer Straßenanteil <b>{de(r.get('sichtanteil_prozent',100),0)} %</b></span>
            </div>
            <small>Rand: {esc(r.get('randmethode',''))}</small>
            </article>"""
        )

    route_text = ', '.join(
        f"Befahrung {i+1}: {len(route)} Foto(s)"
        for i, route in enumerate(routes)
    )

    warn = ''
    if fehler:
        warn = '<div class="warn"><b>Nicht ausgewertet:</b> ' + '; '.join(
            f"{esc(n)} ({esc(m)})" for n, m in fehler
        ) + '</div>'

    return f"""<!doctype html><html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(args.titel)}</title>
<style>
:root{{--bg:#f5f6f4;--paper:#fff;--ink:#1d2226;--muted:#667078;--line:#d9ddda}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:Segoe UI,Arial,sans-serif}}
.wrap{{max-width:1200px;margin:auto;padding:28px 16px 60px}}
header{{background:var(--paper);padding:24px;border:1px solid var(--line);border-radius:12px}}
h1{{margin:.2rem 0}} .lead{{color:var(--muted);max-width:850px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}}
.stat{{background:#f1f3f2;padding:12px;border-radius:8px}} .stat b{{font-size:1.5rem;display:block}}
section{{margin-top:34px}} table{{width:100%;border-collapse:collapse;background:var(--paper)}}
th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left}}
.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}
.card{{background:var(--paper);border:1px solid var(--line);padding:14px;border-radius:10px}}
.cardhead{{display:flex;justify-content:space-between;gap:12px;margin-bottom:10px}}
.cardhead strong{{font-size:1.35rem}} .pics{{display:grid;grid-template-columns:1.45fr 1fr;gap:7px}}
.pics img{{width:100%;display:block}} .metrics{{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin:10px 0}}
.metrics span{{background:#f2f4f3;padding:7px;border-radius:5px}} small{{color:var(--muted)}}
.note,.warn{{padding:12px 14px;margin-top:14px;border-radius:6px}} .note{{background:#eef5ff}} .warn{{background:#fff0ea}}
a{{color:#2e65a7}} @media(max-width:800px){{.stats,.cards{{grid-template-columns:1fr 1fr}}}}
@media(max-width:560px){{.stats,.cards,.pics,.metrics{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<header>
<div>RoadAI · Prototyp v{VERSION}</div>
<h1>{esc(args.titel)}</h1>
<p class="lead">Automatische prozentuale Zustandsauswertung. Die reale Fahrbahnbreite wird nicht benötigt. Bewertet wird die perspektivisch normalisierte, sichtbare Fahrbahnoberfläche.</p>
<div class="stats">
<div class="stat"><b>{de(total_damage)} %</b>Schadanteil gesamt</div>
<div class="stat"><b>{len(S)}</b>ausgewertete Abschnitte</div>
<div class="stat"><b>{len(routes)}</b>Befahrung(en)</div>
<div class="stat"><b>{sum(cnt.values())}</b>Fotos</div>
</div>
<p class="lead">{esc(route_text)}</p>
</header>
<div class="note"><b>Fahrbahnrand:</b> Asphalt wird automatisch gegen Grün, Bankett, Pflaster/Rinne, Gehsteig/Bord und äußere weiße Randmarkierungen abgegrenzt. Es ist keine manuelle Korrektur vorgesehen. Schatten werden über eine Beleuchtungsnormalisierung behandelt und nicht pauschal als Schaden gewertet.</div>
{warn}
<section><h2>Luftbildkarte</h2>
<p><a href="{esc(prefix)}Karte.html" target="_blank">Karte öffnen →</a> · Abschnitte und Fotopunkte sind anklickbar; das jeweilige Ergebnisbild kann direkt geöffnet werden.</p></section>
<section><h2>Ergebnisse</h2>
<div style="overflow:auto"><table><thead><tr><th>Abschnitt</th><th>Befahrung</th><th>Foto</th><th>Schaden</th><th>Einzelriss</th><th>Netzriss</th><th>Flick</th><th>Sichtbar</th><th>Klasse</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
</section>
<section><h2>Abschnitte im Detail</h2><div class="cards">{''.join(cards)}</div></section>
<section><h2>Methodik</h2>
<ul>
<li><b>Bezugsgröße:</b> 100 % = normalisierte, sichtbare Fahrbahnoberfläche des Abschnitts.</li>
<li><b>Keine reale Fahrbahnbreite erforderlich:</b> 5,80 m wird intern nur noch als einheitliche Normskala für die Perspektivtransformation verwendet.</li>
<li><b>Breite Straßen:</b> Ist die volle Breite im Nahbereich nicht sichtbar, wird der Auswertebereich automatisch nach vorne verschoben; bleibt ein Rand außerhalb des Bildes, wird der sichtbare Teil bewertet statt das Foto zu verwerfen.</li>
<li><b>Fahrbahnrand:</b> semantische road-Klasse, Material-/Texturwechsel, Grün, unbefestigtes/geschottertes Bankett, Pflaster/Gehsteig/Bord sowie äußere weiße Randmarkierungen.</li>
<li><b>Schatten:</b> großflächige Beleuchtungsänderungen werden normalisiert. Sie dürfen keine Flickstelle erzeugen; feine Rissstrukturen können weiterhin erkannt werden.</li>
<li><b>Karte:</b> getrennte Befahrungen werden bei großen GPS-/Zeitsprüngen automatisch erkannt.</li>
</ul></section>
</div></body></html>"""


def main_v3():
    ap = argparse.ArgumentParser(
        description='RoadAI v3 – prozentuale Straßenschäden aus Smartphone-Fotos'
    )
    hier = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument(
        'fotos', nargs='?', default=os.path.join(hier, 'bilder'),
        help='Ordner mit JPG/PNG/HEIC-Fotos'
    )
    ap.add_argument(
        '--ausgabe', default=os.path.join(hier, 'Ergebnis_v3'),
        help='Zielordner'
    )
    ap.add_argument('--titel', default='RoadAI – Auswertung')
    ap.add_argument('--ort', default='')
    ap.add_argument(
        '--von', type=float, default=2.0,
        help='gewünschter naher Beginn; wird automatisch weitergeschoben, falls nötig'
    )
    ap.add_argument('--bis', type=float, default=7.0)
    ap.add_argument(
        '--ohne', nargs='*', default=[],
        help='Fotos nicht als Streckenabschnitt werten'
    )
    ap.add_argument('--webhook', default=None)
    ap.add_argument(
        '--ohne-ki-fahrbahn', action='store_true',
        help='SegFormer-Fahrbahnerkennung deaktivieren'
    )
    ap.add_argument(
        '--gps-sprung', type=float, default=GPS_ROUTE_GAP_M,
        help='GPS-Sprung in m, ab dem automatisch eine neue Befahrung beginnt'
    )
    args = ap.parse_args()

    # Interne Kompatibilitätsparameter für die vorhandene Geometrie.
    args.breite = NORM_BREITE
    args.referenzfoto = ''
    args.min_breite = 2.2
    args.max_breite = 7.0
    args.keine_breite_auto = True
    args.kein_auto_nahe = False
    args.abstand = NOMINAL_ABSCHNITT_M

    ordner = os.path.abspath(args.fotos)
    aus = os.path.abspath(args.ausgabe)
    bild_dir = os.path.join(aus, 'bilder')
    os.makedirs(bild_dir, exist_ok=True)

    prefix = ''.join(
        c if c.isalnum() or c in '-_' else '_'
        for c in args.titel
    ).strip('_') + '_'

    alle_dateien = glob.glob(os.path.join(ordner, '*'))
    heic = [
        p for p in alle_dateien
        if p.lower().endswith(('.heic', '.heif'))
    ]
    pfade = sorted(
        p for p in alle_dateien
        if p.lower().endswith(('.jpg', '.jpeg', '.png'))
    )
    heic_meta = {}

    if heic:
        print(f'HEIC: {len(heic)} Datei(en) gefunden – Windows erzeugt JPG-Arbeitskopien.')
        heic_cache = os.path.join(aus, '_heic_jpg_cache')
        heic_jpg, heic_meta = _heic_windows(
            ordner, heic, heic_cache
        )
        stems = {
            os.path.splitext(os.path.basename(x))[0].lower()
            for x in pfade
        }
        for p in heic_jpg:
            if os.path.splitext(os.path.basename(p))[0].lower() not in stems:
                pfade.append(p)
        pfade = sorted(pfade)

    if not pfade:
        sys.exit(f'Keine lesbaren Fotos gefunden in: {ordner}')

    road_ai = None
    if not args.ohne_ki_fahrbahn:
        if HAVE_ROAD_AI:
            try:
                road_ai = RoadMaskAI()
            except Exception as e:
                print(f'  ! Fahrbahn-KI nicht verfügbar ({e}) – klassischer Fallback.')
        else:
            print('  ! torch/transformers nicht verfügbar – klassischer Fallback.')

    print(f'RoadAI v{VERSION}: {len(pfade)} Fotos in {ordner}')
    print('Bewertung: Prozent der normalisierten sichtbaren Fahrbahnfläche; keine reale Breitenmessung.')

    geladen = []
    for p in pfade:
        try:
            img, ex = lade_foto(p)
        except Exception as e:
            print(f'  ! {os.path.basename(p)}: kann nicht gelesen werden ({e})')
            continue

        meta = exif_daten(ex)
        ov = heic_meta.get(
            os.path.splitext(os.path.basename(p))[0].lower()
        )
        if ov:
            if ov.get('zeit'):
                meta['zeit'] = ov['zeit']
            if ov.get('lat') is not None:
                meta['lat'] = ov['lat']
            if ov.get('lon') is not None:
                meta['lon'] = ov['lon']
            if ov.get('modell'):
                meta['modell'] = ov['modell']

        geladen.append((p, img, meta))

    geladen.sort(
        key=lambda t: (t[2]['zeit'] or '', os.path.basename(t[0]))
    )

    S, extra, fehler, alle_overlays = [], [], [], []
    ohne = {o.lower() for o in args.ohne}

    for p, img, meta in geladen:
        foto = Foto(p, img, meta, args, road_ai=road_ai)
        print(f'  · {foto.name} …', end=' ', flush=True)

        try:
            res, top, road, pm, risse, buf, nm, Hm = auswerten_v3(foto, args)
        except Exception as e:
            print(f'übersprungen: {e}')
            fehler.append((foto.name, str(e)))
            continue

        res['modell'] = meta.get('modell', '')

        ov = overlay_draufsicht(top, road, pm, risse, buf, nm)
        imwrite(
            os.path.join(bild_dir, f'{foto.name}_draufsicht_markiert.jpg'),
            ov
        )
        imwrite(
            os.path.join(bild_dir, f'{foto.name}_draufsicht_roh.jpg'),
            top, 88
        )
        klein = cv2.resize(
            ov,
            (max(1, ov.shape[1] // 2), max(1, ov.shape[0] // 2)),
            interpolation=cv2.INTER_AREA
        )
        imwrite(
            os.path.join(bild_dir, f'{foto.name}_draufsicht_klein.jpg'),
            klein, 80
        )

        of = overlay_foto_v3(img, Hm, top, road, pm, risse, buf, nm)
        imwrite(
            os.path.join(bild_dir, f'{foto.name}_foto.jpg'),
            cv2.resize(
                of,
                (960, int(960 * of.shape[0] / of.shape[1])),
                interpolation=cv2.INTER_AREA
            ),
            80
        )

        if foto.name.lower() in ohne:
            extra.append(res)
            print(f'{res["schadanteil"]:.1f} % (nur Referenz/übersprungen)')
        else:
            S.append(res)
            alle_overlays.append(ov)
            print(
                f'{res["schadanteil"]:.1f} % · '
                f'sichtbar {res["sichtanteil_prozent"]:.0f} % · '
                f'Klasse {res["klasse"]}'
            )

    if not S:
        sys.exit('Kein Foto konnte ausgewertet werden.')

    # Getrennte Strecken / Befahrungen
    routes = _split_routes_v3(
        S,
        gps_gap=float(args.gps_sprung),
        time_gap_min=TIME_ROUTE_GAP_MIN
    )

    # Globale Abschnittsnummer
    nr = 1
    for route in routes:
        for r in route:
            r['nr'] = nr
            nr += 1

    # Vogelperspektive: normierte Breite ist bewusst gleich.
    if alle_overlays:
        max_w = max(im.shape[1] for im in alle_overlays)
        padded = []
        for im in alle_overlays:
            d = max_w - im.shape[1]
            l = d // 2
            rr = d - l
            padded.append(
                cv2.copyMakeBorder(
                    im, 0, 0, l, rr,
                    cv2.BORDER_CONSTANT, value=(235, 235, 235)
                )
            )
        mos = np.vstack(padded[::-1])
        imwrite(
            os.path.join(bild_dir, 'strecke_vogelperspektive.jpg'),
            mos, 85
        )

    # Karte/GeoJSON – mehrere Befahrungen korrekt getrennt.
    gj = geojson_v3(routes, args.titel)
    geojson_path = os.path.join(aus, prefix + 'Abschnitte.geojson')
    with open(geojson_path, 'w', encoding='utf-8') as fh:
        json.dump(gj, fh, ensure_ascii=False, indent=1)

    map_path = os.path.join(aus, prefix + 'Karte.html')
    with open(map_path, 'w', encoding='utf-8') as fh:
        fh.write(
            KARTE_HTML_V3
            .replace('__GEOJSON__', json.dumps(gj, ensure_ascii=False))
            .replace('__TITEL__', html.escape(args.titel))
        )

    # CSV – Prozentwerte, keine vorgetäuschten Meter/m².
    csv_path = os.path.join(aus, prefix + 'Schaeden.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow([
            'Abschnitt', 'Befahrung', 'Foto', 'Aufnahmezeit',
            'Breite', 'Länge',
            'Sichtbarer Fahrbahnanteil (%)',
            'Einzelrissfläche (%)',
            'Längsriss-Einflussfläche (%)',
            'Querriss-Einflussfläche (%)',
            'Netzrissfläche (%)',
            'Flickfläche (%)',
            'Schadanteil Union (%)',
            'Zustandsklasse',
            'Randmethode',
            'Google Maps'
        ])
        for r in S:
            w.writerow([
                f"{r['nr']:02d}",
                r.get('befahrung', 1),
                r['datei'],
                r.get('zeit', ''),
                de(r['lat'], 6) if r.get('lat') is not None else '',
                de(r['lon'], 6) if r.get('lon') is not None else '',
                de(r.get('sichtanteil_prozent', 100), 0),
                de(r.get('einzelriss_anteil', 0), 1),
                de(r.get('laengsriss_anteil', 0), 1),
                de(r.get('querriss_anteil', 0), 1),
                de(r.get('netzriss_anteil', 0), 1),
                de(r.get('flick_anteil', 0), 1),
                de(r.get('schadanteil', 0), 1),
                r.get('klasse', ''),
                r.get('randmethode', ''),
                maps_link(r)
            ])

    # JSON
    with open(
        os.path.join(aus, 'ergebnisse.json'),
        'w', encoding='utf-8'
    ) as fh:
        json.dump(
            {
                'titel': args.titel,
                'ort': args.ort,
                'version': VERSION,
                'modus': 'Prozent der normalisierten sichtbaren Fahrbahnfläche',
                'befahrungen': len(routes),
                'abschnitte': S,
                'uebersprungen': extra,
                'nicht_ausgewertet': fehler
            },
            fh, ensure_ascii=False, indent=1, default=float
        )

    # Bericht
    bericht_path = os.path.join(aus, prefix + 'Bericht.html')
    with open(bericht_path, 'w', encoding='utf-8') as fh:
        fh.write(
            bericht_html_v3(args, S, routes, fehler, prefix)
        )

    # Optionaler Webhook
    if args.webhook:
        try:
            total_px = sum(max(1, int(r.get('eval_pixels', 1))) for r in S)
            total_damage = sum(
                r['schadanteil'] * max(1, int(r.get('eval_pixels', 1)))
                for r in S
            ) / max(total_px, 1)
            daten = {
                'titel': args.titel,
                'befahrungen': len(routes),
                'abschnitte': len(S),
                'schadanteil_gesamt': round(total_damage, 1),
                'werte': S
            }
            print(f'  Webhook: Status {sende_webhook(args.webhook, daten)}')
        except Exception as e:
            print(f'  ! Webhook fehlgeschlagen: {e}')

    print(f'\nFertig. Ergebnisse in: {aus}')
    print(f'  Bericht: {os.path.basename(bericht_path)}')
    print(f'  Karte:   {os.path.basename(map_path)}')
    print(f'  CSV:     {os.path.basename(csv_path)}')
    print(f'  Befahrungen erkannt: {len(routes)}')


if __name__ == '__main__':
    main_v3()
