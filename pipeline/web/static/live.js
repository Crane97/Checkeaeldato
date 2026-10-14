// Panel en directo: instantánea por JSON al abrir y eventos por SSE después.
// Usa el, renderClaim, renderPromise, CATEGORIES, PROMISES y formatTime de report.js.

const STAGES = {
  detectada: "Detectada; buscando evidencia…",
  evidencia: "Evidencia encontrada; decidiendo veredicto…",
};
const STATUS_TEXT = { en_curso: "En directo", terminado: "Terminado", parado: "Parado", error: "Error" };
const BLOCK_TEXT = {
  transcribiendo: "transcribiendo", analizando: "analizando", hecho: "analizado", fallido: "fallido",
};

const live = {
  id: null,
  estado: "en_curso",
  speakers: {},
  labels: [],
  claims: new Map(),      // id -> Claim (verdict decided)
  promises: new Map(),
  pending: new Map(),     // id -> { claim, stage, fuentes }
  blocks: new Map(),      // index -> BlockRecord
  blockTimes: new Map(),  // index -> { start: Date, end: Date }
  transcript: [],
  cost: null,
  warnings: [],
  source: null,
  lastSeq: 0,
  filter: null,
  speakerFilter: null,
};

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// --- helpers --------------------------------------------------------------

function speakerName(label) { return live.speakers[label] || label; }

function bump(node) {
  if (reduceMotion) return;
  node.classList.remove("bump");
  void node.offsetWidth;  // restart the animation
  node.classList.add("bump");
}

