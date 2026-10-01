from __future__ import annotations

import base64
import csv
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps

APP_DIR = Path(__file__).resolve().parent
CORE = APP_DIR / "road_ai_core.py"

st.set_page_config(
    page_title="RoadAI",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.block-container{max-width:1450px;padding-top:1.3rem;padding-bottom:3rem}
.hero{padding:1rem 1.2rem;border:1px solid rgba(127,127,127,.25);border-radius:14px;
background:linear-gradient(135deg,rgba(60,110,180,.09),rgba(40,150,110,.05));margin-bottom:1rem}
.hero h1{margin:0 0 .2rem 0;font-size:2.1rem}
.hero p{margin:0;opacity:.75}
[data-testid="stMetric"]{border:1px solid rgba(127,127,127,.22);padding:.75rem;border-radius:12px}
</style>
<div class="hero"><h1>🛣️ RoadAI</h1>
<p>Automatische Straßenoberflächenanalyse aus Smartphone-Fotos</p></div>
""", unsafe_allow_html=True)


def safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._\-ÄÖÜäöüß]", "_", name) or "upload"


def is_heic(name: str) -> bool:
    return Path(name).suffix.lower() in {".heic", ".heif"}


def convert_heic_cloud(src: Path, dst: Path):
    """HEIC -> JPG für Linux/Streamlit Cloud. EXIF wird soweit möglich erhalten."""
    from pillow_heif import register_heif_opener
    register_heif_opener()

    with Image.open(src) as im:
        exif = im.info.get("exif")
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im).convert("RGB")
        kwargs = {"format": "JPEG", "quality": 96, "subsampling": 0}
        if exif:
            kwargs["exif"] = exif
        if icc:
            kwargs["icc_profile"] = icc
        im.save(dst, **kwargs)


def prepare_uploads(uploaded_files, job: Path) -> tuple[Path, list[str]]:
    source = job / "input"
    original = job / "original_uploads"
    source.mkdir(parents=True, exist_ok=True)
    original.mkdir(parents=True, exist_ok=True)

    messages = []
    windows = platform.system().lower().startswith("win")

    for u in uploaded_files:
        name = safe_name(u.name)
        raw_path = original / name
        raw_path.write_bytes(u.getvalue())

        if is_heic(name) and not windows:
            jpg = source / f"{Path(name).stem}.jpg"
            convert_heic_cloud(raw_path, jpg)
            messages.append(f"{name} → {jpg.name}")
        else:
            (source / name).write_bytes(u.getvalue())

    return source, messages


def run_roadai(
    source: Path,
    job: Path,
    title: str,
    location: str,
    start_m: float,
    use_ai: bool,
    gps_gap: float,
):
    output = job / "Ergebnis_v3"
    output.mkdir(parents=True, exist_ok=True)

    core_copy = job / "road_ai_core.py"
    shutil.copy2(CORE, core_copy)

    cmd = [
        sys.executable, str(core_copy), str(source),
        "--ausgabe", str(output),
        "--titel", title,
        "--ort", location,
        "--von", f"{start_m:.2f}",
        "--bis", f"{start_m + 5.0:.2f}",
        "--gps-sprung", f"{gps_gap:.1f}",
    ]
    if not use_ai:
        cmd.append("--ohne-ki-fahrbahn")

    env = os.environ.copy()
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    p = subprocess.run(
        cmd,
        cwd=str(job),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        env=env,
    )
    log = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    if p.returncode != 0:
        raise RuntimeError(log or f"RoadAI Fehlercode {p.returncode}")

    return output, log


def first(folder: Path, pattern: str):
    m = sorted(folder.glob(pattern))
    return m[0] if m else None


def load_json(path: Path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def zip_dir(folder: Path) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(folder))
    return mem.getvalue()


def map_with_embedded_images(map_file: Path, images_dir: Path) -> str:
    html = map_file.read_text(encoding="utf-8")

    def repl(match):
        rel = match.group(1)
        img = images_dir.parent / rel
        if not img.exists():
            return match.group(0)
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        return f'"data:image/jpeg;base64,{b64}"'

    # ersetzt "bilder/....jpg" in JS/HTML
    html = re.sub(r'"(bilder/[^"]+\.jpg)"', repl, html)
    html = re.sub(r"'(bilder/[^']+\.jpg)'", repl, html)
    return html


def read_csv(path: Path | None):
    if not path or not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


with st.sidebar:
    st.header("Projekt")
    title = st.text_input("Titel", "RoadAI Teststrecke")
    location = st.text_input("Ort / Beschreibung", "")

    st.header("Auswertung")
    start_m = st.number_input(
        "Beginn des 5-m-Bereichs [m]",
        min_value=1.0,
        max_value=5.0,
        value=2.0,
        step=0.25,
    )
    st.caption(f"Zielbereich: {start_m:.2f}–{start_m+5.0:.2f} m")

    use_ai = st.checkbox(
        "KI für Fahrbahnrand verwenden",
        value=True,
        help="Zusätzlich zu Grün, Bankett, Pflaster, Bord und äußerer weißer Markierung.",
    )
    gps_gap = st.number_input(
        "Neue Befahrung bei GPS-Sprung > [m]",
        min_value=20.0,
        max_value=500.0,
        value=60.0,
        step=10.0,
    )

    st.divider()
    st.caption(
        "RoadAI v3 bewertet primär den prozentual geschädigten Anteil der "
        "sichtbaren, perspektivisch normalisierten Fahrbahnfläche."
    )


st.subheader("1 · Fotos hochladen")
uploads = st.file_uploader(
    "JPG, JPEG, PNG, HEIC oder HEIF",
    type=["jpg", "jpeg", "png", "heic", "heif"],
    accept_multiple_files=True,
)

if uploads:
    st.success(f"{len(uploads)} Foto(s) ausgewählt.")
    with st.expander("Dateien"):
        st.write([u.name for u in uploads])


st.subheader("2 · Auswertung")
if st.button(
    "🚀 RoadAI starten",
    type="primary",
    use_container_width=True,
    disabled=not bool(uploads),
):
    old = st.session_state.get("job")
    if old:
        shutil.rmtree(old, ignore_errors=True)

    job = Path(tempfile.mkdtemp(prefix="roadai_web_"))

    try:
        with st.spinner("Fotos werden vorbereitet …"):
            source, converted = prepare_uploads(uploads, job)

        if converted:
            with st.expander("HEIC-Konvertierung"):
                for line in converted:
                    st.write("✓", line)

        with st.spinner(
            "RoadAI erkennt Fahrbahnrand, Schäden, Befahrungen und Karte …"
        ):
            output, log = run_roadai(
                source=source,
                job=job,
                title=title.strip() or "RoadAI",
                location=location.strip(),
                start_m=float(start_m),
                use_ai=bool(use_ai),
                gps_gap=float(gps_gap),
            )

        st.session_state["job"] = str(job)
        st.session_state["output"] = str(output)
        st.session_state["log"] = log
        st.success("Auswertung abgeschlossen.")

    except subprocess.TimeoutExpired:
        shutil.rmtree(job, ignore_errors=True)
        st.error("Die Auswertung wurde nach 30 Minuten abgebrochen.")
    except Exception as e:
        st.session_state["log"] = str(e)
        st.error("Die Auswertung konnte nicht abgeschlossen werden.")
        with st.expander("Technische Details"):
            st.code(str(e))


if st.session_state.get("output"):
    output = Path(st.session_state["output"])
    if output.exists():
        data = load_json(output / "ergebnisse.json")
        sections = data.get("abschnitte", [])

        st.divider()
        st.subheader("3 · Ergebnis")

        if sections:
            weights = [max(1, int(r.get("eval_pixels", 1))) for r in sections]
            total = sum(
                float(r.get("schadanteil", 0)) * w
                for r, w in zip(sections, weights)
            ) / max(sum(weights), 1)

            c1, c2, c3 = st.columns(3)
            c1.metric("Ausgewertete Abschnitte", len(sections))
            c2.metric("Befahrungen", int(data.get("befahrungen", 1)))
            c3.metric("Schadanteil gesamt", f"{total:.1f} %")

        tabs = st.tabs([
            "🗺️ Karte",
            "📋 Werte",
            "🖼️ Fotos",
            "📄 Bericht",
            "⬇️ Download",
            "⚙️ Protokoll",
        ])

        with tabs[0]:
            map_file = first(output, "*Karte.html")
            if map_file:
                try:
                    map_html = map_with_embedded_images(
                        map_file, output / "bilder"
                    )
                    components.html(map_html, height=760, scrolling=False)
                except Exception as e:
                    st.warning(f"Karte konnte nicht eingebettet werden: {e}")
            else:
                st.info("Keine Karte vorhanden. Für die Karte werden GPS-Daten benötigt.")

        with tabs[1]:
            rows = read_csv(first(output, "*Schaeden.csv"))
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.info("Keine Tabelle vorhanden.")

        with tabs[2]:
            imgs = sorted((output / "bilder").glob("*_foto.jpg"))
            if not imgs:
                st.info("Keine Ergebnisbilder gefunden.")
            else:
                cols = st.columns(2)
                for i, p in enumerate(imgs):
                    with cols[i % 2]:
                        st.image(str(p), caption=p.stem, use_container_width=True)

        with tabs[3]:
            report = first(output, "*Bericht.html")
            if report:
                # Bericht enthält relative Bilder; in Streamlit zeigen wir ihn
                # primär als Download und die Inhalte in den anderen Tabs.
                st.download_button(
                    "⬇️ HTML-Bericht herunterladen",
                    report.read_bytes(),
                    file_name=report.name,
                    mime="text/html",
                    use_container_width=True,
                )
                st.info(
                    "Die wichtigsten Berichtsinhalte sind direkt in den Tabs "
                    "Karte, Werte und Fotos verfügbar."
                )

        with tabs[4]:
            st.download_button(
                "⬇️ Komplettes RoadAI-Ergebnis als ZIP",
                zip_dir(output),
                file_name="RoadAI_Ergebnis.zip",
                mime="application/zip",
                use_container_width=True,
            )
            for label, pat, mime in [
                ("CSV", "*Schaeden.csv", "text/csv"),
                ("GeoJSON", "*Abschnitte.geojson", "application/geo+json"),
                ("Karte HTML", "*Karte.html", "text/html"),
            ]:
                p = first(output, pat)
                if p:
                    st.download_button(
                        f"⬇️ {label}",
                        p.read_bytes(),
                        file_name=p.name,
                        mime=mime,
                        use_container_width=True,
                    )

        with tabs[5]:
            st.code(st.session_state.get("log", ""))

st.divider()
st.caption(
    "Prototyp. Vor einem öffentlichen Produktivbetrieb sollten Login, "
    "automatische Löschfristen sowie Datenschutzfunktionen für Personen "
    "und Kennzeichen ergänzt werden."
)
