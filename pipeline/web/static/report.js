// Pintado de resultados, compartido por la web local y el informe HTML del correo.
// Todo el contenido externo (citas, extractos, títulos, URLs) se inserta como texto.

const CATEGORIES = {
  verdadero: "Verdadero",
  verdadero_sin_fuente_oficial: "Verdadero sin fuente oficial",
  confusion: "Confusión",
  no_verificable: "No verificable",
  falso_sin_fuente_oficial: "Falso sin fuente oficial",
  falso_con_fuente_oficial: "Falso con fuente oficial",
};
const BASIS = {
  factcheck: "desmentido publicado", oficial: "fuente oficial", web: "solo web", sin_fuente: "sin fuente",
};

const $ = (id) => document.getElementById(id);
let filter = null;
let speakerFilter = null;
let lastReport = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function safeLink(url, text) {
  // Only http(s) links; text is always set as text, never as HTML.
  if (!/^https?:\/\//i.test(url)) return el("span", {}, text);
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
}

function formatTime(seconds) {
  const s = Math.floor(seconds), m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function show(id, visible) { $(id).classList.toggle("hidden", !visible); }

function resetFilters() { filter = null; speakerFilter = null; }

function renderClaim(claim, speakers) {
  const color = `var(--${claim.category})`;
  const header = el("header", {},
    el("span", { class: "badge", style: `background: var(--tint-${claim.category}); color: ${color}` },
      CATEGORIES[claim.category]),
  );
  if (claim.verdict === "falso" && claim.matched_factcheck) {
    header.append(el("span", { class: "badge", style: `background: ${color}; color: var(--card)` }, "Bulo"));
  }
  if (claim.verdict !== "no_verificable") {
    header.append(el("span", { class: "pill" }, `confianza ${Math.round(claim.confidence * 100)} %`));
  }
  if (claim.basis) header.append(el("span", { class: "pill" }, BASIS[claim.basis]));
  header.append(el("span", { class: "pill" }, speakers[claim.speaker] || claim.speaker));
  if (claim.start !== null && claim.start !== undefined) {
    header.append(el("span", { class: "pill" }, formatTime(claim.start)));
  }

  const card = el("article", { class: "card claim", style: `border-left-color: ${color}` },
    header,
    el("blockquote", {}, `«${claim.text_original}»`),
    el("p", { class: "normalized" }, claim.text_normalized),
    el("p", { class: "explanation" }, claim.explanation),
  );
  if (claim.context_status === "resuelto") {
    const periods = claim.time_scope.map((r) => `${r.start} – ${r.end}`).join("; ");
    card.append(el("p", { class: "meta" }, `Contexto: ${claim.resolved_actor} · ${periods}`,
      claim.resolution_notes ? ` — ${claim.resolution_notes}` : ""));
  } else if (claim.context_status === "no_resuelto") {
    card.append(el("p", { class: "note" }, `Contexto no resuelto: ${claim.resolution_notes || ""}`));
  }
  if (claim.sources.length) {
    card.append(el("ul", { class: "sources" }, ...claim.sources.map((s) => el("li", {},
      `${s.organization} — `, safeLink(s.url, s.title), s.date ? ` (${s.date})` : ""))));
  }
  if (claim.analysis_error) card.append(el("p", { class: "note" }, `El análisis falló: ${claim.analysis_error}`));
  if (claim.evidence_errors.length) {
    card.append(el("p", { class: "note" }, `Búsquedas con errores: ${claim.evidence_errors.join("; ")}`));
  }
  return card;
}

function renderSpeakers(report) {
  const speakers = Object.entries(report.by_speaker);
  show("oradores-wrap", speakers.length > 1);
  if (speakers.length < 2) return;
  const head = el("tr", {}, el("th", {}, "Orador"),
    ...Object.values(CATEGORIES).map((n) => el("th", {}, n)), el("th", {}, "Total"));
  const rows = speakers.map(([speaker, counts]) => {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const row = el("tr", { class: speakerFilter === speaker ? "active" : "" }, el("td", {}, speaker),
      ...Object.keys(CATEGORIES).map((k) =>
        el("td", { style: counts[k] ? `color: var(--${k}); font-weight: 700` : "" }, counts[k])),
      el("td", {}, total));
    row.addEventListener("click", () => {
      speakerFilter = speakerFilter === speaker ? null : speaker;
      renderReport(lastReport);
    });
    return row;
  });
  $("oradores").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

function renderReport(report) {
  lastReport = report;
  const total = report.claims.length;
  $("titulo-resumen").textContent = `${total} afirmaci${total === 1 ? "ón verificable" : "ones verificables"}`;
  $("tiles").replaceChildren(...Object.entries(CATEGORIES).map(([key, name]) => {
    const tile = el("button", { type: "button", class: "tile" + (filter === key ? " active" : ""),
      style: `color: var(--${key}); background: var(--tint-${key})` },
      el("span", { class: "n" }, report.by_category[key]), name);
    tile.addEventListener("click", () => { filter = filter === key ? null : key; renderReport(lastReport); });
    return tile;
  }));
  const basis = Object.entries(report.by_basis).filter(([, n]) => n).map(([k, n]) => `${BASIS[k]}: ${n}`);
  $("base").textContent = basis.length ? `En qué se apoyan los veredictos — ${basis.join(", ")}` : "";
  const plan = report.cost.entries.some((e) => e.provider === "claude-code");
  $("coste").textContent = `Coste estimado: ${report.cost.total_usd.toFixed(4)} $` +
    (plan ? " (equivalente orientativo, cubierto por el plan de Claude)" : "");
  $("avisos").replaceChildren(...report.warnings.map((w) => el("li", {}, w)));

  renderSpeakers(report);
  const name = (c) => report.speakers[c.speaker] || c.speaker;
  const claims = report.claims.filter((c) =>
    (!filter || c.category === filter) && (!speakerFilter || name(c) === speakerFilter));
  const active = [filter && CATEGORIES[filter], speakerFilter].filter(Boolean);
  $("filtros").textContent = active.length
    ? `Mostrando: ${active.join(" · ")} (${claims.length}). Pulsa de nuevo para quitar el filtro.` : "";
  $("afirmaciones").replaceChildren(...claims.map((c) => renderClaim(c, report.speakers)));
  if (!total) {
    $("afirmaciones").append(el("p", { class: "meta" }, "No se han detectado afirmaciones verificables en el texto."));
  }
  show("resultado", true);
}
