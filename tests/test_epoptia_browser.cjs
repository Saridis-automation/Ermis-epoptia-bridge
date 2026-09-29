'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {classify, schema, sanitizeDOM, loadSession, inspectContext, inspectBrowser} = require('../epoptia_browser.cjs');
const {scheduled} = require('./browser_policy_fixture.cjs');
const p = {...require('../epoptia_browser_policy.cjs'), origin: 'https://example.invalid'};

test('exact GET origin/route/type/query allowlist', () => {
  assert.equal(classify(p.origin + '/workorders?page=1', 'GET', 'xhr', p).route, 'workorders');
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD', 'get']) {
    assert.equal(classify(p.origin + '/workorders', method, 'fetch', p), null);
  }
  for (const suffix of ['/logout', '/workorders/delete', '/workorders?token=PRIVATE',
    '/workorders?action=delete', '/workorders?page=1&page=2', '/workorders?page=9999',
    '/workorders?page=PRIVATE', '/workorders#PRIVATE', '/%77orkorders',
    '/reports/../workorders', '/workorders?date=PRIVATE']) {
    assert.equal(classify(p.origin + suffix, 'GET', 'fetch', p), null);
  }
  for (const url of ['https://other.invalid/workorders', 'http://example.invalid/workorders',
    'https://PRIVATE@example.invalid/workorders', 'file:///workorders',
    'https://example.invalid:443/workorders', 'https://example.invalid\\workorders']) {
    assert.equal(classify(url, 'GET', 'document', p), null);
  }
  assert.equal(classify(p.origin + '/workorders', 'GET', 'image', p), null);
  assert.equal(classify(p.origin + '/assets/app.js', 'GET', 'script', p), null);
});

test('only recognized names and types survive schema and DOM redaction', () => {
  const result = schema({data: [{eta: 'PRIVATE', deadline: 'PRIVATE',
    authorization: 'PRIVATE', PRIVATE: {due_date: 'PRIVATE'}}], token: 'PRIVATE'});
  assert.deepEqual(result.fields.map(f => f.name), ['data', 'deadline', 'eta']);
  assert.equal(result.omitted_fields, 3);
  assert.deepEqual(result.date_candidates, [
    {name: 'eta', location: '$.data[].eta'},
    {name: 'deadline', location: '$.data[].deadline'},
  ]);
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  const dom = sanitizeDOM({tags: {table: 'PRIVATE', form: 20000, PRIVATE: 5},
    fields: ['PRIVATE', 'eta', 'eta', 'authorization'], password_present: 'PRIVATE'});
  assert.equal(dom.tags.table, 0);
  assert.equal(dom.tags.form, 10000);
  assert.deepEqual(dom.fields, ['eta']);
  assert.ok(!JSON.stringify(dom).includes('PRIVATE'));
});

test('scope orchestration uses fixed pages, isolates smoke, and withholds session metadata', async () => {
  const visits = [], options = [];
  let browserClosed = 0, contextsClosed = 0;
  const browser = {
    close: async () => { browserClosed++; },
    newContext: async opts => {
      options.push(opts);
      return {
        route: async () => {}, routeWebSocket: async () => {}, addInitScript: async () => {},
        close: async () => { contextsClosed++; },
        newPage: async () => ({
          goto: async url => { visits.push(url); }, waitForTimeout: async () => {},
          evaluate: async () => opts.storageState ?
            {tags: {input: 1}, fields: ['eta', 'PRIVATE'], password_present: false} : true,
        }),
      };
    },
  };
  const dates = await scheduled(s => inspectBrowser(browser, 'dates', {state: {}}, p, s));
  assert.equal(dates.ok, true);
  assert.deepEqual(visits, Object.values(p.pages).map(route => p.origin + route));
  assert.ok(dates.pages.every(page => page.date_candidates[0].name === 'eta'));
  assert.ok(!JSON.stringify(dates).includes('PRIVATE'));
  const session = await scheduled(s => inspectBrowser(browser, 'session', {state: {}}, p, s));
  assert.deepEqual(session, {ok: false, usable: false, status: 'session_unverified'});
  const smoke = await inspectBrowser(browser, 'smoke', {}, p);
  assert.equal(smoke.ok, true);
  assert.equal(visits.at(-1), 'about:blank');
  assert.equal(options.at(-1).storageState, undefined);
  assert.equal(browserClosed, 3);
  assert.equal(contextsClosed, 6);
});

