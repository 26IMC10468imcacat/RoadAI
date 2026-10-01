/* RoadAI Oberfläche. Kein Framework, damit es am Handy schnell lädt. */
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
    status: null, beispiele: [], strecke: null, entscheidung: null, verlauf: [],
    ansicht: "start", beschaeftigt: false, zapier: {},
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
        strecke: zustand.strecke, entscheidung: zustand.entscheidung, verlauf: zustand.verlauf, ansicht: zustand.ansicht,
      }));
    } catch { /* privates Fenster: egal */ }
  }
  function wiederherstellen() {
    try {
      const d = JSON.parse(sessionStorage.getItem("roadai") || "null");
      if (d && d.strecke && Array.isArray(d.strecke.abschnitte)) Object.assign(zustand, d);
    } catch { /* ignorieren */ }
  }

  // ---------- Navigation ----------
  function zeige(ansicht) {
    if (ansicht !== "start" && !zustand.strecke) ansicht = "start";
    zustand.ansicht = ansicht;
    speichern();
    render();
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }

  function render() {
    const a = zustand.ansicht;
    $("#tabs").hidden = a === "start";
    $("#zurueck").hidden = a === "start";
    document.querySelectorAll("#tabs button").forEach((b) =>
      b.dataset.ansicht === a ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
    main.classList.toggle("mit-eingabe", a === "chat");
    document.querySelector(".eingabe")?.remove();
    main.innerHTML = { start: ansichtStart, strecke: ansichtStrecke, ergebnis: ansichtErgebnis, chat: ansichtChat }[a]();
    if (a === "chat") chatEingabe();
  }

  function renderStatus() {
    const s = zustand.status;
    if (!s) return;
    $("#status").innerHTML =
      `<span class="pille ${s.ki ? "an" : "aus"}">${s.ki ? "KI aktiv" : "Offline-Modus"}</span>` +
      `<span class="pille ${s.zapier ? "an" : "aus"}">${s.zapier ? "Sheets verbunden" : "Sheets aus"}</span>`;
  }

  // ---------- Ansicht: Start ----------
  function ansichtStart() {
    const karten = zustand.beispiele.map((b) => `
      <button class="wahl" type="button" data-aktion="laden" data-id="${esc(b.id)}">
        <span class="symbol" aria-hidden="true">${b.simuliert ? "◇" : "◆"}</span>
        <span class="txt"><b>${esc(b.name)}</b><span class="klein">${esc(b.beschreibung)}</span></span>
        <span class="pfeil" aria-hidden="true">›</span>
      </button>`).join("");
    return `
      <section>
        <div class="eyebrow">Straßenzustand · Prototyp</div>
        <h1>Welche Strecke soll RoadAI bewerten?</h1>
        <p class="lead" style="margin-top:8px">RoadAI liest die Auswertung der Straßenfotos, entscheidet, was zu tun ist, und leitet Sie zum passenden nächsten Schritt.</p>
      </section>
      <div class="liste">${karten || `<p class="leer">Lade Strecken …</p>`}
        <button class="wahl" type="button" data-aktion="hochladen">
          <span class="symbol" aria-hidden="true">⇪</span>
          <span class="txt"><b>Eigene Auswertung laden</b><span class="klein">JSON-Datei aus dem RoadAI-Python-Programm</span></span>
          <span class="pfeil" aria-hidden="true">›</span>
        </button>
      </div>
      ${zustand.strecke ? `<button class="knopf zweit" type="button" data-aktion="weiter">Zurück zu „${esc(zustand.strecke.name)}“</button>` : ""}`;
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
        ${link ? `<div class="links"><a href="${link}" target="_blank" rel="noopener">In Google Maps öffnen ↗</a></div>` : ""}
      </details>`;
    }).join("");
    const grenzen = s.grenzen.length ? `
      <details class="karte"><summary><b>Grenzen der Auswertung</b></summary>
        <ul style="margin:8px 0 0;padding-left:1.1em">${s.grenzen.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>
      </details>` : "";
    return `
      <section style="display:flex;flex-direction:column;gap:6px">
        ${s.simuliert ? `<span class="marke-sim">Simulierte Demo-Daten</span>` : `<div class="eyebrow">Echte Auswertung</div>`}
        <h1>${esc(s.name)}</h1>
        <p class="klein">${esc([s.ort, datum(s.aufnahme), `Fahrbahn ${zahl(s.fahrbahnbreite_m, 2)} m`].filter(Boolean).join(" · "))}</p>
      </section>
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
      <button class="knopf akzent" type="button" data-aktion="bewerten" ${zustand.beschaeftigt ? "disabled" : ""}>
        ${zustand.beschaeftigt ? `<span class="spinner"></span>RoadAI bewertet …` : zustand.entscheidung ? "Bewertung ansehen" : "KI-Bewertung starten"}
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
          ${zustand.beschaeftigt ? `<span class="spinner"></span>RoadAI bewertet …` : "KI-Bewertung starten"}</button>`;
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

Hinweis: Automatische Auswertung mit RoadAI (Prototyp), Werte etwa ±10 %.

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
      <div class="chat" id="chat" aria-live="polite">${nachrichten}${zustand.beschaeftigt === "chat" ? `<div class="msg assistant tippt">RoadAI denkt nach …</div>` : ""}</div>
      <div class="chips">${FRAGEN.map((f) => `<button class="chip" type="button" data-aktion="frage" data-text="${esc(f)}">${esc(f)}</button>`).join("")}</div>`;
  }

  function chatEingabe() {
    const div = document.createElement("div");
    div.className = "eingabe";
    div.innerHTML = `<form id="chatform" autocomplete="off">
      <label for="frage" class="klein" style="position:absolute;left:-9999px">Ihre Frage</label>
      <input id="frage" name="frage" maxlength="600" placeholder="Frage an RoadAI …" enterkeyhint="send">
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
      neueStrecke(d.strecke);
      toast("Auswertung geladen.");
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
    const ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RoadAI//DE", "BEGIN:VEVENT",
      `UID:${Date.now()}@roadai`, `DTSTAMP:${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}Z`,
      `DTSTART;VALUE=DATE:${d}`, `SUMMARY:RoadAI Wiederholungsbefahrung ${s.name.replace(/[,;\\]/g, " ")}`,
      "DESCRIPTION:Strecke erneut fotografieren und mit RoadAI auswerten.", "END:VEVENT", "END:VCALENDAR"].join("\r\n");
    const url = URL.createObjectURL(new Blob([ics], { type: "text/calendar" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "roadai-wiederholung.ics" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

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
    else if (aktion === "hochladen") $("#datei").click();
    else if (aktion === "weiter") zeige("strecke");
    else if (aktion === "start") zeige("start");
    else if (aktion === "bewerten") bewerten();
    else if (aktion === "neu-bewerten") bewerten(true);
    else if (aktion === "zapier") zapier();
    else if (aktion === "frage") fragen(el.dataset.text);
    else if (aktion === "kalender") kalender(el.dataset.datum);
    else if (aktion === "kopieren") {
      const t = $("#meldung")?.value || "";
      navigator.clipboard?.writeText(t).then(() => toast("Text kopiert."), () => toast("Kopieren nicht möglich. Bitte markieren und kopieren."));
    }
  });
  $("#zurueck").addEventListener("click", () => zeige("start"));
  $("#datei").addEventListener("change", (ev) => { hochladen(ev.target.files[0]); ev.target.value = ""; });

  // ---------- Start ----------
  wiederherstellen();
  render();
  api("/api/status").then((s) => { zustand.status = s; renderStatus(); if (zustand.ansicht === "ergebnis") render(); }).catch(() => {});
  api("/api/beispiele").then((d) => { zustand.beispiele = d.beispiele; if (zustand.ansicht === "start") render(); })
    .catch((e) => toast(e.message));
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