function countTo(node, value) {
  const from = Number(node.dataset.value || 0);
  node.dataset.value = value;
  if (reduceMotion || from === value) { node.textContent = value; return; }
  const start = performance.now(), ms = 500;
  const tick = (t) => {
    const p = Math.min(1, (t - start) / ms), eased = 1 - Math.pow(1 - p, 3);
    node.textContent = Math.round(from + (value - from) * eased);
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  bump(node.closest(".tile") || node);
}

function byCategory() {
  const counts = Object.fromEntries(Object.keys(CATEGORIES).map((k) => [k, 0]));
  for (const c of live.claims.values()) counts[c.category] += 1;
  return counts;
}

function bySpeaker() {
  const result = new Map();
  const seen = (label) => {
    const name = speakerName(label);
    if (!result.has(name)) result.set(name, { counts: Object.fromEntries(Object.keys(CATEGORIES).map((k) => [k, 0])), labels: new Set() });
    result.get(name).labels.add(label);
    return result.get(name);
  };
  for (const label of live.labels) seen(label);
  for (const c of live.claims.values()) seen(c.speaker).counts[c.category] += 1;
  for (const p of live.pending.values()) seen(p.claim.speaker);
  return result;
}

function sortedClaims() {
  return [...live.claims.values()].sort((a, b) => (b.start ?? 0) - (a.start ?? 0));
}

// --- rendering ------------------------------------------------------------

function renderStatus() {
  const node = $("estado");
  node.className = `status ${live.estado}`;
  $("estado-texto").textContent = STATUS_TEXT[live.estado] || live.estado;
  $("parar").classList.toggle("hidden", live.estado !== "en_curso");
  $("fuente-texto").textContent = live.source || "";
  const seconds = [...live.blocks.values()].filter((b) => b.status !== "transcribiendo")
    .reduce((n, b) => n + b.duration, 0);
  $("reloj").textContent = formatTime(seconds);
}

function renderBlocks() {
  const blocks = [...live.blocks.values()].sort((a, b) => a.index - b.index);
  $("bloques").replaceChildren(...blocks.map((b) => el("span", {
    class: `block ${b.status}` + (b.claims ? " has-claims" : ""),
    title: `Bloque ${b.index + 1} · ${formatTime(b.start)}–${formatTime(b.start + b.duration)} · ` +
      `${BLOCK_TEXT[b.status]}` + (b.claims ? ` · ${b.claims} afirmación${b.claims === 1 ? "" : "es"}` : "") +
      (b.error ? ` · ${b.error}` : ""),
  })));
  const running = blocks.filter((b) => b.status === "transcribiendo" || b.status === "analizando");
  const done = blocks.filter((b) => b.status === "hecho");
  let msg = "";
  if (live.estado === "en_curso") {
    msg = running.length
      ? `Bloque ${running[0].index + 1}: ${BLOCK_TEXT[running[0].status]}…`
      : "Esperando el siguiente bloque de audio…";
  } else if (live.estado === "error") {
    msg = "";
  } else {
    msg = `${done.length} bloque${done.length === 1 ? "" : "s"} analizado${done.length === 1 ? "" : "s"}.`;
  }
  $("mensaje").textContent = msg;
}

function renderTiles() {
  const counts = byCategory();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  countTo($("total"), total);
  const pending = live.pending.size;
  $("pendientes-n").textContent = pending ? `· ${pending} en análisis` : "";
  const tiles = $("tiles");
  if (!tiles.children.length) {
    tiles.replaceChildren(...Object.entries(CATEGORIES).map(([key, name]) => {
      const tile = el("button", { type: "button", class: "tile", "data-key": key,
        style: `color: var(--${key}); background: var(--tint-${key})` },
        el("span", { class: "n", "data-value": "0" }, "0"), name);
      tile.addEventListener("click", () => {
        live.filter = live.filter === key ? null : key;
        renderTiles(); renderFeed();
      });
      return tile;
    }));
  }
  for (const tile of tiles.children) {
    const key = tile.dataset.key;
    tile.classList.toggle("active", live.filter === key);
    const n = tile.querySelector(".n");
    if (Number(n.dataset.value) !== counts[key]) countTo(n, counts[key]);
  }
}

function renderChips() {
  const speakers = [...bySpeaker().entries()].filter(([, v]) => Object.values(v.counts).some(Boolean));
  const chips = speakers.map(([name, v]) => {
    const total = Object.values(v.counts).reduce((a, b) => a + b, 0);
    const chip = el("button", { type: "button", class: "chip" + (live.speakerFilter === name ? " active" : "") },
      name, el("span", { class: "n" }, total));
    chip.addEventListener("click", () => {
      live.speakerFilter = live.speakerFilter === name ? null : name;
      renderChips(); renderFeed();
    });
    return chip;
  });
  $("chips").replaceChildren(...chips);
}

function renderFeed(freshId = null) {
  const claims = sortedClaims().filter((c) =>
    (!live.filter || c.category === live.filter) &&
    (!live.speakerFilter || speakerName(c.speaker) === live.speakerFilter));
  const feed = $("afirmaciones");
  if (!claims.length) {
    const text = live.claims.size ? "Ninguna afirmación con ese filtro."
      : "Todavía no hay veredictos. Los primeros llegan cuando se analiza el primer bloque (uno o dos minutos).";
    feed.replaceChildren(el("p", { class: "empty" }, text));
    return;
  }
  feed.replaceChildren(...claims.map((c) => {
    const card = renderClaim(c, live.speakers);
    if (c.id === freshId) card.classList.add("fresh");
    return card;
  }));
  renderChips();
}

function renderPromisesLive() {
  const promises = [...live.promises.values()].sort((a, b) => (b.start ?? 0) - (a.start ?? 0));
  show("promesas-wrap", promises.length > 0);
  if (!promises.length) return;
  $("titulo-promesas").textContent = `${promises.length} promesa${promises.length === 1 ? "" : "s"}`;
  const counts = Object.fromEntries(Object.keys(PROMISES).map((k) => [k, 0]));
  for (const p of promises) counts[p.assessment] += 1;
  $("tiles-promesas").replaceChildren(...Object.entries(PROMISES).map(([key, name]) =>
    el("div", { class: "tile", style: `color: var(--${key}); background: var(--tint-${key})` },
      el("span", { class: "n" }, counts[key]), name)));
  $("promesas").replaceChildren(...promises.map((p) => renderPromise(p, live.speakers)));
}

function renderPending() {
  const list = $("pendientes");
  const items = [...live.pending.values()].sort((a, b) => (a.claim.start ?? 0) - (b.claim.start ?? 0));
  if (!items.length) { list.replaceChildren(el("li", { class: "empty" }, "Nada en cola.")); return; }
  list.replaceChildren(...items.map(({ claim, stage, fuentes }) => el("li", {},
    el("div", {}, el("strong", {}, speakerName(claim.speaker)), " · ", el("q", {}, claim.text_original)),
    el("div", { class: "stage" }, el("span", { class: "icon spin" }, "◐"),
      stage === "evidencia" ? `${fuentes} fuente${fuentes === 1 ? "" : "s"}; decidiendo veredicto…` : STAGES[stage]))));
}

function renderSpeakers() {
  const list = $("oradores-lista");
  const entries = [...bySpeaker().entries()];
  if (!entries.length) { list.replaceChildren(el("li", { class: "empty" }, "Aún no hay oradores.")); return; }
  list.replaceChildren(...entries.map(([name, { counts, labels }]) => {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const labelList = [...labels];
    const named = labelList.some((l) => live.speakers[l]);
    const button = el("button", { type: "button", title: `Etiquetas: ${labelList.join(", ")}` },
      named ? name : `${name} — ¿quién es?`);
    button.addEventListener("click", () => openSpeakerDialog(labelList[labelList.length - 1]));
    const bar = el("div", { class: "bar" }, ...Object.entries(counts).filter(([, n]) => n).map(([k, n]) =>
      el("span", { style: `width: ${(100 * n / total).toFixed(1)}%; background: var(--${k})`,
        title: `${CATEGORIES[k]}: ${n}` })));
    return el("li", {}, el("div", { class: "who" }, button,
      el("span", { class: "count" }, total ? `${total} afirm.` : "sin afirmaciones")), bar);
  }));
}

function renderTicker() {
  const ticker = $("ticker");
  const last = live.transcript.slice(-14);
  if (!last.length) return;
  ticker.replaceChildren(...last.map((s) => el("p", {},
    el("span", { class: "t" }, formatTime(s.start ?? 0)),
    el("span", { class: "sp" }, speakerName(s.speaker) + ":"), s.text)));
  ticker.scrollTop = ticker.scrollHeight;
}

function renderStats() {
  const stats = [];
  const blocks = [...live.blocks.values()];
  const done = blocks.filter((b) => b.status === "hecho");
  stats.push(["Bloques", `${done.length}/${blocks.length}`]);
  const times = done.map((b) => live.blockTimes.get(b.index)).filter((t) => t && t.end);
  if (times.length) {
    const secs = (t) => Math.round((t.end - t.start) / 1000);
    stats.push(["Último bloque", `${secs(times[times.length - 1])} s`]);
    stats.push(["Media por bloque", `${Math.round(times.reduce((n, t) => n + secs(t), 0) / times.length)} s`]);
  }
  if (live.cost) {
    const plan = live.cost.entries.some((e) => e.provider === "claude-code");
    stats.push(["Coste estimado", `${live.cost.total_usd.toFixed(3)} $` + (plan ? " (plan)" : "")]);
    const searches = live.cost.entries.filter((e) => e.provider === "tavily");
    if (searches.length) {
      const sum = (k) => searches.reduce((n, e) => n + e[k], 0);
      stats.push(["Búsquedas", `${sum("searches")} reales · ${sum("cached_searches")} caché`]);
    }
  }
  $("stats").replaceChildren(...stats.map(([k, v]) => el("div", { class: "stat" },
    el("span", { class: "v" }, v), el("span", { class: "k" }, k))));
  $("avisos").replaceChildren(...live.warnings.map((w) => el("li", {}, w)));
}

function renderAll(freshId = null) {
  renderStatus(); renderBlocks(); renderTiles(); renderFeed(freshId); renderPromisesLive();
  renderPending(); renderSpeakers(); renderTicker(); renderStats();
}

// --- state updates ------------------------------------------------------------

function applySnapshot(snapshot) {
  live.id = snapshot.session_id;
  live.estado = snapshot.estado;
  live.source = snapshot.fuente;
  live.speakers = snapshot.oradores;
  live.labels = snapshot.etiquetas;
  live.claims = new Map(snapshot.informe.claims.map((c) => [c.id, c]));
  live.promises = new Map(snapshot.informe.promises.map((p) => [p.id, p]));
  live.pending = new Map(snapshot.pendientes.map((c) => [c.id, { claim: c, stage: "detectada", fuentes: 0 }]));
  live.blocks = new Map(snapshot.bloques.map((b) => [b.index, b]));
  live.transcript = snapshot.transcripcion;
  live.cost = snapshot.informe.cost;
  live.warnings = snapshot.informe.warnings;
  live.lastSeq = snapshot.ultimo_evento;
  $("descargar").onclick = () => { window.location.href = `/api/directo/${live.id}/informe.html`; };
  if (snapshot.error) fail(snapshot.error);
  renderAll();
}

async function refreshSnapshot() {
  const response = await fetch(`/api/directo/${live.id}`);
  if (!response.ok) return;
  const snapshot = await response.json();
  live.cost = snapshot.informe.cost;
  live.warnings = snapshot.informe.warnings;
  live.estado = snapshot.estado;
  renderStats(); renderStatus();
}

function handleEvent(type, data, at) {
  switch (type) {
    case "bloque": {
      live.blocks.set(data.index, data);
      const t = live.blockTimes.get(data.index) || { start: at, end: null };
      if (data.status === "hecho" || data.status === "fallido") t.end = at;
      live.blockTimes.set(data.index, t);
      renderStatus(); renderBlocks(); renderStats();
      if (data.status === "hecho" || data.status === "fallido") refreshSnapshot().catch(() => {});
      break;
    }
    case "transcripcion":
      live.transcript.push(...data.segmentos);
      for (const s of data.segmentos) if (!live.labels.includes(s.speaker)) live.labels.push(s.speaker);
      renderTicker(); renderSpeakers();
      break;
    case "oradores":
      live.speakers = data.oradores;
      renderAll();
      break;
    case "afirmaciones":
      for (const c of data.afirmaciones) live.pending.set(c.id, { claim: c, stage: "detectada", fuentes: 0 });
      renderPending(); renderTiles(); renderSpeakers();
      break;
    case "evidencia": {
      const item = live.pending.get(data.id);
      if (item) { item.stage = "evidencia"; item.fuentes = data.fuentes; renderPending(); }
      break;
    }
    case "veredicto":
      live.pending.delete(data.afirmacion.id);
      live.claims.set(data.afirmacion.id, data.afirmacion);
      renderTiles(); renderFeed(data.afirmacion.id); renderPending(); renderSpeakers();
      break;
    case "promesa":
      live.pending.delete(data.promesa.id);
      live.promises.set(data.promesa.id, data.promesa);
      renderPromisesLive(); renderPending(); renderTiles();
      break;
    case "estado":
      live.estado = data.estado;
      if (data.error) fail(data.error);
      renderStatus(); renderBlocks();
      refreshSnapshot().catch(() => {});
      break;
    default:
      break;
  }
}

// --- connection -----------------------------------------------------------------

let eventSource = null;
let pollTimer = null;

function connect() {
  if (eventSource) eventSource.close();
  if (live.estado !== "en_curso") return;
  eventSource = new EventSource(`/api/directo/${live.id}/eventos?desde=${live.lastSeq}`);
  for (const type of ["bloque", "transcripcion", "oradores", "afirmaciones", "evidencia", "veredicto", "promesa", "estado"]) {
    eventSource.addEventListener(type, (event) => {
      live.lastSeq = Number(event.lastEventId) || live.lastSeq;
      handleEvent(type, JSON.parse(event.data), new Date());
      if (type === "estado" && live.estado !== "en_curso") eventSource.close();
    });
  }
  eventSource.onerror = () => {
    // The server restarted or the connection dropped: fall back to polling the snapshot.
    eventSource.close();
    if (live.estado !== "en_curso") return;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/directo/${live.id}`);
        if (response.ok) { applySnapshot(await response.json()); connect(); }
        else fail("Se perdió la conexión con el servidor");
      } catch (e) { fail("Se perdió la conexión con el servidor"); }
    }, 3000);
  };
}

async function openSession(id) {
  const response = await fetch(`/api/directo/${id}`);
  if (!response.ok) throw new Error("Sesión no encontrada");
  applySnapshot(await response.json());
  show("panel", true);
  show("configurar", false);
  history.replaceState(null, "", `/directo?sesion=${id}`);
  try { localStorage.setItem("ultimoDirecto", id); } catch (e) { /* storage unavailable */ }
  connect();
}

function fail(message) {
  $("error").replaceChildren(el("strong", {}, "Problema en la sesión. "), message);
  show("error", true);
  $("empezar").disabled = false;
}

// --- speaker dialog ---------------------------------------------------------------

let dialogLabel = null;

function openSpeakerDialog(label) {
  dialogLabel = label;
  $("dialogo-etiqueta").textContent = label;
  $("orador-nombre").value = ""; $("orador-partido").value = ""; $("orador-rol").value = "";
  $("dialogo-orador").showModal();
  $("orador-nombre").focus();
}

$("orador-cancelar").addEventListener("click", () => $("dialogo-orador").close());
$("form-orador").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = { name: $("orador-nombre").value.trim() || null, party: $("orador-partido").value.trim() || null,
    role: $("orador-rol").value.trim() || null };
  if (!body.name && !body.party) { $("dialogo-orador").close(); return; }
  try {
    const response = await fetch(`/api/directo/${live.id}/oradores/${encodeURIComponent(dialogLabel)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error("No se pudo guardar el orador");
    live.speakers = await response.json();
    renderAll();
  } catch (error) { fail(error.message); }
  $("dialogo-orador").close();
});

// --- form and previous sessions ---------------------------------------------------

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  show("error", false);
  $("empezar").disabled = true;
  try {
    const response = await fetch("/api/directo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fuente: $("fuente").value, fecha: $("fecha").value,
        bloque_segundos: Number($("bloque").value), oradores: $("oradores").value,
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Datos no válidos");
    await openSession(body.session_id);
  } catch (error) { fail(error.message); }
});

