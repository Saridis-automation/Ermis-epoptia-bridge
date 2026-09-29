'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {allowed, check} = require('../epoptia_browser_access_check.cjs');
const {scheduled} = require('./browser_policy_fixture.cjs');
const p = {origin: 'https://example.invalid', sessionFile: null,
  pages: {production_report: '/reports/factory/productiondata'}, assets: []};
const target = p.origin + p.pages.production_report;

test('only exact approved origin and GET document; reject write-capable routes', () => {
  assert.equal(allowed(target, 'GET', 'document', p), true);
  for (const url of ['https://other.invalid' + p.pages.production_report,
    target + '?delete=1', target + '#x', p.origin + '/logout', p.origin + '/delete',
    p.origin + '/a/../reports/factory/productiondata', target.replace('https:', 'http:')]) {
    assert.equal(allowed(url, 'GET', 'document', p), false);
  }
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']) {
    assert.equal(allowed(target, method, 'document', p), false);
  }
  for (const type of ['fetch', 'xhr', 'script', 'stylesheet', 'websocket']) {
    assert.equal(allowed(target, 'GET', type, p), false);
  }
});

test('missing and invalid sessions never launch', async () => {
  for (const [input, expected] of [['auth_material_missing', 'session_missing'],
    ['session_state_invalid', 'access_denied'], ['session_policy_invalid', 'access_denied']]) {
    assert.deepEqual(await check({p, session: () => ({status: input}),
      launch: () => { throw Error('must not launch'); }}), {ok: false, status: expected});
  }
});

test('sanitized states, redirects blocked, cleanup on timeout and errors', async () => {
  for (const [code, marker, expected] of [[200, '', 'navigation_failed'],
    [200, 'password', 'login_required'], [200, 'one-time-code', 'mfa_required'],
    [401, '', 'login_required'], [403, '', 'circuit_open'],
    [429, '', 'circuit_open'], [500, '', 'circuit_open'], [599, '', 'circuit_open'],
    [302, '', 'login_required'], [200, 'timeout', 'timeout'],
    [200, 'error', 'navigation_failed']]) {
    let handler, closed = 0, disposed = 0, fetches = 0;
    const frame = {};
    const page = {mainFrame: () => frame, evaluate: async () => 0,
      locator: selector => ({count: async () => marker && selector.includes(marker) ? 1 : 0}),
      goto: async (url, options) => {
        assert.equal(url, target); assert.equal(options.timeout, 10000);
        if (marker === 'timeout') throw Object.assign(Error('PRIVATE'), {name: 'TimeoutError'});
        if (marker === 'error') throw Error('PRIVATE');
        let aborted = false;
        const route = {request: () => ({url: () => target, method: () => 'GET',
          resourceType: () => 'document', isNavigationRequest: () => true, frame: () => frame}),
          abort: async () => { aborted = true; },
          fetch: async options => {
            fetches++; assert.deepEqual(options, {maxRedirects: 0, maxRetries: 0, timeout: 5000});
            return {status: () => code, headers: () => ({'content-type': 'text/html', 'content-length': '7'}),
              body: async () => Buffer.from('PRIVATE'), dispose: async () => { disposed++; }};
          }, fulfill: async options => {
            assert.equal(options.headers['set-cookie'], undefined);
            assert.match(options.headers['content-security-policy'], /default-src 'none'/);
          }};
        await handler(route);
        if (aborted) throw Error('PRIVATE');
        await handler(route); // Even an identical second navigation is prohibited.
        assert.equal(fetches, 1);
      }};
    const context = {routeWebSocket: async (_, cb) => cb({close: () => {}}),
      newPage: async () => page, route: async (_, cb) => { handler = cb; },
      close: async () => { closed++; }};
    const browser = {newContext: async options => {
      assert.equal(options.javaScriptEnabled, false);
      assert.equal(options.serviceWorkers, 'block');
      assert.equal(options.acceptDownloads, false);
      return context;
    }, close: async () => { closed++; }};
    assert.deepEqual(await scheduled(scheduler => check({p, scheduler, session: () => ({state: {cookies: [], origins: []}}),
      launch: async () => ({browser})})), {ok: false, status: expected});
    assert.equal(closed, 2);
    assert.equal(disposed, fetches);
    assert.ok(fetches <= 1);
  }
});
