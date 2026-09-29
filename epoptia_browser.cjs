'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {Scheduler, visibleDenial} = require('./epoptia_browser_scheduler.cjs');
const {PrivateStore} = require('./epoptia_browser_session.cjs');
const policy = require('./epoptia_browser_policy.cjs');
const {launchBrowser, reason} = require('./epoptia_browser_runtime.cjs');

const LIMIT = 512 * 1024;
const MAX_REQUESTS = 80;
// Only literal known schema names leave the process, never arbitrary object keys.
const FIELDS = new Set(('id data rows total recordsTotal recordsFiltered workorder_id ' +
  'workorderline_id wol_id status date eta ETA due_date deadline delivery_date ' +
  'start_date end_date planned_start planned_end planned_start_date planned_end_date ' +
  'estimated_start estimated_end estimated_completion estimated_completion_date ' +
  'actual_start actual_end actual_completion actual_completion_date completed_at ' +
  'created_at updated_at startDate endDate dueDate deliveryDate estimatedCompletionDate ' +
  'completionDate plannedStartDate plannedEndDate actualStartDate actualEndDate ' +
  'quantity completed_quantity remaining_quantity workhours workstation_id').split(' '));
const QUERY = new Set(['page', 'limit', 'offset', 'start_date', 'end_date', 'date']);
const TYPES = new Set(['document', 'script', 'stylesheet', 'xhr', 'fetch']);
const failure = status => ({ok: false, status});
const DATE_FIELDS = new Set([...FIELDS].filter(name =>
  /date|deadline|eta|start|end|completion|completed_at|created_at|updated_at/i.test(name)));

function originValid(origin) {
  try {
    const u = new URL(origin);
    return u.protocol === 'https:' && u.origin === origin && !u.username && !u.password;
  } catch { return false; }
}

function classify(raw, method, type, p = policy) {
  if (!originValid(p.origin) || method !== 'GET' || !TYPES.has(type) ||
      typeof raw !== 'string' || raw.length > 2048 || /[\\\s%]/.test(raw)) return null;
  try {
    const u = new URL(raw);
    if (u.origin !== p.origin || u.username || u.password || u.hash) return null;
    // Reject dot-segment normalization, ambiguous encodings and repeated params.
    if (raw.split('?')[0] !== p.origin + u.pathname) return null;
    const page = Object.entries(p.pages).find(([, value]) => value === u.pathname);
    const asset = p.assets.includes(u.pathname);
    if (!page && !asset) return null;
    if (asset && !['script', 'stylesheet'].includes(type)) return null;
    if (page && !['document', 'xhr', 'fetch'].includes(type)) return null;
    const names = [...u.searchParams.keys()];
    if (new Set(names).size !== names.length || names.length > 6) return null;
    for (const [name, value] of u.searchParams) {
      if (asset || !QUERY.has(name)) return null;
      if (['page', 'limit', 'offset'].includes(name)) {
        if (!/^\d{1,4}$/.test(value) || Number(value) > 1000) return null;
      } else if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    }
    return {route: page ? page[0] : 'approved_asset', resource_type: type,
      method: 'GET', query_names: names.sort()};
  } catch { return null; }
}

function schema(value) {
  const found = new Map(), candidates = new Map();
  let visited = 0, omitted = 0;
  function walk(item, depth, location = '$') {
    if (++visited > 1000 || depth > 6 || item === null || typeof item !== 'object') return;
    if (Array.isArray(item)) { item.slice(0, 10).forEach(v => walk(v, depth + 1, location + '[]')); return; }
    for (const [key, v] of Object.entries(item).slice(0, 100)) {
      if (!FIELDS.has(key)) { omitted++; continue; }
      const type = v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v;
      if (!found.has(key)) found.set(key, new Set());
      found.get(key).add(type);
      const fieldLocation = location + '.' + key;
      if (DATE_FIELDS.has(key) && candidates.size < 100) {
        candidates.set(fieldLocation, {name: key, location: fieldLocation});
      }
      walk(v, depth + 1, fieldLocation);
    }
  }
  walk(value, 0);
  return {fields: [...found].sort().map(([name, types]) => ({name, types: [...types].sort()})),
    date_candidates: [...candidates.values()],
    omitted_fields: Math.min(omitted, 1000)};
}