$("parar").addEventListener("click", async () => {
  $("parar").disabled = true;
  try { await fetch(`/api/directo/${live.id}/parar`, { method: "POST" }); } catch (e) { /* shown by SSE */ }
});

async function loadPrevious() {
  try {
    const sessions = await (await fetch("/api/directo")).json();
    show("anteriores-wrap", sessions.length > 0);
    $("anteriores").replaceChildren(...sessions.slice(0, 8).map((s) => {
      const link = el("a", { href: `/directo?sesion=${s.session_id}` }, s.fuente);
      link.addEventListener("click", (event) => { event.preventDefault(); openSession(s.session_id).catch((e) => fail(e.message)); });
      return el("li", {}, link,
        el("span", { class: "when" }, `${new Date(s.inicio).toLocaleString("es-ES")} · ${s.bloques} bloques · ${s.afirmaciones} afirm.`),
        el("span", { class: `status ${s.estado}` }, el("span", { class: "dot" }), STATUS_TEXT[s.estado] || s.estado));
    }));
  } catch (e) { /* no previous sessions */ }
}

$("fecha").value = new Date().toISOString().slice(0, 10);
loadPrevious();
const wanted = new URLSearchParams(location.search).get("sesion");
if (wanted) openSession(wanted).catch((e) => fail(e.message));
