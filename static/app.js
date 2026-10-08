/* RoadSense Oberfläche. Kein Framework, damit es am Handy schnell lädt. */
(() => {
  "use strict";

  const ROUTEN = {
    beobachten: { symbol: "✓", farbe: "var(--r-beobachten)" },
    sanierung_planen: { symbol: "▲", farbe: "var(--r-sanierung_planen)" },
    dringend_melden: { symbol: "!", farbe: "var(--r-dringend_melden)" },
    neu_aufnehmen: { symbol: "↺", farbe: "var(--r-neu_aufnehmen)" },
  };
  const KLASSEN = { 1: "sehr gut", 2: "gut", 3: "mittel", 4: "schlecht", 5: "sehr schlecht" };
  const FRAGEN = [
    "Wo ist es am schlimmsten?",
    "Warum diese Bewertung?",
    "Wie genau sind die Werte?",
    "Was soll ich als Nächstes tun?",
  ];

  const $ = (s) => document.querySelector(s);
  const main = $("#inhalt");
  const zustand = {
    status: null, beispiele: [], eigeneStrecken: [], strecke: null, entscheidung: null, verlauf: [],
    ansicht: "start", modul: "condition", beschaeftigt: false, zapier: {},
    aufnahme: { fotos: [], name: "", ort: "", breite: "", auftrag: null, upload: 0, fehler: "", vorbereiten: 0 },
  };

  // ---------- Hilfen ----------
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const zahl = (n, d = 1) => Number(n).toLocaleString("de-AT", { minimumFractionDigits: d, maximumFractionDigits: d });
  const nr2 = (n) => String(n).padStart(2, "0");
  const station = (a) => `${zahl(a.station_von_m, 0)}–${zahl(a.station_bis_m, 0)} m`;
  const datum = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString("de-AT", { day: "2-digit", month: "2-digit", year: "numeric" }); };
  const karte = (a) => (a.lat != null && a.lon != null ? `https://www.google.com/maps?q=${a.lat.toFixed(6)},${a.lon.toFixed(6)}` : "");
  const abschnitt = (n) => zustand.strecke.abschnitte.find((a) => a.nr === n);

  function toast(text) {
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.hidden = true), 3800);
  }

  async function api(pfad, body) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 45000);
    try {
      const r = await fetch(pfad, {
        method: body ? "POST" : "GET",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      });
      let daten;
      try { daten = await r.json(); } catch { throw new Error("Der Server hat unerwartet geantwortet."); }
      if (!r.ok || !daten.ok) throw new Error(daten.fehler || "Unbekannter Fehler.");
      return daten;
    } catch (e) {
      if (e.name === "AbortError") throw new Error("Der Server antwortet nicht. Bitte noch einmal versuchen.");
      if (e instanceof TypeError) throw new Error("Keine Verbindung. Sind Sie online?");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  function speichern() {
    try {
      sessionStorage.setItem("roadai", JSON.stringify({
        strecke: zustand.strecke, entscheidung: zustand.entscheidung, verlauf: zustand.verlauf,
        ansicht: zustand.ansicht === "aufnahme" ? "start" : zustand.ansicht,
        auftrag: zustand.aufnahme.auftrag?.status === "laeuft" || zustand.aufnahme.auftrag?.status === "wartet" ? zustand.aufnahme.auftrag.id : null,
      }));
    } catch { /* privates Fenster: egal */ }
  }
  function wiederherstellen() {
    try {
      const d = JSON.parse(sessionStorage.getItem("roadai") || "null");
      if (d && d.strecke && Array.isArray(d.strecke.abschnitte)) {
        const { auftrag, ...rest } = d;
        Object.assign(zustand, rest);
      }
      if (d && typeof d.auftrag === "string") zustand.weiterVerfolgen = d.auftrag;
    } catch { /* ignorieren */ }
  }

  // Fertig ausgewertete eigene Strecken lokal im Browser merken.
  // Absichtlich kein Server-/Render-Umbau: Die bestehende Render↔Laptop-Architektur bleibt unverändert.
  const EIGENE_KEY = "roadsense.eigeneStrecken.v1";
  function eigeneLaden() {
    try {
      const liste = JSON.parse(localStorage.getItem(EIGENE_KEY) || "[]");
      zustand.eigeneStrecken = Array.isArray(liste)
        ? liste.filter((s) => s && typeof s === "object" && typeof s.strecke_id === "string" && Array.isArray(s.abschnitte)).slice(0, 20)
        : [];
    } catch { zustand.eigeneStrecken = []; }
  }
  function eigeneSpeichern(strecke) {
    if (!strecke || typeof strecke.strecke_id !== "string" || !Array.isArray(strecke.abschnitte)) return;
    try {
      const liste = zustand.eigeneStrecken.filter((s) => s.strecke_id !== strecke.strecke_id);
      liste.unshift(strecke);
      zustand.eigeneStrecken = liste.slice(0, 20);
      localStorage.setItem(EIGENE_KEY, JSON.stringify(zustand.eigeneStrecken));
    } catch { /* Browser-Speicher nicht verfügbar: App läuft trotzdem weiter */ }
  }
  function eigeneBeschreibung(s) {
    const ort = s.ort ? `${s.ort} · ` : "";
    const tag = datum(s.aufnahme);
    const n = Array.isArray(s.abschnitte) ? s.abschnitte.length : 0;
    return `${ort}${tag ? `Auswertung vom ${tag}` : "Eigene Auswertung"}${n ? ` · ${n} Abschnitte` : ""}`;
  }

  // ---------- Navigation ----------
  function zeige(ansicht) {
    if (!["start", "aufnahme"].includes(ansicht) && !zustand.strecke) ansicht = "start";
    if (ansicht !== "aufnahme") {
      window.RoadAIFotos.gpsStoppen();
      window.RoadAIFotos.wachHalten(false);
      zustand.aufnahme.gefuehrt = false;
    }
    document.body.classList.remove("karte-offen");
    zustand.ansicht = ansicht;
    speichern();
    render();
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }

  function render() {
    const a = zustand.ansicht;
    $("#tabs").hidden = a === "start" || a === "aufnahme";
    $("#zurueck").hidden = a === "start";
    document.querySelectorAll("#tabs button").forEach((b) =>
      b.dataset.ansicht === a ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
    main.classList.toggle("mit-eingabe", a === "chat");
    main.classList.toggle("breit", a === "start");
    document.querySelector(".eingabe")?.remove();
    main.innerHTML = { start: ansichtStart, aufnahme: ansichtAufnahme, strecke: ansichtStrecke, ergebnis: ansichtErgebnis, chat: ansichtChat }[a]();
    if (a === "chat") chatEingabe();
    if (a === "aufnahme" && zustand.aufnahme.gefuehrt) aktualisiereAssistent();
  }

  function renderStatus() {
    const s = zustand.status;
    if (!s) return;
    if (s.vorschau) { $("#status").innerHTML = ""; return; }
    $("#status").innerHTML =
      `<span class="pille ${s.ki ? "an" : "aus"}">${s.ki ? "KI aktiv" : "Offline-Modus"}</span>` +
      `<span class="pille ${s.zapier ? "an" : "aus"}">${s.zapier ? "Sheets verbunden" : "Sheets aus"}</span>`;
  }

  // ---------- Ansicht: Start (Module) ----------
  const MODULE = [
    { id: "condition", symbol: "🛣️", name: "RoadSense Condition", titel: "Straßenzustand",
      text: "Straßen automatisch erfassen, Schäden erkennen und Zustand bewerten.",
      nr: 1, lang: "Straßenzustandsmanagement", slogan: "Erfassen. Bewerten. Priorisieren." },
    { id: "planning", symbol: "📅", name: "RoadSense Planning", titel: "Bauprogramm",
      text: "Maßnahmen priorisieren, Budgets planen und Sanierungen dokumentieren.",
      nr: 2, lang: "Bauprogramm-Management", slogan: "Vom Zustand zur Maßnahme." },
    { id: "assets", symbol: "📍", name: "RoadSense Assets", titel: "Inventar",
      text: "Straßeninventar zentral erfassen, verwalten und aktuell halten.",
      nr: 3, lang: "Inventarverwaltung", slogan: "Die gesamte Straßeninfrastruktur auf einer Karte." },
  ];

  function ansichtStart() {
    const aktiv = MODULE.find((m) => m.id === zustand.modul) || MODULE[0];
    const menue = MODULE.map((m) => `
        <button type="button" class="modul${m.id === aktiv.id ? " aktiv" : ""}" data-aktion="modul" data-id="${m.id}"${m.id === aktiv.id ? ' aria-current="page"' : ""}>
          <span class="symbol" aria-hidden="true">${m.symbol}</span>
          <span class="txt"><span class="modul-titel">${m.titel}</span><b>${m.name}</b><span class="klein">${m.text}</span></span>
        </button>`).join("");
    return `
      <div class="startlayout">
        <aside class="seitenmenue" aria-label="Module">
          <div class="eyebrow">Unsere Module</div>
          <nav class="module">${menue}</nav>
          <p class="plattform">Eine Plattform. Ein Straßennetz. Alle Informationen.</p>
        </aside>
        <div class="modulinhalt">
          <section>
            <div class="eyebrow">Modul ${aktiv.nr} · ${aktiv.lang}</div>
            <h1>${aktiv.slogan}</h1>
          </section>
          ${aktiv.id === "condition" ? inhaltCondition() : inhaltVorschau(aktiv)}
        </div>
      </div>`;
  }

  function inhaltCondition() {
    const eigene = zustand.eigeneStrecken.map((s) => `
      <button class="wahl" type="button" data-aktion="laden-lokal" data-id="${esc(s.strecke_id)}">
        <span class="symbol" aria-hidden="true">◆</span>
        <span class="txt"><b>${esc(s.name)}</b><span class="klein">${esc(eigeneBeschreibung(s))}</span></span>
        <span class="pfeil" aria-hidden="true">›</span>
      </button>`).join("");
    const beispiele = zustand.beispiele.map((b) => `
      <button class="wahl" type="button" data-aktion="laden" data-id="${esc(b.id)}">
        <span class="symbol" aria-hidden="true">${b.simuliert ? "◇" : "◆"}</span>
        <span class="txt"><b>${esc(b.name)}</b><span class="klein">${esc(b.beschreibung)}</span></span>
        <span class="pfeil" aria-hidden="true">›</span>
      </button>`).join("");
    const karten = eigene + beispiele;
    return `
      <p class="lead">RoadSense liest die Auswertung der Straßenfotos, entscheidet, was zu tun ist, und leitet Sie zum passenden nächsten Schritt.</p>
      <button class="wahl neu" type="button" data-aktion="aufnehmen">
        <span class="symbol" aria-hidden="true">📷</span>
        <span class="txt"><b>Neue Strecke aufnehmen</b><span class="klein">Mit der Kamera fotografieren oder Fotos aus der Galerie wählen, RoadSense wertet sie aus</span></span>
        <span class="pfeil" aria-hidden="true">›</span>
      </button>
      <button class="wahl flotte" type="button" data-aktion="flotte">
        <span class="symbol" aria-hidden="true">🚗</span>
        <span class="txt"><b>Zustand aus Flottendaten</b><span class="klein">Ausgewertete RoadSense-Flottendaten einsehen</span></span>
        <span class="pfeil" aria-hidden="true">›</span>
      </button>
      <h2 style="margin-top:4px">Oder eine ausgewertete Strecke ansehen</h2>
      <div class="liste" id="strecken">${karten || `<p class="leer">Lade Strecken …</p>`}
      </div>
      ${zustand.strecke ? `<button class="knopf zweit" type="button" data-aktion="weiter">Zurück zu „${esc(zustand.strecke.name)}“</button>` : ""}`;
  }

  function inhaltVorschau(m) {
    const punkte = m.id === "planning"
      ? ["Abschnitte nach Zustandsklasse und Dringlichkeit reihen", "Budgets je Jahr und Straße planen", "Sanierungen dokumentieren und den Zustand danach neu erfassen"]
      : ["Verkehrszeichen, Leitschienen, Bodenmarkierungen und Schächte erfassen", "Alle Objekte auf einer gemeinsamen Karte verwalten", "Änderungen aus neuen Befahrungen automatisch nachführen"];
    return `
      <p class="lead">${m.text}</p>
      <section class="karte">
        <span class="pille-hell">In Vorbereitung</span>
        <h3>${m.name}</h3>
        <ul class="punkte">${punkte.map((p) => `<li>${p}</li>`).join("")}</ul>
        <p class="klein">Dieses Modul baut auf den Zustandsdaten aus RoadSense Condition auf.</p>
      </section>
      <button class="knopf zweit" type="button" data-aktion="modul" data-id="condition">Zu RoadSense Condition</button>`;
  }

  // ---------- Ansicht: Neue Strecke aufnehmen ----------
  const leereAufnahme = () => ({ fotos: [], name: "", ort: "", breite: "", auftrag: null, upload: 0, fehler: "", vorbereiten: 0 });

  function ansichtAufnahme() {
    const A = zustand.aufnahme, st = zustand.status || {};
    const auftrag = A.auftrag;
    const laeuft = auftrag && (auftrag.status === "wartet" || auftrag.status === "laeuft");
    if ((A.upload > 0 && A.upload < 1) || laeuft) {
      const proz = laeuft ? Math.round(5 + 95 * (auftrag.erledigt / Math.max(1, auftrag.gesamt))) : Math.round(A.upload * 100);
      const min = Math.max(1, Math.round((auftrag?.gesamt || 1) * 12 / 60)), max = Math.max(2, Math.round((auftrag?.gesamt || 1) * 20 / 60));
      return `
        <section><div class="eyebrow">Neue Strecke</div><h1>${esc(A.name || "Neue Strecke")}</h1></section>
        <section class="karte">
          <h3>${laeuft ? esc(auftrag.phase) : "Fotos werden hochgeladen …"}</h3>
          <div class="balken" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${proz}"><i style="width:${proz}%"></i></div>
          <p class="klein">${laeuft ? `Die Auswertung dauert etwa ${min}–${max} Minuten. Sie können die App offen lassen oder später zurückkommen.` : `${proz} % hochgeladen. Bitte die App offen lassen.`}</p>
          ${auftrag?.hinweise?.length ? `<p class="hinweis">${esc(auftrag.hinweise.join(" "))}</p>` : ""}
        </section>`;
    }
    if (A.gefuehrt) return ansichtAssistent();
    const fotos = A.fotos;
    const ohneGps = fotos.filter((f) => f.meta.lat == null).length;
    const kacheln = fotos.map((f, i) => `
      <figure class="kachel">
        ${f.vorschau ? `<img src="${f.vorschau}" alt="Foto ${i + 1}">` : `<div class="ohnebild">HEIC</div>`}
        ${f.hinweise?.length ? `<span class="warnung" title="${esc(f.hinweise.join(", "))}">⚠</span>` : ""}
        <figcaption><b>${i + 1}</b><span class="${f.meta.lat != null ? "ok" : "nein"}">${f.meta.lat != null ? (f.gpsQuelle === "handy" ? "GPS Handy" : "GPS") : "kein GPS"}</span></figcaption>
        <button type="button" class="weg" data-aktion="foto-weg" data-id="${f.id}" aria-label="Foto ${i + 1} entfernen">×</button>
      </figure>`).join("");
    const nichtBereit = st.auswertung === false;
    return `
      <section>
        <div class="eyebrow">Neue Strecke</div>
        <h1>Strecke fotografieren</h1>
        <p class="lead" style="margin-top:8px">Etwa alle 5 m ein Foto in Fahrtrichtung, Kamera auf Augenhöhe, beide Fahrbahnränder im Bild.</p>
      </section>
      ${nichtBereit ? `<p class="hinweis">${esc(st.auswertung_hinweis || "Die Bildauswertung ist auf diesem Server noch nicht eingerichtet.")}</p>` : ""}
      ${A.fehler ? `<p class="hinweis fehler" role="alert">${esc(A.fehler)}</p>` : ""}
      <section class="karte felder">
        <label>Name der Strecke<input id="a-name" maxlength="60" value="${esc(A.name)}" placeholder="z. B. Kremser Straße"></label>
        <div class="zwei">
          <label>Ort<input id="a-ort" maxlength="60" value="${esc(A.ort)}" placeholder="z. B. Langenlois"></label>
          <label>Breite in m<input id="a-breite" inputmode="decimal" maxlength="5" value="${esc(A.breite)}" placeholder="optional"></label>
        </div>
        <p class="klein">Die Fahrbahnbreite vor Ort zu messen macht das Ergebnis genauer. Ohne Angabe schätzt RoadSense sie aus den Fotos.</p>
      </section>
      <div class="reihe neben">
        <button class="knopf akzent" type="button" data-aktion="assistent-start">🧭 Geführte Aufnahme starten</button>
        <button class="knopf zweit" type="button" data-aktion="galerie">🖼 Fotos aus Galerie wählen</button>
      </div>
      <p class="klein">Die geführte Aufnahme zeigt beim Gehen den Abstand zum letzten Foto, gibt bei 5 m ein Signal und prüft jedes Foto auf Schärfe und Licht.</p>
      ${A.vorbereiten ? `<p class="klein"><span class="spinner klein-spinner"></span>${A.vorbereiten} Foto(s) werden vorbereitet …</p>` : ""}
      ${fotos.length ? `<section class="karte">
          <h3>${fotos.length} ${fotos.length === 1 ? "Foto" : "Fotos"}</h3>
          <div class="kacheln">${kacheln}</div>
          ${ohneGps ? `<p class="klein">${ohneGps} Foto(s) ohne Standort. Tipp: Fotos mit der geführten Aufnahme machen, dann speichert RoadSense den Standort des Handys mit.</p>` : ""}
          ${fotos.some((f) => f.hinweise?.length) ? `<p class="klein">⚠ = Foto möglicherweise ${esc([...new Set(fotos.flatMap((f) => f.hinweise || []))].join(", "))}. Antippen und mit × entfernen oder neu aufnehmen.</p>` : ""}
        </section>` : `<p class="leer">Noch keine Fotos.</p>`}
      <button class="knopf" type="button" data-aktion="auswerten" ${fotos.length < 2 || A.vorbereiten || nichtBereit ? "disabled" : ""}>
        ${fotos.length < 2 ? "Mindestens 2 Fotos nötig" : `${fotos.length} Fotos auswerten`}
      </button>
      <p class="klein">Die Fotos werden nur für die Auswertung verwendet und danach gelöscht. Bericht und Draufsichten bleiben 24 Stunden abrufbar.</p>`;
  }


  // ---------- Aufnahme-Assistent ----------
  const F = () => window.RoadAIFotos;
  const ZIEL_M = 5, FENSTER_VON = 4, FENSTER_BIS = 6.5;

  function letzteMitGps() {
    const f = [...zustand.aufnahme.fotos].reverse().find((x) => x.meta.lat != null);
    return f ? { lat: f.meta.lat, lon: f.meta.lon } : null;
  }

  function streckenlaenge() {
    const p = zustand.aufnahme.fotos.filter((x) => x.meta.lat != null).map((x) => ({ lat: x.meta.lat, lon: x.meta.lon }));
    let summe = 0;
    for (let i = 1; i < p.length; i++) summe += F().abstand(p[i - 1], p[i]);
    return summe;
  }

  function ansichtAssistent() {
    const A = zustand.aufnahme, fotos = A.fotos, letztes = fotos[fotos.length - 1];
    const tipps = A.tippsGesehen ? "" : `
      <section class="karte tipps">
        <h3>Vor dem Start</h3>
        <ul class="check">
          <li>Auf den Verkehr achten. Am Fahrbahnrand gehen, Warnweste tragen.</li>
          <li>Kamera auf Augenhöhe, Blick in Fahrtrichtung, beide Fahrbahnränder im Bild.</li>
          <li>Am Anfang der Strecke das erste Foto machen, dann alle 5 m eines. RoadSense meldet sich.</li>
          <li>Bei der ersten Aufnahme den Standort erlauben.</li>
        </ul>
        <button class="knopf zweit" type="button" data-aktion="tipps-ok">Verstanden</button>
      </section>`;
    const kacheln = fotos.slice(-8).map((f) => {
      const i = fotos.indexOf(f);
      return `<figure class="kachel mini">${f.vorschau ? `<img src="${f.vorschau}" alt="Foto ${i + 1}">` : `<div class="ohnebild">HEIC</div>`}
        ${f.hinweise?.length ? `<span class="warnung">⚠</span>` : ""}<figcaption><b>${i + 1}</b></figcaption></figure>`;
    }).join("");
    return `
      <section class="assist-kopf">
        <div><div class="eyebrow">Geführte Aufnahme</div><h2>${esc(A.name || "Neue Strecke")}</h2></div>
        <button class="knopf zweit kompakt" type="button" data-aktion="assistent-ende">Fertig</button>
      </section>
      ${tipps}
      <section class="abstand" id="abstand" aria-live="polite"></section>
      <button class="ausloeser" type="button" data-aktion="assistent-foto" ${A.vorbereiten ? "disabled" : ""}>
        <span aria-hidden="true">📷</span>${A.vorbereiten ? "Foto wird geprüft …" : A.ersetzen ? "Foto ersetzen" : fotos.length ? "Nächstes Foto" : "Erstes Foto"}
      </button>
      ${letztes ? `<section class="karte letztes ${letztes.hinweise?.length ? "warn" : "gut"}">
          <div class="letztes-zeile">
            ${letztes.vorschau ? `<img src="${letztes.vorschau}" alt="Letztes Foto">` : ""}
            <div>
              <b>Foto ${fotos.length}${letztes.meta.lat != null ? "" : " · ohne Standort"}</b>
              <p class="klein">${letztes.hinweise?.length ? `⚠ Möglicherweise ${esc(letztes.hinweise.join(", "))}. Besser neu aufnehmen.` : "✓ Schärfe und Licht in Ordnung."}</p>
            </div>
          </div>
          <button class="knopf zweit kompakt" type="button" data-aktion="foto-ersetzen" data-id="${letztes.id}">↺ Letztes Foto neu aufnehmen</button>
        </section>` : ""}
      <p class="statistik">${fotos.length} ${fotos.length === 1 ? "Foto" : "Fotos"} · ca. ${zahl(streckenlaenge(), 0)} m Strecke</p>
      ${kacheln ? `<div class="kacheln-zeile">${kacheln}</div>` : ""}`;
  }

  function aktualisiereAssistent() {
    const el = $("#abstand"), A = zustand.aufnahme;
    if (!el || !A.gefuehrt) return;
    const pos = F().position(), fehler = F().gpsFehler(), basis = letzteMitGps();
    let art = "warten", titel = "", zahlText = "", text = "", fortschritt = 0;
    if (fehler && !pos) { art = "fehler"; titel = "Kein Standort"; text = fehler; }
    else if (!pos) { titel = "Warte auf GPS …"; text = "Kurz still halten, unter freiem Himmel geht es schneller."; }
    else if (!A.fotos.length) { art = "bereit"; titel = "Bereit"; text = "Am Anfang der Strecke das erste Foto machen."; }
    else if (!basis) { art = "bereit"; titel = "Abstand schätzen"; text = "Beim letzten Foto fehlte der Standort. Etwa 6–7 Schritte weitergehen, dann fotografieren."; }
    else {
      const d = F().abstand(basis, pos);
      zahlText = `${zahl(d, 1)} m`;
      fortschritt = Math.min(1, d / ZIEL_M);
      if (d < FENSTER_VON) { art = "gehen"; titel = "Weitergehen"; text = `noch ca. ${zahl(Math.max(0, ZIEL_M - d), 1)} m bis zum nächsten Foto`; A.signalGegeben = false; }
      else if (d <= FENSTER_BIS) {
        art = "jetzt"; titel = "Jetzt fotografieren!"; text = "Stehen bleiben, Kamera in Fahrtrichtung.";
        if (!A.signalGegeben) { A.signalGegeben = true; F().signal(); }
      } else { art = "weit"; titel = "Zu weit gegangen"; text = "Jetzt fotografieren und beim nächsten Mal früher stehen bleiben."; }
    }
    const genau = pos ? `GPS ±${zahl(pos.genauigkeit, 0)} m${pos.genauigkeit > 12 ? " · ungenau, Abstand nur grob" : ""}` : "";
    el.dataset.art = art;
    el.innerHTML = `
      <div class="abstand-titel">${art === "warten" ? `<span class="spinner klein-spinner"></span>` : ""}${esc(titel)}</div>
      ${zahlText ? `<div class="abstand-zahl">${zahlText}</div>
        <div class="balken"><i style="width:${Math.round(fortschritt * 100)}%"></i></div>` : ""}
      <p>${esc(text)}</p>
      ${genau ? `<p class="klein">${esc(genau)}</p>` : ""}`;
  }

  function assistentStarten() {
    const A = zustand.aufnahme;
    felderMerken();
    A.gefuehrt = true;
    A.signalGegeben = false;
    F().signalVorbereiten();
    F().gpsStarten(aktualisiereAssistent);
    F().wachHalten(true);
    render();
    window.scrollTo({ top: 0 });
  }

  function assistentBeenden() {
    const A = zustand.aufnahme;
    A.gefuehrt = false;
    A.ersetzen = null;
    F().wachHalten(false);
    render();
    window.scrollTo({ top: 0 });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && zustand.aufnahme.gefuehrt) {
      F().wachHalten(true);
      aktualisiereAssistent();
    }
  });

  function felderMerken() {
    const A = zustand.aufnahme;
    if ($("#a-name")) { A.name = $("#a-name").value; A.ort = $("#a-ort").value; A.breite = $("#a-breite").value; }
  }

  async function fotosDazu(dateien, ausKamera) {
    const A = zustand.aufnahme, max = zustand.status?.max_fotos || 30;
    felderMerken();
    dateien = [...dateien].filter((f) => f.type.startsWith("image/") || /\.(hei[cf]|jpe?g|png)$/i.test(f.name));
    if (!dateien.length) return toast("Bitte Fotos auswählen (JPG oder HEIC).");
    if (A.fotos.length + dateien.length > max) {
      toast(`Höchstens ${max} Fotos pro Strecke.`);
      dateien = dateien.slice(0, Math.max(0, max - A.fotos.length));
    }
    A.vorbereiten += dateien.length;
    A.fehler = "";
    render();
    for (const d of dateien) { // nacheinander, damit das Handy nicht zu viel Speicher braucht
      try {
        const neu = await window.RoadAIFotos.uebernehmen(d, ausKamera);
        const i = A.ersetzen ? A.fotos.findIndex((x) => x.id === A.ersetzen) : -1;
        if (i >= 0) {
          if (A.fotos[i].vorschau) URL.revokeObjectURL(A.fotos[i].vorschau);
          A.fotos[i] = neu;
        } else {
          A.fotos.push(neu);
        }
        A.ersetzen = null;
        A.signalGegeben = false;
      }
      catch { toast(`${d.name || "Ein Foto"} konnte nicht gelesen werden.`); }
      A.vorbereiten--;
      if (zustand.ansicht === "aufnahme") { render(); aktualisiereAssistent(); }
    }
  }

  async function auswertungStarten() {
    const A = zustand.aufnahme;
    felderMerken();
    if (A.fotos.length < 2 || zustand.beschaeftigt || A.vorbereiten) return;
    const b = A.breite.trim().replace(",", ".");
    if (b && !(Number(b) >= 2 && Number(b) <= 20)) {
      A.fehler = "Die Fahrbahnbreite muss zwischen 2 und 20 m liegen, z. B. 5,80.";
      return render();
    }
    zustand.beschaeftigt = "upload";
    A.fehler = "";
    A.upload = 0.001;
    render();
    try {
      A.auftrag = await window.RoadAIFotos.hochladen(A.fotos, { name: A.name.trim(), ort: A.ort.trim(), breite: b },
        (p) => { A.upload = Math.min(p, 0.999); if (zustand.ansicht === "aufnahme") render(); });
      A.upload = 1;
      speichern();
      verfolgen();
    } catch (e) {
      A.upload = 0;
      A.fehler = e.message;
    }
    zustand.beschaeftigt = false;
    render();
  }

  async function verfolgen(id) {
    const A = zustand.aufnahme;
    id = id || A.auftrag?.id;
    if (!id) return;
    clearTimeout(verfolgen.timer);
    try {
      const d = await api(`/api/auswertung/${encodeURIComponent(id)}`);
      A.auftrag = d.auftrag;
      if (d.auftrag.status === "fertig") {
        A.fotos.forEach((f) => f.vorschau && URL.revokeObjectURL(f.vorschau));
        zustand.aufnahme = leereAufnahme();
        eigeneSpeichern(d.auftrag.strecke);
        neueStrecke(d.auftrag.strecke);
        toast("Auswertung fertig und unter ausgewerteten Strecken gespeichert.");
        return;
      }
      if (d.auftrag.status === "fehler") {
        A.fehler = d.auftrag.fehler;
        A.auftrag = null;
        A.upload = 0;
        speichern();
        if (zustand.ansicht === "aufnahme") render(); else toast(A.fehler);
        return;
      }
      if (zustand.ansicht === "aufnahme") render();
    } catch (e) {
      if (/gibt es nicht/.test(e.message)) {
        A.auftrag = null;
        A.upload = 0;
        speichern();
        return render();
      }
    }
    verfolgen.timer = setTimeout(() => verfolgen(id), 2500);
  }

  // ---------- Ansicht: Strecke ----------
  function ansichtStrecke() {
    const s = zustand.strecke, k = s.kennzahlen;
    const band = s.abschnitte.map((a) => {
      const kl = a.bildqualitaet === "unbrauchbar" ? "kx" : "k" + a.klasse;
      return `<a href="#ab-${nr2(a.nr)}" class="${kl}" data-aktion="springe" data-nr="${a.nr}" title="Abschnitt ${nr2(a.nr)}: ${zahl(a.schadanteil_pct)} %">${nr2(a.nr)}</a>`;
    }).join("");
    const liste = s.abschnitte.map((a) => {
      const unbr = a.bildqualitaet === "unbrauchbar";
      const link = karte(a);
      return `
      <details class="ab" id="ab-${nr2(a.nr)}">
        <summary>
          <span class="nr ${unbr ? "kx" : "k" + a.klasse}">${nr2(a.nr)}</span>
          <span class="st">Station ${station(a)}<br><span class="klein">${unbr ? "Foto nicht auswertbar" : `Klasse ${a.klasse} · ${KLASSEN[a.klasse]}`}</span></span>
          <span class="pct">${unbr ? "–" : zahl(a.schadanteil_pct) + " %"}</span>
        </summary>
        <dl class="kv">
          <div><dt>Längsrisse</dt><dd>${zahl(a.laengsrisse_m)} m</dd></div>
          <div><dt>Querrisse</dt><dd>${zahl(a.querrisse_m)} m</dd></div>
          <div><dt>Netzrisse (Fläche)</dt><dd>${zahl(a.netzrisse_m2, 2)} m²</dd></div>
          <div><dt>Flickstellen</dt><dd>${zahl(a.flickstellen_m2, 2)} m²</dd></div>
          <div><dt>Schadfläche</dt><dd>${zahl(a.schaden_m2, 2)} m²</dd></div>
          <div><dt>Foto</dt><dd>${esc(a.foto || "–")}</dd></div>
        </dl>
        ${a.bild || a.foto_bild ? `<div class="bilder">
          ${a.foto_bild ? `<figure><img src="${esc(a.foto_bild)}" alt="Foto ${esc(a.foto)} mit Auswertefläche" loading="lazy"><figcaption class="klein">Foto mit Auswertefläche</figcaption></figure>` : ""}
          ${a.bild ? `<figure><img src="${esc(a.bild)}" alt="Draufsicht Abschnitt ${nr2(a.nr)} mit markierten Schäden" loading="lazy"><figcaption class="klein">Draufsicht, Schäden markiert</figcaption></figure>` : ""}
        </div>` : ""}
        ${a.hinweis ? `<p class="klein" style="padding:0 14px 8px">${esc(a.hinweis)}</p>` : ""}
        ${link ? `<div class="links"><a href="${link}" target="_blank" rel="noopener">In Google Maps öffnen ↗</a></div>` : ""}
      </details>`;
    }).join("");
    const grenzen = s.grenzen.length ? `
      <details class="karte"><summary><b>Grenzen der Auswertung</b></summary>
        <ul style="margin:8px 0 0;padding-left:1.1em">${s.grenzen.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>
      </details>` : "";
    return `
      <section style="display:flex;flex-direction:column;gap:6px">
        ${s.simuliert ? `<span class="marke-sim">Simulierte Daten</span>` : `<div class="eyebrow">Auswertung</div>`}
        <h1>${esc(s.name)}</h1>
        <p class="klein">${esc([s.ort, datum(s.aufnahme), `Fahrbahn ${zahl(s.fahrbahnbreite_m, 2)} m`].filter(Boolean).join(" · "))}</p>
        ${s.bericht ? `<a class="klein" href="${esc(s.bericht)}" target="_blank" rel="noopener">Ausführlichen Prüfbericht mit allen Bildern öffnen ↗</a>` : ""}
      </section>
      ${s.nicht_ausgewertet?.length ? `<p class="hinweis">${s.nicht_ausgewertet.length} Foto(s) konnten nicht ausgewertet werden: ${esc(s.nicht_ausgewertet.map((x) => x.foto).join(", "))}</p>` : ""}
      <div class="kpis">
        <div class="kpi"><b>${zahl(k.schadanteil_pct)} %</b><span>Schadanteil gesamt</span></div>
        <div class="kpi"><b>Klasse ${k.klasse_gesamt}</b><span>${KLASSEN[k.klasse_gesamt]}</span></div>
        <div class="kpi"><b>${zahl(k.schaden_m2)} m²</b><span>Schadfläche</span></div>
        <div class="kpi"><b>${zahl(k.laengsrisse_m + k.querrisse_m, 0)} m</b><span>Längs- und Querrisse</span></div>
      </div>
      <section class="karte">
        <h3>Zustand je Abschnitt</h3>
        <div class="band" role="list">${band}</div>
        <ul class="legende">${[1, 2, 3, 4, 5].map((n) => `<li><i class="k${n}"></i>${n} ${KLASSEN[n]}</li>`).join("")}</ul>
      </section>
      ${s.karte ? `<section class="karte">
        <h3>Karte</h3>
        <div class="kartenbox" id="kartenbox">
          <iframe class="kartenrahmen" src="${esc(s.karte)}" title="Luftbildkarte mit Zustandsklassen je Abschnitt" loading="lazy"></iframe>
          <button class="vollbild-knopf" type="button" data-aktion="karte-vollbild" aria-expanded="false">⤢ Vollbild</button>
        </div>
        ${s.karte.startsWith("data:") ? "" : `<a class="klein" href="${esc(s.karte)}" target="_blank" rel="noopener">Karte in neuem Tab öffnen ↗</a>`}
      </section>` : ""}
      <button class="knopf akzent" type="button" data-aktion="bewerten" ${zustand.beschaeftigt ? "disabled" : ""}>
        ${zustand.beschaeftigt ? `<span class="spinner"></span>RoadSense bewertet …` : zustand.entscheidung ? "Bewertung ansehen" : "KI-Bewertung starten"}
      </button>
      <section class="liste"><h2>Abschnitte</h2>${liste}</section>
      ${grenzen}`;
  }

  // ---------- Ansicht: Ergebnis (Brain is Alive) ----------
  function ansichtErgebnis() {
    const e = zustand.entscheidung;
    if (!e) {
      return `<div class="leer"><p>Diese Strecke wurde noch nicht bewertet.</p></div>
        <button class="knopf akzent" type="button" data-aktion="bewerten" ${zustand.beschaeftigt ? "disabled" : ""}>
          ${zustand.beschaeftigt ? `<span class="spinner"></span>RoadSense bewertet …` : "KI-Bewertung starten"}</button>`;
    }
    const r = ROUTEN[e.route];
    const herkunft = { ki: "Entschieden von der KI", "ki+regel": "KI-Vorschlag, durch Messwerte verschärft", regel: "Entschieden nach Regeln" }[e.quelle] || "";
    const kopf = `
      <section class="route" style="--rc:${r.farbe}">
        <div class="eyebrow">${r.symbol} Ergebnis für ${esc(zustand.strecke.name)}</div>
        <h1>${esc(e.titel)}</h1>
        <p>${esc(e.begruendung)}</p>
        <span class="herkunft">${esc(herkunft)}</span>
      </section>
      ${e.hinweis ? `<p class="hinweis">${esc(e.hinweis)}</p>` : ""}
      ${zustand.strecke.simuliert ? `<p class="hinweis">Achtung: Diese Strecke enthält simulierte Werte zum Vorführen.</p>` : ""}
      <section class="karte"><div class="eyebrow">Nächster Schritt</div><p>${esc(e.naechster_schritt)}</p></section>`;
    const teil = { dringend_melden: teilDringend, sanierung_planen: teilSanierung, beobachten: teilBeobachten, neu_aufnehmen: teilNeu }[e.route]();
    return kopf + teil + `
      <div class="reihe neben">
        <button class="knopf zweit" type="button" data-ansicht="chat">Assistent fragen</button>
        <button class="knopf zweit" type="button" data-aktion="neu-bewerten" ${zustand.beschaeftigt ? "disabled" : ""}>Neu bewerten</button>
      </div>`;
  }

  function prioListe(nummern) {
    if (!nummern.length) return `<p class="klein">Keine Abschnitte priorisiert.</p>`;
    return `<ol class="prio">${nummern.map(abschnitt).filter(Boolean).map((a) => {
      const link = karte(a);
      return `<li><span class="nr k${a.klasse}">${nr2(a.nr)}</span>
        <span class="t">Station ${station(a)} · <b>${zahl(a.schadanteil_pct)} %</b> · Klasse ${a.klasse}<br>
        <span class="klein">${zahl(a.laengsrisse_m)} m Längs-, ${zahl(a.querrisse_m)} m Querrisse${a.netzrisse_m2 > 0 ? `, ${zahl(a.netzrisse_m2, 2)} m² Netzrisse` : ""}</span></span>
        ${link ? `<a href="${link}" target="_blank" rel="noopener" aria-label="Abschnitt ${nr2(a.nr)} in Google Maps">Karte</a>` : ""}</li>`;
    }).join("")}</ol>`;
  }

  function zapierBlock(text) {
    const e = zustand.entscheidung, s = zustand.status || {};
    const ergebnis = zustand.zapier[e.entscheidung_id];
    let info = "";
    if (ergebnis) info = `<p class="${ergebnis.ok ? "ok" : "err"}">${esc(ergebnis.meldung)}</p>`;
    else if (!s.zapier) info = `<p class="klein">Zapier ist am Server noch nicht eingerichtet. Siehe Anleitung (ZAPIER_WEBHOOK_URL).</p>`;
    return `<section class="karte zap">
      <h3>${esc(text)}</h3>
      <p class="klein">Überträgt Strecke, Kennzahlen und diese Bewertung als neue Zeile in Google Sheets.</p>
      <button class="knopf" type="button" data-aktion="zapier" ${ergebnis?.ok || zustand.beschaeftigt ? "disabled" : ""}>
        ${zustand.beschaeftigt === "zapier" ? `<span class="spinner"></span>Wird übertragen …` : ergebnis?.ok ? "✓ Eingetragen" : "In Google Sheets eintragen"}
      </button>${info}</section>`;
  }

  function meldungText() {
    const s = zustand.strecke, e = zustand.entscheidung;
    const nummern = e.prioritaet_abschnitte.length ? e.prioritaet_abschnitte : s.abschnitte.filter((a) => a.klasse >= 4).map((a) => a.nr);
    const zeilen = nummern.map(abschnitt).filter(Boolean).map((a) =>
      `- Abschnitt ${nr2(a.nr)}, Station ${station(a)}: ${zahl(a.schadanteil_pct)} % Schadanteil (Klasse ${a.klasse}), ` +
      `${zahl(a.laengsrisse_m)} m Längsrisse, ${zahl(a.querrisse_m)} m Querrisse${karte(a) ? `\n  Karte: ${karte(a)}` : ""}`);
    return `Sehr geehrte Damen und Herren,

bei der Zustandsaufnahme der Strecke „${s.name}“${s.ort ? ` (${s.ort})` : ""} am ${datum(s.aufnahme)} wurden folgende Abschnitte als vordringlich eingestuft:

${zeilen.join("\n")}

Wir bitten um Begutachtung vor Ort und Rückmeldung zu möglichen Maßnahmen.

Hinweis: Automatische Auswertung mit RoadSense, Werte etwa ±10 %.

Mit freundlichen Grüßen`;
  }

  function teilDringend() {
    const e = zustand.entscheidung;
    const betreff = `Straßenschaden ${zustand.strecke.name}: Bitte um Begutachtung`;
    const mail = `mailto:?subject=${encodeURIComponent(betreff)}&body=${encodeURIComponent(meldungText())}`;
    return `
      <section class="karte"><h3>Diese Abschnitte zuerst</h3>${prioListe(e.prioritaet_abschnitte)}</section>
      <section class="karte">
        <h3>Meldung an die Straßenmeisterei</h3>
        <p class="klein">Vorbereitet aus den Messwerten. Sie können den Text vor dem Senden anpassen.</p>
        <label for="meldung" class="klein">Text der Meldung</label>
        <textarea id="meldung">${esc(meldungText())}</textarea>
        <div class="reihe neben">
          <button class="knopf zweit" type="button" data-aktion="kopieren">Text kopieren</button>
          <a class="knopf zweit" href="${esc(mail)}" style="text-decoration:none">Als E-Mail öffnen</a>
        </div>
      </section>
      ${zapierBlock("Meldung protokollieren")}`;
  }

  function teilSanierung() {
    const e = zustand.entscheidung, k = zustand.strecke.kennzahlen;
    return `
      <section class="karte"><h3>Sanierungsliste nach Priorität</h3>${prioListe(e.prioritaet_abschnitte)}</section>
      <section class="karte">
        <h3>Was zu sanieren ist</h3>
        <p>${zahl(k.laengsrisse_m)} m Längsrisse, ${zahl(k.querrisse_m)} m Querrisse, ${zahl(k.netzrisse_m2, 2)} m² Netzrisse und ${zahl(k.flickstellen_m2, 2)} m² Flickstellen auf ${zahl(k.laenge_m, 0)} m Strecke.</p>
        <p class="klein">Für eine Kostenschätzung braucht es Einheitspreise oder ein Angebot.</p>
      </section>
      ${zapierBlock("In die Sanierungsplanung übernehmen")}`;
  }

  function teilBeobachten() {
    const naechste = new Date(zustand.strecke.aufnahme || Date.now());
    if (isNaN(naechste)) naechste.setTime(Date.now());
    naechste.setFullYear(naechste.getFullYear() + 1);
    return `
      <section class="karte">
        <h3>Wiederholungsbefahrung</h3>
        <p>Empfohlen ab <b>${naechste.toLocaleDateString("de-AT", { month: "long", year: "numeric" })}</b>, damit sich Veränderungen früh zeigen.</p>
        <button class="knopf zweit" type="button" data-aktion="kalender" data-datum="${naechste.toISOString().slice(0, 10)}">Termin in den Kalender</button>
      </section>
      ${zapierBlock("Im Straßenregister vermerken")}`;
  }

  function teilNeu() {
    const s = zustand.strecke;
    const schlecht = s.abschnitte.filter((a) => a.bildqualitaet === "unbrauchbar");
    return `
      <section class="karte">
        <h3>Nicht auswertbare Fotos</h3>
        <p>${schlecht.length ? schlecht.map((a) => `${esc(a.foto || "Abschnitt " + nr2(a.nr))} (Station ${station(a)})`).join(", ") : "Keine auswertbaren Abschnitte."}</p>
      </section>
      <section class="karte">
        <h3>So gelingt die neue Aufnahme</h3>
        <ul class="check">
          <li>Bei gleichmäßigem Licht fotografieren, kein Gegenlicht und keine harten Schatten.</li>
          <li>Beide Fahrbahnränder müssen im Bild sein.</li>
          <li>Etwa alle 5 m ein Foto, Kamera auf Augenhöhe, Blick in Fahrtrichtung.</li>
          <li>Keine Fahrzeuge oder Personen im Bereich 4–9 m vor der Kamera.</li>
          <li>Standortdienste für die Kamera einschalten, damit GPS gespeichert wird.</li>
        </ul>
      </section>
      <button class="knopf" type="button" data-aktion="start">Andere Strecke wählen</button>`;
  }

  // ---------- Ansicht: Chat (Turing Test) ----------
  function ansichtChat() {
    const v = zustand.verlauf;
    const nachrichten = v.length ? v.map((m) => `<div class="msg ${m.role}">${esc(m.content)}${m.quelle === "offline" ? `<span class="q">Offline-Antwort ohne KI</span>` : ""}</div>`).join("")
      : `<div class="msg assistant">Grüß Gott! Ich kenne die Auswertung von „${esc(zustand.strecke.name)}“. Fragen Sie mich zu Schäden, Abschnitten oder dem nächsten Schritt.</div>`;
    return `
      <section><div class="eyebrow">Assistent</div><h2>Fragen zur Strecke</h2></section>
      <div class="chat" id="chat" aria-live="polite">${nachrichten}${zustand.beschaeftigt === "chat" ? `<div class="msg assistant tippt">RoadSense denkt nach …</div>` : ""}</div>
      <div class="chips">${FRAGEN.map((f) => `<button class="chip" type="button" data-aktion="frage" data-text="${esc(f)}">${esc(f)}</button>`).join("")}</div>`;
  }

  function chatEingabe() {
    const div = document.createElement("div");
    div.className = "eingabe";
    div.innerHTML = `<form id="chatform" autocomplete="off">
      <label for="frage" class="klein" style="position:absolute;left:-9999px">Ihre Frage</label>
      <input id="frage" name="frage" maxlength="600" placeholder="Frage an RoadSense …" enterkeyhint="send">
      <button class="knopf akzent" type="submit" ${zustand.beschaeftigt ? "disabled" : ""}>Senden</button></form>`;
    document.body.appendChild(div);
    div.querySelector("form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const feld = div.querySelector("input");
      fragen(feld.value);
      feld.value = "";
    });
    const chat = $("#chat");
    chat?.lastElementChild?.scrollIntoView({ block: "end" });
  }

  // ---------- Aktionen ----------
  async function laden(id) {
    try {
      const d = await api(`/api/beispiele/${encodeURIComponent(id)}`);
      neueStrecke(d.strecke);
    } catch (e) { toast(e.message); }
  }

  function ladenLokal(id) {
    const s = zustand.eigeneStrecken.find((x) => x.strecke_id === id);
    if (!s) return toast("Diese gespeicherte Strecke wurde nicht gefunden.");
    neueStrecke(s);
  }

  function neueStrecke(strecke) {
    zustand.strecke = strecke;
    zustand.entscheidung = null;
    zustand.verlauf = [];
    zeige("strecke");
  }

  async function hochladen(datei) {
    if (!datei) return;
    if (datei.size > 1_000_000) return toast("Die Datei ist zu groß (höchstens 1 MB).");
    let roh;
    try { roh = JSON.parse(await datei.text()); } catch { return toast("Die Datei ist kein gültiges JSON."); }
    try {
      const d = await api("/api/pruefen", { strecke: roh });
      eigeneSpeichern(d.strecke);
      neueStrecke(d.strecke);
      toast("Auswertung geladen und gespeichert.");
    } catch (e) { toast(e.message); }
  }

  async function bewerten(erzwingen = false) {
    if (zustand.beschaeftigt) return;
    if (zustand.entscheidung && !erzwingen) return zeige("ergebnis");
    zustand.beschaeftigt = "bewerten";
    render();
    try {
      const d = await api("/api/entscheidung", { strecke: zustand.strecke });
      zustand.entscheidung = d.entscheidung;
      zustand.verlauf = [];
      zustand.beschaeftigt = false;
      zeige("ergebnis");
    } catch (e) {
      zustand.beschaeftigt = false;
      render();
      toast(e.message);
    }
  }

  async function zapier() {
    const e = zustand.entscheidung;
    if (!e || zustand.beschaeftigt || zustand.zapier[e.entscheidung_id]?.ok) return;
    zustand.beschaeftigt = "zapier";
    render();
    try {
      const d = await api("/api/zapier", { strecke: zustand.strecke, entscheidung: e });
      zustand.zapier[e.entscheidung_id] = { ok: true, meldung: d.meldung };
    } catch (err) {
      zustand.zapier[e.entscheidung_id] = { ok: false, meldung: err.message };
    }
    zustand.beschaeftigt = false;
    render();
  }

  async function fragen(text) {
    text = String(text || "").trim();
    if (!text || zustand.beschaeftigt) return;
    if (text.length > 600) return toast("Bitte kürzer fassen (höchstens 600 Zeichen).");
    zustand.verlauf.push({ role: "user", content: text });
    zustand.beschaeftigt = "chat";
    render();
    try {
      const d = await api("/api/chat", {
        strecke: zustand.strecke,
        entscheidung: zustand.entscheidung,
        verlauf: zustand.verlauf.map(({ role, content }) => ({ role, content })),
      });
      zustand.verlauf.push({ role: "assistant", content: d.antwort, quelle: d.quelle });
    } catch (e) {
      zustand.verlauf.pop();
      toast(e.message);
    }
    zustand.beschaeftigt = false;
    speichern();
    render();
    $("#frage")?.focus();
  }

  function kalender(tag) {
    const s = zustand.strecke;
    const d = tag.replace(/-/g, "");
    const ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RoadSense//DE", "BEGIN:VEVENT",
      `UID:${Date.now()}@roadsense`, `DTSTAMP:${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}Z`,
      `DTSTART;VALUE=DATE:${d}`, `SUMMARY:RoadSense Wiederholungsbefahrung ${s.name.replace(/[,;\\]/g, " ")}`,
      "DESCRIPTION:Strecke erneut fotografieren und mit RoadSense auswerten.", "END:VEVENT", "END:VCALENDAR"].join("\r\n");
    const url = URL.createObjectURL(new Blob([ics], { type: "text/calendar" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "roadsense-wiederholung.ics" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  // ---------- Karte im Vollbild (ohne Neuladen der Karte) ----------
  function karteVollbild(an) {
    const box = $("#kartenbox");
    if (!box) return;
    an = an ?? !box.classList.contains("voll");
    box.classList.toggle("voll", an);
    document.body.classList.toggle("karte-offen", an);
    const k = box.querySelector(".vollbild-knopf");
    k.textContent = an ? "✕ Schließen" : "⤢ Vollbild";
    k.setAttribute("aria-expanded", String(an));
    if (an) k.focus();
  }
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") karteVollbild(false); });

  // ---------- Konto (simuliert: Magistrat Krems ist angemeldet) ----------
  function kontoReiter(id) {
    document.querySelectorAll("#konto [data-aktion='konto-reiter']").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.id === id)));
    $("#form-anmelden").hidden = id !== "anmelden";
    $("#form-registrieren").hidden = id !== "registrieren";
  }
  function kontoOeffnen() {
    const d = $("#konto");
    kontoReiter("anmelden");
    if (d.showModal) d.showModal(); else d.setAttribute("open", "");
  }
  document.querySelectorAll("#konto form").forEach((f) => f.addEventListener("submit", (ev) => {
    ev.preventDefault();
    f.reset();
    $("#konto").close();
    toast(f.id === "form-anmelden" ? "Anmeldung ist in dieser Demo noch nicht aktiv." : "Registrierung ist in dieser Demo noch nicht aktiv.");
  }));
  $("#konto").addEventListener("click", (ev) => { if (ev.target.id === "konto") ev.target.close(); });

  // ---------- Ereignisse ----------
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-aktion],[data-ansicht]");
    if (!el) return;
    if (el.dataset.ansicht) return zeige(el.dataset.ansicht);
    const aktion = el.dataset.aktion;
    if (aktion === "springe") {
      ev.preventDefault();
      const d = document.getElementById("ab-" + nr2(el.dataset.nr));
      if (d) { d.open = true; d.scrollIntoView({ behavior: "smooth", block: "start" }); }
    } else if (aktion === "laden") laden(el.dataset.id);
    else if (aktion === "laden-lokal") ladenLokal(el.dataset.id);
    else if (aktion === "hochladen") $("#datei").click();
    else if (aktion === "aufnehmen") { zeige("aufnahme"); statusLaden(); }
    else if (aktion === "modul") { zustand.modul = el.dataset.id; render(); window.scrollTo({ top: 0 }); }
    else if (aktion === "flotte") {
      laden("TS01");
    }
    else if (aktion === "konto") kontoOeffnen();
    else if (aktion === "konto-zu") $("#konto").close();
    else if (aktion === "konto-reiter") kontoReiter(el.dataset.id);
    else if (aktion === "abmelden") toast("Abmelden ist in dieser Demo noch nicht aktiv.");
    else if (aktion === "assistent-start") assistentStarten();
    else if (aktion === "assistent-ende") assistentBeenden();
    else if (aktion === "tipps-ok") { zustand.aufnahme.tippsGesehen = true; render(); aktualisiereAssistent(); }
    else if (aktion === "assistent-foto") { F().signalVorbereiten(); F().gpsStarten(aktualisiereAssistent); $("#kamera").click(); }
    else if (aktion === "foto-ersetzen") { zustand.aufnahme.ersetzen = el.dataset.id; F().gpsStarten(aktualisiereAssistent); $("#kamera").click(); }
    else if (aktion === "kamera") { felderMerken(); window.RoadAIFotos.gpsStarten(); $("#kamera").click(); }
    else if (aktion === "galerie") { felderMerken(); $("#galerie").click(); }
    else if (aktion === "auswerten") auswertungStarten();
    else if (aktion === "foto-weg") {
      felderMerken();
      const A = zustand.aufnahme, f = A.fotos.find((x) => x.id === el.dataset.id);
      if (f?.vorschau) URL.revokeObjectURL(f.vorschau);
      A.fotos = A.fotos.filter((x) => x.id !== el.dataset.id);
      render();
    }
    else if (aktion === "weiter") zeige("strecke");
    else if (aktion === "start") zeige("start");
    else if (aktion === "bewerten") bewerten();
    else if (aktion === "neu-bewerten") bewerten(true);
    else if (aktion === "zapier") zapier();
    else if (aktion === "frage") fragen(el.dataset.text);
    else if (aktion === "kalender") kalender(el.dataset.datum);
    else if (aktion === "karte-vollbild") karteVollbild();
    else if (aktion === "kopieren") {
      const t = $("#meldung")?.value || "";
      navigator.clipboard?.writeText(t).then(() => toast("Text kopiert."), () => toast("Kopieren nicht möglich. Bitte markieren und kopieren."));
    }
  });
  $("#zurueck").addEventListener("click", () => zeige("start"));
  $("#datei").addEventListener("change", (ev) => { hochladen(ev.target.files[0]); ev.target.value = ""; });
  $("#kamera").addEventListener("change", (ev) => { fotosDazu(ev.target.files, true); ev.target.value = ""; });
  $("#galerie").addEventListener("change", (ev) => { fotosDazu(ev.target.files, false); ev.target.value = ""; });
  main.addEventListener("input", (ev) => { if (ev.target.id?.startsWith("a-")) felderMerken(); });

  // ---------- Start ----------
  wiederherstellen();
  eigeneLaden();
  render();
  if (zustand.weiterVerfolgen) {
    zustand.aufnahme.auftrag = { id: zustand.weiterVerfolgen, status: "laeuft", phase: "Auswertung läuft", erledigt: 0, gesamt: 1 };
    zeige("aufnahme");
    verfolgen(zustand.weiterVerfolgen);
  }
  function statusLaden() {
    api("/api/status").then((s) => {
      zustand.status = s; renderStatus();
      if (zustand.ansicht === "ergebnis" || (zustand.ansicht === "aufnahme" && !zustand.aufnahme.gefuehrt)) render();
    }).catch(() => {});
  }
  statusLaden();
  api("/api/beispiele").then((d) => { zustand.beispiele = d.beispiele; if (zustand.ansicht === "start") render(); })
    .catch((e) => toast(e.message));
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
