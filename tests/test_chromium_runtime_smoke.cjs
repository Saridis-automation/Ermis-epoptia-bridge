'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {smoke} = require('../chromium_runtime_smoke.cjs');

test('uses resolver, blocks requests and sockets, visits only blank, closes browser', async () => {
  const visits = [];
  let closed = 0, blocked = 0;
  const chromium = {launch: async options => {
    assert.equal(options.executablePath, '/synthetic/chrome');
    assert.equal(options.timeout, 10000);
    return {close: async () => { closed++; }, newContext: async options => {
      assert.deepEqual(options, {offline: true, serviceWorkers: 'block'});
      return {
        route: async (pattern, handler) => { assert.equal(pattern, '**/*'); await handler({abort: () => {blocked++;}}); },
        routeWebSocket: async (pattern, handler) => { await handler({close: () => {blocked++;}}); },
        newPage: async () => ({goto: async url => {visits.push(url);}, url: () => 'about:blank'}),
      };
    }};
  }};
  assert.deepEqual(await smoke({load: () => ({chromium}), resolve: value => {
    assert.equal(value, chromium); return {executablePath: '/synthetic/chrome'};
  }}), {status: 'completed'});
  assert.deepEqual(visits, ['about:blank']);
  assert.equal(blocked, 2);
  assert.equal(closed, 1);
});

test('missing dependencies and browser failures contain no exception detail', async () => {
  assert.deepEqual(await smoke({load: () => {throw Object.assign(Error('PRIVATE'), {code: 'MODULE_NOT_FOUND'});}}),
    {status: 'playwright_missing'});
  assert.deepEqual(await smoke({load: () => ({}), resolve: () => ({status: 'chromium_missing'})}),
    {status: 'chromium_missing'});
  let closed = 0;
  const browser = {close: async () => {closed++;}, newContext: async () => {throw Error('PRIVATE');}};
  assert.deepEqual(await smoke({load: () => ({chromium: {launch: async () => browser}}),
    resolve: () => ({executablePath: '/synthetic/chrome'})}), {status: 'launch_failed'});
  assert.equal(closed, 1);
});
