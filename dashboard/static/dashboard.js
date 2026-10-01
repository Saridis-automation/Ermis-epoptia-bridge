"use strict";
const el = id => document.getElementById(id);
const percent = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? `${Math.round(value)}%` : "—";
// Station load may exceed 100% (more work due than the station can do in time).
const loadPercent = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${Math.round(value)}%` : "—";
const dayMonth = iso => typeof iso === "string" && /^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}` : "—";
const decimal = value => typeof value === "number" && Number.isFinite(value) ? String(Math.round(value * 10) / 10).replace(".", ",") : "—";
function loadDetail(model) {
  const tight = model && model.tightest;
  return tight ? `ως ${dayMonth(tight.by)}: χρειάζονται ${decimal(tight.needed)} · χωράνε ${decimal(tight.fits)}` : "";
}
// UI load is the inverse of the native active-order mean; unknown stays unknown.
const productionLoad = progress => typeof progress === "number" && Number.isFinite(progress) ? Math.max(0, Math.min(100, 100 - progress)) : null;
// Presentation only: preserve the verified calendar date without timezone conversion.
function deadlineLabel(value) {
  if (typeof value !== "string") return "Μη επαληθευμένη";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[3]}/${match[2]}/${match[1]}` : "Μη επαληθευμένη";
}
let observedAt = null;
let state = "loading";
let lastModel = null;
let freshnessMs = 120000;
let halt = null;
function status() {
  const stale = ["live", "online"].includes(state) && Date.now() - Date.parse(observedAt) > freshnessMs;
  const current = stale ? "stale" : state;
  el("data-status").dataset.state = current;
  el("data-status").textContent = {
    loading: "● Φόρτωση δεδομένων",
    live: "● Ζωντανά δεδομένα", online: "● Ζωντανά δεδομένα", stale: "● Παλαιά δεδομένα",
    partial: "● Μερικώς διαθέσιμα δεδομένα",
    offline: "● Χωρίς σύνδεση · τα δεδομένα δεν ενημερώνονται",
    halted: `● Σταματημένο · το Epoptia απάντησε HTTP ${halt?.http_status ?? "—"} · χρειάζεται χειροκίνητη επαναφορά`,
    disconnected: "● Χωρίς σύνδεση · τα δεδομένα δεν ενημερώνονται"
  }[current];
}
function tick() {
  const now = new Date();
  el("date").textContent = now.toLocaleDateString("el-GR", {timeZone:"Europe/Athens", weekday:"long", day:"numeric", month:"long", year:"numeric"});
  el("clock").textContent = now.toLocaleTimeString("el-GR", {timeZone:"Europe/Athens", hour12:false, hour:"2-digit", minute:"2-digit"});
  status();
  if (lastModel) {
    sourceNotes(lastModel);
    for (const star of document.querySelectorAll('.priority')) updateStar(star);
  }
}
function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function sourceNotes(data) {
  const note = (field, source) => {
    const meta = data.sources?.[source] ?? {};
    const at = data.field_observed_at?.[field];
    const age = at ? Math.max(0, Math.floor((Date.now() - Date.parse(at)) / 60000)) : null;
    const stale = meta.stale || data.field_status?.[field] === "stale" || (at && Date.now() - Date.parse(at) > freshnessMs);
    const failed = Boolean(meta.failure_reason);
    const label = failed ? (meta.last_success ? "Σφάλμα ενημέρωσης · προηγούμενη επιτυχής ανάγνωση" : "Σφάλμα ενημέρωσης · μη διαθέσιμα δεδομένα") :
      stale ? "Παλαιά δεδομένα" : ({available: "Ενημερωμένα δεδομένα", refreshing: "Ανανέωση σε εξέλιξη",
        cached: "Προηγούμενη επιτυχής ανάγνωση", partial: "Μερική κάλυψη"}[data.field_status?.[field]] ?? "Μη διαθέσιμη πηγή");
    return `${label}${age != null ? ` · πριν ${age} λεπτά` : ""}`;
  };
  el("stations-status").textContent = note("workstations", "workstation_wip");
  el("orders-status").textContent = `${note("active_production", data.sources?.whole_orders ? "whole_orders" : "production_overview")} · Χωρίς επαληθευμένη προθεσμία: ${data.canonical_orders?.undated_unfinished_orders ?? "—"}`;
  el("overdue-status").textContent = data.today?.overdue_work == null ? "Δεν καλύπτονται όλες οι προθεσμίες" : "";
}
function starState(changedAt, now = Date.now()) {
  const age = now - Date.parse(changedAt);
  return !Number.isFinite(age) || age < 0 ? 'neutral' : age < 3600000 ? 'gold' : age <= 7200000 ? 'neutral' : 'red';
}
function updateStar(star) {
  const color = starState(star.dataset.changedAt);
  star.dataset.state = color;
  star.title = {gold:'Παρατηρήθηκε αλλαγή πριν από λιγότερο από 1 ώρα', neutral:'Αλλαγή πριν από 1–2 ώρες ή χωρίς ιστορικό αλλαγής', red:'Πάνω από 2 ώρες από την τελευταία παρατηρημένη αλλαγή'}[color];
  star.setAttribute('aria-label', star.title);
}
function stationIcon(name) {
  const paths = {
    'LASER':'M8 8h30v8H8M26 16h16v12l-8 8-8-8zM34 36v10M8 50h48l-8 8H8zM22 40l5 4m14-1 6-5m-6 10 9-1',
    'ΚΟΠΗ ΨΑΛΙΔΙ':'M8 56V10h48v46M8 18h48M14 22l36 12v6H14zM8 46h48M18 46v10m28-10v10M20 10v8m24-8v8',
    'ΜΟΝΤΑΖ 1':'M12 8l20 14-7 10L5 18zM28 27l12 9-5 7-12-12M38 40l4 6M8 54h48M42 48v-6m5 8 7-4m-17 4-6-4M15 8l5-5',
    'ΜΟΝΤΑΖ 2':'M10 8h28l4 20-8 12H16L6 28zM14 16h20v10H14zM20 40v6M52 26l-9 20M8 56h48M43 49l-5-3m9 4 6-3m-10 6v-4',
    'ΜΟΝΤΑΖ ΤΖΑΜΙΑ':'M14 8h36v48H14zM20 42l22-22M20 28l10-10M34 46l10-10',
    'ΣΤΡΑΝΤΖΑ':'M10 10h44v12H10zM26 22l6 12 6-12M10 54V42h12l10 8 10-8h12v12z',
    'ΨΥΚΤΙΚΑ':'M32 6v52M10 19l44 26M10 45l44-26M24 10l8 8 8-8M24 54l8-8 8 8M10 28l11-3-3-11M46 50l-3-11 11-3M10 36l11 3-3 11M46 14l-3 11 11 3',
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 64 64');
  svg.setAttribute('class', 'station-icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', paths[name]);
  svg.append(path);
  return svg;
}
function render(data) {
  const visibleStations = ['LASER', 'ΚΟΠΗ ΨΑΛΙΔΙ', 'ΣΤΡΑΝΤΖΑ', 'ΜΟΝΤΑΖ 1', 'ΜΟΝΤΑΖ 2', 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ', 'ΨΥΚΤΙΚΑ'];
  const normalize = name => (name ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().replace(/\s+/g, ' ').toLocaleUpperCase('el-GR').replace(/^ΣΤΡΑΤΖΑ$/, 'ΣΤΡΑΝΤΖΑ');
  const stations = visibleStations.map(name => ({...((data.workstations ?? []).find(row => normalize(row.name) === name) ?? {}), name}));
  lastModel = data;
  const canonical = data.canonical_orders?.canonical_version === 2 ? data.canonical_orders : null;
  const urgent = canonical ? data.urgent_orders ?? [] : [];
  const orders = urgent.length ? urgent : canonical?.complete === true ?
    (canonical.orders ?? []).filter(order => order.lifecycle === "unfinished").slice(0, 5) : [];
  el("orders-title").textContent = "5 ΠΙΟ ΕΠΕΙΓΟΥΣΕΣ ΠΑΡΑΓΓΕΛΙΕΣ";
  el("order-ranking").hidden = urgent.length > 0 || orders.length === 0;
  sourceNotes(data);
  const today = data.today ?? {};
  // Exact commercial-flow exclusion is unverified; diagnostics stay in the API.
  el("daily-kpi").textContent = "—";
  const fields = data.field_status ?? {};
  const freshness = key => ({cached: "Προηγούμενη επιτυχής ανάγνωση",
    partial: "Μερική κάλυψη: εξαιρούνται ανεπιβεβαίωτες προθεσμίες", stale: "Παλαιά δεδομένα", loading: "Φόρτωση δεδομένων",
    unavailable: "Μη διαθέσιμο από την πηγή δεδομένων",
    completion_history_unverified: "Δεν έχει επαληθευτεί ιστορικό ολοκλήρωσης"}[fields[key]] ?? "");
  el("stations").title = freshness("workstations");
  el("orders").title = `${freshness("urgent_orders")} Χωρίς επαληθευμένη προθεσμία: ${data.canonical_orders?.undated_unfinished_orders ?? "—"}`;
  const load = productionLoad(data.native_mean_order_progress_percent);
  el("overall").textContent = percent(load);
  el("overall").title = `${freshness("active_production")} Κάλυψη εγγενούς προόδου: ${percent(data.native_progress_coverage_percent)}`;
  el("production-load").setAttribute("aria-label", `ΦΟΡΤΟΣ ΠΑΡΑΓΩΓΗΣ: ${load == null ? "Μη διαθέσιμο" : percent(load)}. 0% χαμηλός φόρτος, 100% υψηλός φόρτος.`);
  el("progress-needle").setAttribute("visibility", load == null ? "hidden" : "visible");
  el("progress-needle").setAttribute("transform", `rotate(${load == null ? 0 : load * 1.8} 160 142)`);
  el("stations").replaceChildren(...stations.map(station => {
    const card = node("article", null, "station");
    const load = node("div", null, "load");
    const heading = node('div', null, 'station-heading');
    heading.append(node('h2', station.name));
    const star = node('span', null, 'priority');
    const symbol = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    symbol.setAttribute('viewBox', '0 0 24 24');
    symbol.setAttribute('aria-hidden', 'true');
    const outline = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    outline.setAttribute('d', 'M12 2l3 6 7 1-5 5 1 8-6-4-6 4 1-8-5-5 7-1z');
    symbol.append(outline); star.append(symbol);
    star.dataset.changedAt = station.last_change_at ?? '';
    updateStar(star);
    heading.append(star);
    const bar = node('div', null, 'bar');
    const value = typeof station.load_percent === 'number' && Number.isFinite(station.load_percent) && station.load_percent >= 0 ? station.load_percent : null;
    const detail = loadDetail(station.load_model);
    bar.setAttribute('role', 'meter');
    bar.setAttribute('aria-label', 'Φόρτος σταθμού: προϊόντα που πρέπει να περάσουν ως την πιο σφιχτή προθεσμία ÷ δυνατότητα σταθμού');
    load.title = `Φόρτος σταθμού (σταθμισμένα προϊόντα ÷ δυνατότητα ως την προθεσμία). ${detail}`.trim();
    if (value != null && value > 100) card.classList.add('overloaded');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    if (value != null) bar.setAttribute('aria-valuenow', String(value));
    else bar.setAttribute('aria-valuetext', 'Μη διαθέσιμο');
    for (let index = 0; index < 10; index++) {
      const segment = node('span', null, 'segment');
      segment.dataset.level = String(index + 1);
      segment.dataset.filled = String(value != null && index < Math.min(10, Math.ceil(value / 10)));
      bar.append(segment);
    }
    load.append(bar, node('strong', loadPercent(value)));
    const count = Number.isSafeInteger(station.pending_steps) && station.pending_steps >= 0 ? station.pending_steps : '—';
    const pending = node('div', null, 'pending');
    pending.append(node('span', 'Εκκρεμείς εργασίες '), node('strong', count));
    if (detail) pending.append(node('small', detail, 'load-detail'));
    card.append(heading, stationIcon(station.name), load, pending);
    return card;
  }));
  if (!stations.length) el("stations").textContent = "Δεν υπάρχουν διαθέσιμοι σταθμοί εργασίας";
  el("orders").replaceChildren(...orders.slice(0, 5).map(order => {
    const row = node("tr");
    row.append(...[order.code && order.code !== "—" ? `${order.code} (${order.id})` : order.id ?? "—", order.customer ?? "—", percent(order.native_progress ?? order.completion_percent), deadlineLabel(order.deadline)].map(value => node("td", value)));
    for (const cell of row.children) cell.title = cell.textContent;
    const value = order.native_progress ?? order.completion_percent;
    const progress = node('div', null, 'order-progress');
    const fill = node('span');
    fill.style.width = percent(value) === '—' ? '0%' : `${value}%`;
    progress.setAttribute('aria-hidden', 'true');
    progress.append(fill);
    row.children[2].append(progress);
    const todayAthens = new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Athens', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date(Date.now()));
    if (order.deadline && order.deadline < todayAthens) row.lastElementChild.className = 'overdue-deadline';
    row.lastElementChild.title = order.deadline ? "Επαληθευμένη προθεσμία ολόκληρης παραγγελίας" : "Δεν διατίθεται επαληθευμένη προθεσμία παραγγελίας";
    return row;
  }));
  if (!orders.length) {
    const row = node("tr"), cell = node("td", canonical?.complete === true ? "Δεν υπάρχουν ενεργές παραγγελίες" : "Μη διαθέσιμες επαληθευμένες ολόκληρες παραγγελίες");
    cell.colSpan = 4; row.append(cell); el("orders").append(row);
  }
  for (const [id, key] of [["active","active_work"], ["overdue","overdue_work"]]) {
    el(id).textContent = key === "overdue_work" && data.canonical_orders?.canonical_version !== 2 ? "—" : today[key] ?? "—";
    el(id).title = freshness(key === "active_work" ? "active_production" : key) ||
      (today[key] == null ? "Μη διαθέσιμο από την πηγή δεδομένων" : "");
  }
}
async function refresh() {
  const started = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/api/dashboard", {cache:"no-store", signal:controller.signal});
    if (!response.ok) throw new Error("Unavailable");
    const data = await response.json();
    if (data.schema_version !== 1 || !["loading", "live", "online", "partial", "stale", "offline", "halted"].includes(data.data_status)) throw new Error("Invalid model");
    render(data);
    state = data.data_status;
    halt = data.halt ?? null;
    if (typeof data.freshness_seconds === "number" && data.freshness_seconds > 0) freshnessMs = data.freshness_seconds * 1000;
    observedAt = data.observed_at;
  } catch {
    state = "disconnected";
  } finally {
    clearTimeout(timeout);
    status();
    setTimeout(refresh, Math.max(0, 45000 - (Date.now() - started)));
  }
}
function missingLogo() {
  if (!el('logo').dataset.fallback) {
    el('logo').dataset.fallback = 'true';
    el('logo').src = '/static/assets/saridis-logo.png';
    el('logo').style.maxWidth = '350px';
  } else { el('logo').hidden = true; el('logo-placeholder').hidden = false; }
}
function fitCanvas() {
  const scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
  el('factory-canvas').style.transform = `translate(-50%, -50%) scale(${scale})`;
}
if (typeof window !== 'undefined') {
  window.addEventListener('resize', fitCanvas);
  document.addEventListener('fullscreenchange', fitCanvas);
  fitCanvas();
}
el("logo").addEventListener("error", missingLogo);
if (el("logo").complete && !el("logo").naturalWidth) missingLogo();
tick();
setInterval(tick, 1000);
refresh();
