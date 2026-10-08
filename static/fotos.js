/* RoadSense: Fotos aufnehmen oder auswählen, verkleinern, hochladen, Auswertung verfolgen.
   Wird vor app.js geladen und stellt window.RoadAIFotos bereit. */
(() => {
  "use strict";
  const MAX_KANTE = 4032;

  // ---------- EXIF aus JPEG lesen (Zeit, GPS, Brennweite, Modell) ----------
  function liesExif(buf) {
    const v = new DataView(buf);
    if (v.byteLength < 4 || v.getUint16(0) !== 0xffd8) return {};
    let p = 2;
    while (p + 4 < v.byteLength) {
      const marker = v.getUint16(p), len = v.getUint16(p + 2);
      if (marker === 0xffe1 && v.getUint32(p + 4) === 0x45786966) return tiff(v, p + 10);
      if ((marker & 0xff00) !== 0xff00 || marker === 0xffda) break;
      p += 2 + len;
    }
    return {};
  }

  function tiff(v, t) {
    const le = v.getUint16(t) === 0x4949;
    const u16 = (o) => v.getUint16(o, le), u32 = (o) => v.getUint32(o, le);
    const tabelle = (o) => {
      const tags = {};
      if (o <= 0 || t + o + 2 > v.byteLength) return tags;
      const n = u16(t + o);
      for (let i = 0; i < n; i++) {
        const e = t + o + 2 + i * 12;
        if (e + 12 > v.byteLength) break;
        tags[u16(e)] = { typ: u16(e + 2), anzahl: u32(e + 4), wert: e + 8 };
      }
      return tags;
    };
    const ascii = (tag) => {
      if (!tag) return "";
      const off = tag.anzahl > 4 ? t + u32(tag.wert) : tag.wert;
      let s = "";
      for (let i = 0; i < tag.anzahl && off + i < v.byteLength; i++) {
        const c = v.getUint8(off + i); if (!c) break; s += String.fromCharCode(c);
      }
      return s.trim();
    };
    const rational = (tag, i = 0) => {
      if (!tag) return NaN;
      const off = t + u32(tag.wert) + i * 8;
      if (off + 8 > v.byteLength) return NaN;
      const z = u32(off), n = u32(off + 4);
      return n ? z / n : NaN;
    };
    const kurz = (tag) => (tag ? (tag.typ === 3 ? u16(tag.wert) : u32(tag.wert)) : undefined);
    try {
      const ifd0 = tabelle(u32(t + 4));
      const out = { modell: ascii(ifd0[0x0110]) };
      if (ifd0[0x8769]) {
        const ex = tabelle(u32(ifd0[0x8769].wert));
        out.zeit = ascii(ex[0x9003]) || ascii(ifd0[0x0132]);
        out.f35 = kurz(ex[0xa405]);
      }
      if (ifd0[0x8825]) {
        const g = tabelle(u32(ifd0[0x8825].wert));
        const grad = (tag) => rational(tag, 0) + rational(tag, 1) / 60 + rational(tag, 2) / 3600;
        const lat = grad(g[2]), lon = grad(g[4]);
        if (isFinite(lat) && isFinite(lon) && (lat || lon)) {
          out.lat = ascii(g[1]) === "S" ? -lat : lat;
          out.lon = ascii(g[3]) === "W" ? -lon : lon;
          const acc = rational(g[31]);
          if (isFinite(acc)) out.genauigkeit = acc;
        }
      }
      return out;
    } catch {
      return {};
    }
  }

  // ---------- Handy-GPS für Fotos, die in der App aufgenommen werden ----------
  let gpsWatch = null, letztePosition = null, gpsFehler = "";
  const gpsBeobachter = new Set();
  function gpsStarten(beobachter) {
    if (beobachter) gpsBeobachter.add(beobachter);
    if (gpsWatch !== null) return;
    if (!navigator.geolocation) { gpsFehler = "Dieses Gerät liefert keinen Standort."; return; }
    gpsWatch = navigator.geolocation.watchPosition(
      (pos) => {
        letztePosition = { lat: pos.coords.latitude, lon: pos.coords.longitude, genauigkeit: pos.coords.accuracy, t: Date.now() };
        gpsFehler = "";
        gpsBeobachter.forEach((f) => f(letztePosition));
      },
      (e) => {
        gpsFehler = e.code === 1 ? "Standort nicht erlaubt. Bitte in den Browser-Einstellungen freigeben."
          : "Standort gerade nicht verfügbar.";
        gpsBeobachter.forEach((f) => f(null));
      },
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 20000 });
  }
  const position = () => (letztePosition && Date.now() - letztePosition.t < 60000 ? letztePosition : null);

  // Entfernung zweier GPS-Punkte in Metern (Haversine)
  function abstand(a, b) {
    const r = Math.PI / 180, R = 6371000;
    const dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  // ---------- Signal: Ton + Vibration, Bildschirm wach halten ----------
  let audio = null, wachSperre = null;
  function signalVorbereiten() { // muss bei einem Tipp passieren (iOS)
    try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume?.(); } catch { audio = null; }
  }
  function signal() {
    try { navigator.vibrate?.([120, 80, 120]); } catch { /* egal */ }
    if (!audio) return;
    try {
      const o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(audio.destination);
      const t = audio.currentTime;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);
      o.start(t); o.stop(t + 0.26);
    } catch { /* egal */ }
  }
  async function wachHalten(an) {
    try {
      if (an && "wakeLock" in navigator && document.visibilityState === "visible") wachSperre = await navigator.wakeLock.request("screen");
      if (!an && wachSperre) { await wachSperre.release(); wachSperre = null; }
    } catch { wachSperre = null; }
  }

  // ---------- Fotoqualität grob prüfen (an echten Straßenfotos kalibriert) ----------
  async function qualitaet(blob) {
    const url = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      const w = 320, h = Math.max(2, Math.round(img.naturalHeight * w / img.naturalWidth));
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, w, h);
      const px = ctx.getImageData(0, 0, w, h).data;
      const g = new Float32Array(w * h);
      let summe = 0, hell = 0;
      for (let i = 0; i < w * h; i++) {
        const v = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
        g[i] = v; summe += v; if (v > 245) hell++;
      }
      // Schärfe: Varianz des Laplace-Filters in der unteren Bildhälfte (Fahrbahn)
      let n = 0, m = 0, q = 0;
      for (let y = Math.floor(h / 2) + 1; y < h - 1; y++) {
        for (let x = 1; x < w - 1; x++) {
          const i = y * w + x;
          const l = 4 * g[i] - g[i - 1] - g[i + 1] - g[i - w] - g[i + w];
          n++; const d = l - m; m += d / n; q += d * (l - m);
        }
      }
      const mittel = summe / (w * h), anteilHell = hell / (w * h), schaerfe = n > 1 ? q / (n - 1) : 0;
      const hinweise = [];
      if (mittel < 60) hinweise.push("zu dunkel");
      if (mittel > 190 || anteilHell > 0.15) hinweise.push("überbelichtet oder Gegenlicht");
      if (schaerfe < 60) hinweise.push("unscharf oder verwackelt");
      c.width = c.height = 0;
      return hinweise;
    } catch {
      return [];
    } finally {
      URL.revokeObjectURL(url);
    }
  }
  function gpsStoppen() {
    if (gpsWatch !== null) navigator.geolocation.clearWatch(gpsWatch);
    gpsWatch = null;
    gpsBeobachter.clear();
  }
  const exifZeit = (d) => {
    const z = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}:${z(d.getMonth() + 1)}:${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
  };

  // ---------- Foto übernehmen: Metadaten lesen, verkleinern ----------
  const istJpeg = (f) => /jpe?g$/i.test(f.type) || /\.jpe?g$/i.test(f.name);
  const istHeic = (f) => /hei[cf]/i.test(f.type) || /\.hei[cf]$/i.test(f.name);

  async function verkleinern(datei) {
    const url = URL.createObjectURL(datei);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      const s = Math.min(1, MAX_KANTE / Math.max(img.naturalWidth, img.naturalHeight));
      if (s === 1 && istJpeg(datei)) return datei;
      const c = document.createElement("canvas");
      c.width = Math.round(img.naturalWidth * s);
      c.height = Math.round(img.naturalHeight * s);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.9));
      c.width = c.height = 0; // Speicher am Handy freigeben
      return blob || datei;
    } catch {
      return datei; // z. B. HEIC in Chrome: Original schicken, der Server wandelt um
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  async function uebernehmen(datei, ausKamera) {
    let meta = {};
    if (istJpeg(datei)) {
      try { meta = liesExif(await datei.slice(0, 256 * 1024).arrayBuffer()); } catch { meta = {}; }
    }
    let gpsQuelle = meta.lat != null ? "foto" : "";
    if (ausKamera) {
      if (!meta.zeit) meta.zeit = exifZeit(new Date());
      if (meta.lat == null && letztePosition && Date.now() - letztePosition.t < 60000) {
        Object.assign(meta, { lat: letztePosition.lat, lon: letztePosition.lon, genauigkeit: letztePosition.genauigkeit });
        gpsQuelle = "handy";
      }
    }
    const upload = istHeic(datei) ? datei : await verkleinern(datei);
    const hinweise = istHeic(datei) ? [] : await qualitaet(upload);
    const name = (datei.name && datei.name !== "image.jpg" ? datei.name : `Foto_${exifZeit(new Date()).replace(/\D/g, "")}.jpg`)
      .replace(/\.(heic|heif|png|webp)$/i, istHeic(datei) ? "$&" : ".jpg");
    return {
      id: Math.random().toString(36).slice(2),
      name, upload, meta, gpsQuelle,
      vorschau: istHeic(datei) ? null : URL.createObjectURL(upload),
      hinweise,
      groesse: upload.size,
    };
  }

  // ---------- Hochladen mit Fortschritt ----------
  function hochladen(fotos, felder, fortschritt) {
    return new Promise((res, rej) => {
      const fd = new FormData();
      Object.entries(felder).forEach(([k, v]) => fd.append(k, v ?? ""));
      fd.append("meta", JSON.stringify(fotos.map((f) => f.meta)));
      fotos.forEach((f) => fd.append("fotos", f.upload, f.name));
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/auswertung");
      xhr.timeout = 10 * 60 * 1000;
      xhr.upload.onprogress = (e) => e.lengthComputable && fortschritt(e.loaded / e.total);
      xhr.onload = () => {
        let d = null;
        try { d = JSON.parse(xhr.responseText); } catch { /* unten */ }
        if (xhr.status === 200 && d?.ok) res(d.auftrag);
        else rej(new Error(d?.fehler || `Hochladen fehlgeschlagen (HTTP ${xhr.status}).`));
      };
      xhr.onerror = () => rej(new Error("Keine Verbindung. Sind Sie online?"));
      xhr.ontimeout = () => rej(new Error("Das Hochladen hat zu lange gedauert. Bitte im WLAN erneut versuchen."));
      xhr.send(fd);
    });
  }

  window.RoadAIFotos = {
    uebernehmen, hochladen, gpsStarten, gpsStoppen, liesExif, qualitaet,
    position, abstand, signal, signalVorbereiten, wachHalten,
    gpsFehler: () => gpsFehler,
  };
})();