function sanitizeDOM(raw) {
  const tags = {};
  for (const tag of ['table', 'thead', 'th', 'tr', 'form', 'input', 'select', 'time']) {
    const n = raw?.tags?.[tag];
    tags[tag] = Number.isInteger(n) && n >= 0 ? Math.min(n, 10000) : 0;
  }
  return {tags, fields: Array.isArray(raw?.fields) ?
    [...new Set(raw.fields.filter(v => FIELDS.has(v)))].sort() : [],
  password_present: raw?.password_present === true};
}

function loadSession(p = policy) {
  if (!p.sessionFile) return {status: 'auth_material_missing'};
  if (!originValid(p.origin)) return {status: 'session_policy_invalid'};
  if (p.sessionFile === 'private') {
    try { return new PrivateStore().session(p.origin); }
    catch { return {status: 'session_state_invalid'}; }
  }
  let fd;
  try {
    const parts = p.sessionFile.split('/');
    if (parts.some(v => !v || v === '.' || v === '..') || path.isAbsolute(p.sessionFile)) throw Error();
    let dir = __dirname;
    for (const part of parts.slice(0, -1)) {
      dir = path.join(dir, part);
      const s = fs.lstatSync(dir);
      if (!s.isDirectory() || s.isSymbolicLink() || s.uid !== process.getuid() || (s.mode & 0o022)) throw Error();
    }
    const parent = fs.lstatSync(dir);
    if (parent.mode & 0o077) throw Error();
    fd = fs.openSync(path.join(__dirname, p.sessionFile), fs.constants.O_RDONLY |
      fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const s = fs.fstatSync(fd);
    if (!s.isFile() || s.uid !== process.getuid() || s.nlink !== 1 ||
        (s.mode & 0o077) || s.size > LIMIT) throw Error();
    const state = JSON.parse(fs.readFileSync(fd, 'utf8'));
    const host = new URL(p.origin).hostname;
    if (!Array.isArray(state.cookies) || !Array.isArray(state.origins) ||
        state.cookies.some(c => c.domain !== host || c.secure !== true) ||
        state.origins.some(o => o.origin !== p.origin || ('indexedDB' in o && !Array.isArray(o.indexedDB)))) throw Error();
    if (!state.cookies.length && !state.origins.some(o => o.localStorage?.length || o.indexedDB?.length)) {
      return {status: 'auth_material_missing'};
    }
    return {state};
  } catch (error) {
    return {status: error.code === 'ENOENT' ? 'auth_material_missing' :
      reason(error) === 'runtime_permission_denied' ? 'runtime_permission_denied' : 'session_state_invalid'};
  }
  finally { if (fd !== undefined) fs.closeSync(fd); }
}

async function inspectContext(context, pageName, p = policy, scheduler) {
  if (!scheduler) return new Scheduler().run(s => inspectContext(context, pageName, p, s));
  if (!Object.hasOwn(p.pages, pageName) || !originValid(p.origin)) return failure('invalid_page');
  const network = [], tasks = new Set();
  let blocked = 0, count = 0, navigationUsed = false;
  const page = await context.newPage();
  await context.routeWebSocket('**/*', socket => socket.close());
  await context.addInitScript(() => {
    document.addEventListener('submit', e => e.preventDefault(), true);
    HTMLFormElement.prototype.submit = function () {};
    HTMLFormElement.prototype.requestSubmit = function () {};
    window.open = () => null;
    navigator.sendBeacon = () => false;
    for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
      Object.defineProperty(window, name, {value: undefined, configurable: false, writable: false});
    }
  });
  await context.route('**/*', route => {
    const task = (async () => {
      const request = route.request();
      if (request.isNavigationRequest() && request.frame() === page.mainFrame() &&
          request.url() !== p.origin + p.pages[pageName]) {
        scheduler.observe(0, true); await route.abort(); return;
      }
      const meta = classify(request.url(), request.method(), request.resourceType(), p);
      if (scheduler.state.circuit || ++count > MAX_REQUESTS || !meta ||
          (request.isNavigationRequest() && (request.frame() !== page.mainFrame() ||
           request.url() !== p.origin + p.pages[pageName]))) {
        blocked = Math.min(blocked + 1, 10000);
        await route.abort(); return;
      }
      if (request.isNavigationRequest()) {
        if (navigationUsed) { await route.abort(); return; }
        navigationUsed = true;
      }
      let response;
      try {
        // Fetch without following ANY redirects, including same-origin login routes.
        response = await route.fetch({maxRedirects: 0, maxRetries: 0, timeout: 5000});
        const status = response.status();
        meta.status_class = `${Math.floor(status / 100)}xx`;
        network.push(meta);
        if (scheduler.observe(status)) { await route.abort(); return; }
        const headers = response.headers();
        const mime = (headers['content-type'] || '').split(';')[0].trim();
        const mimes = {document: ['text/html'], script: ['application/javascript', 'text/javascript'],
          stylesheet: ['text/css'], xhr: ['application/json'], fetch: ['application/json']};
        const size = Number(headers['content-length']);
        if (status !== 200 || !mimes[meta.resource_type].includes(mime) ||
            !Number.isInteger(size) || size < 0 || size > LIMIT) {
          meta.body_status = 'withheld'; await route.abort(); return;
        }
        const body = await response.body();
        if (body.length > LIMIT) { meta.body_status = 'too_large'; await route.abort(); return; }
        if (mime === 'application/json') {
          try { meta.schema = schema(JSON.parse(body.toString('utf8'))); }
          catch { meta.body_status = 'invalid_json'; }
        }
        // Do not forward Set-Cookie, redirect, reporting, refresh or other headers.
        await route.fulfill({status: 200, body, headers: {'content-type': mime,
          'x-dns-prefetch-control': 'off',
          'content-security-policy': "default-src 'none'; script-src 'self' 'unsafe-inline'; " +
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'none'; " +
            "frame-src 'none'; worker-src 'none'; base-uri 'none'"}});
      } catch { meta.body_status = 'unavailable'; await route.abort().catch(() => {}); }
      finally { if (response) await response.dispose(); }
    })();
    tasks.add(task);
    task.finally(() => tasks.delete(task)).catch(() => {});
    return task;
  });
  let loaded = true;
  try { await scheduler.navigate(() => page.goto(p.origin + p.pages[pageName], {waitUntil: 'domcontentloaded', timeout: 12000})); }
  catch { loaded = false; }
  if (loaded) await page.waitForTimeout(1000);
  let dom = {tags: {}, fields: [], password_present: false};
  if (loaded) {
    dom = sanitizeDOM(await page.evaluate(known => {
      const tags = {};
      for (const tag of ['table', 'thead', 'th', 'tr', 'form', 'input', 'select', 'time']) {
        tags[tag] = Math.min(document.querySelectorAll(tag).length, 10000);
      }
      const fields = new Set();
      for (const el of [...document.querySelectorAll('[name], [data-field]')].slice(0, 2000)) {
        for (const attr of ['name', 'data-field']) {
          const name = el.getAttribute(attr);
          if (known.includes(name)) fields.add(name);
        }
      }
      return {tags, fields: [...fields].sort(), password_present: !!document.querySelector('input[type=password]')};
    }, [...FIELDS]));
  }
  if (loaded) {
    const visibleCode = await visibleDenial(page);
    scheduler.observe(Number.isInteger(visibleCode) ? visibleCode : 0, dom.password_present);
  }
  await context.close();
  await Promise.allSettled([...tasks]);
  if (scheduler.state.circuit) return scheduler.observe(0);
  if (dom.password_present) return failure('login_required');
  return {ok: loaded, status: loaded ? 'ok' : 'inspection_incomplete', page: pageName,
    dom, network: network.slice(0, MAX_REQUESTS), blocked_requests: blocked,
    redacted: true};
}