test('session defaults fail closed without reading state', () => {
  assert.deepEqual(loadSession(), {status: 'auth_material_missing'});
  assert.deepEqual(loadSession({...p, origin: null, sessionFile: 'unused'}),
    {status: 'session_policy_invalid'});
  assert.deepEqual(loadSession({...p, sessionFile: '../outside'}), {status: 'session_state_invalid'});
});

test('synthetic session requires private regular file and exact origin scope', () => {
  const base = path.resolve(__dirname, '../.cache');
  fs.mkdirSync(base, {recursive: true});
  const dir = fs.mkdtempSync(path.join(base, 'browser-session-test-'));
  fs.chmodSync(dir, 0o700);
  const file = path.join(dir, 'state.json');
  const pp = {...p, sessionFile: path.relative(path.resolve(__dirname, '..'), file)};
  const state = {cookies: [{name: 'synthetic', value: 'SYNTHETIC', domain: 'example.invalid',
    path: '/', secure: true, httpOnly: true, expires: -1, sameSite: 'Lax'}], origins: []};
  try {
    fs.writeFileSync(file, JSON.stringify(state), {mode: 0o600});
    assert.ok(loadSession(pp).state);
    fs.writeFileSync(file, JSON.stringify({cookies: [], origins: []}));
    assert.deepEqual(loadSession(pp), {status: 'auth_material_missing'});
    fs.writeFileSync(file, JSON.stringify(state));
    fs.chmodSync(file, 0o644);
    assert.deepEqual(loadSession(pp), {status: 'session_state_invalid'});
    fs.chmodSync(file, 0o600);
    state.cookies[0].domain = '.example.invalid';
    fs.writeFileSync(file, JSON.stringify(state));
    assert.deepEqual(loadSession(pp), {status: 'session_state_invalid'});
    fs.unlinkSync(file);
    assert.deepEqual(loadSession(pp), {status: 'auth_material_missing'});
    fs.symlinkSync('missing', file);
    assert.deepEqual(loadSession(pp), {status: 'session_state_invalid'});
  } finally { fs.unlinkSync(file); fs.rmdirSync(dir); }
});

test('transport denies writes, redirects, unknown routes and oversized bodies', async () => {
  const frame = {};
  let handler, fetches = 0, aborted = 0, fulfilled = 0, closed = false;
  const response = (status, body, length = body.length) => ({
    status: () => status,
    headers: () => ({'content-type': 'application/json', 'content-length': String(length),
      authorization: 'PRIVATE', 'set-cookie': 'PRIVATE', location: 'https://outside.invalid'}),
    body: async () => Buffer.from(body), dispose: async () => {},
  });
  async function request(url, method, reply, navigation = false, requestFrame = frame) {
    await handler({
      request: () => ({url: () => url, method: () => method, resourceType: () => 'fetch',
        isNavigationRequest: () => navigation, frame: () => requestFrame}),
      fetch: async options => { assert.equal(options.maxRedirects, 0); fetches++; return reply; },
      abort: async () => { aborted++; },
      fulfill: async options => {
        fulfilled++;
        assert.deepEqual(Object.keys(options.headers).sort(),
          ['content-security-policy', 'content-type', 'x-dns-prefetch-control']);
      },
    });
  }
  const page = {
    mainFrame: () => frame,
    goto: async () => {
      for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
        await request(p.origin + '/workorders', method);
      }
      await request(p.origin + '/logout', 'GET');
      await request('https://outside.invalid/workorders', 'GET');
      await request(p.origin + '/workorders?token=PRIVATE', 'GET');
      await request(p.origin + '/workorders', 'GET', null, true, {});
      await request(p.origin + '/workorders', 'GET', response(200, '{"eta":"PRIVATE"}'));
      await request(p.origin + '/workorders', 'GET', response(200, '{}', 999999));
      await request(p.origin + '/workorders', 'GET', response(302, '{}'));
    },
    waitForTimeout: async () => {},
    evaluate: async () => ({tags: {}, fields: ['PRIVATE', 'eta']}),
  };
  const context = {
    newPage: async () => page,
    routeWebSocket: async (glob, cb) => { let denied = false; cb({close: () => { denied = true; }}); assert.ok(denied); },
    addInitScript: async () => {},
    route: async (glob, cb) => { handler = cb; },
    close: async () => { closed = true; },
  };
  const result = await scheduled(s => inspectContext(context, 'production_report', p, s));
  assert.deepEqual(result, {ok: false, status: 'login_required'});
  assert.equal(fetches, 3);
  assert.equal(fulfilled, 1);
  assert.equal(aborted, 10);
  assert.ok(closed);
});