async function main() {
  const pageName = process.argv[2];
  if (!Object.hasOwn(policy.pages, pageName) && !['session', 'dates', 'smoke'].includes(pageName)) {
    return failure('invalid_page');
  }
  const session = pageName === 'smoke' ? {} : loadSession();
  if (pageName !== 'smoke' && !session.state) return pageName === 'session' ?
    {...failure(session.status), usable: false} : failure(session.status);
  const launch = async scheduler => {
    const {browser, status} = await launchBrowser();
    if (!browser) return pageName === 'session' ? {...failure(status), usable: false} : failure(status);
    return inspectBrowser(browser, pageName, session, policy, scheduler);
  };
  return pageName === 'smoke' ? launch() : new Scheduler().run(launch);
}

async function inspectBrowser(browser, pageName, session, p = policy, scheduler) {
  if (pageName !== 'smoke' && !scheduler) {
    let entered = false;
    try { return await new Scheduler().run(s => { entered = true; return inspectBrowser(browser, pageName, session, p, s); }); }
    finally { if (!entered) await browser.close(); }
  }
  try {
    if (pageName === 'smoke') {
      const context = await browser.newContext({serviceWorkers: 'block', acceptDownloads: false});
      try {
        await context.route('**/*', route => route.abort());
        await context.routeWebSocket('**/*', socket => socket.close());
        const page = await context.newPage();
        await page.goto('about:blank');
        const ok = await page.evaluate(() => document.location.href === 'about:blank');
        return {ok, status: ok ? 'ok' : 'launch_failed', redacted: true};
      } finally { await context.close(); }
    }
    if (pageName === 'dates') {
      const pages = [];
      for (const name of Object.keys(p.pages)) {
        const context = await browser.newContext({storageState: session.state,
          serviceWorkers: 'block', acceptDownloads: false, permissions: []});
        const result = await inspectContext(context, name, p, scheduler);
        const date_candidates = (result.dom?.fields || []).filter(f => DATE_FIELDS.has(f))
          .map(name => ({name, location: 'DOM name/data-field'}));
        pages.push({...result, page: name, date_candidates});
        if (['login_required', 'circuit_open'].includes(result.status)) return {...result, pages, redacted: true};
      }
      const ok = pages.length === Object.keys(p.pages).length && pages.every(p => p.ok);
      return {ok, status: ok ? 'ok' : 'inspection_incomplete', pages, redacted: true};
    }
    const context = await browser.newContext({storageState: session.state,
      serviceWorkers: 'block', acceptDownloads: false, permissions: []});
    const result = await inspectContext(context, pageName === 'session' ? 'production_report' : pageName, p, scheduler);
    // A successful 200 alone cannot prove authentication. Until an authenticated
    // page marker is reviewed, report uncertainty rather than a false positive.
    if (pageName === 'session') return {ok: false, usable: false,
      status: result.ok ? 'session_unverified' :
        result.status === 'login_required' ? 'login_required' : result.status};
    return result;
  } finally { await browser.close(); }
}

module.exports = {classify, schema, sanitizeDOM, loadSession, inspectContext, inspectBrowser};
if (require.main === module) {
  // Never serialize exceptions, browser logs, headers, session state or raw bodies.
  main().then(result => process.stdout.write(JSON.stringify(result)))
    .catch(error => process.stdout.write(JSON.stringify(failure(reason(error)))));
}
