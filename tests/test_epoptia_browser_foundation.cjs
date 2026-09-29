'use strict';
const {test, mock} = require('node:test');
// Offline absent-runtime fixture: never contact the real control socket.
mock.method(require('node:net'), 'createConnection', () => {
  const socket = new (require('node:events').EventEmitter)();
  socket.setTimeout = () => {}; socket.destroy = () => {};
  queueMicrotask(() => socket.emit('error', Error('synthetic_unavailable')));
  return socket;
});
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {Scheduler, visibleDenial} = require('../epoptia_browser_scheduler.cjs');
const {PrivateStore} = require('../epoptia_browser_session.cjs');
const {LoginSupervisor, loopback, command} = require('../epoptia_browser_login.cjs');
const {scheduled, FixtureStore} = require('./browser_policy_fixture.cjs');
const origin = 'https://example.invalid';
const state = {cookies: [{name: 'session', value: 'SYNTHETIC', domain: 'example.invalid',
  path: '/', expires: -1, secure: true, httpOnly: true, sameSite: 'Strict'}], origins: []};

test('timing floors, ten-navigation cooldown, settle, and persisted reservations', async () => {
  for (const options of [{navigationMs: 7999}, {settleMs: 2999}, {cooldownMs: 59999}, {navigationMs: NaN}])
    assert.throws(() => new Scheduler(options));
  await scheduled(async s => {
    const times = [];
    for (let i = 0; i < 11; i++) await s.navigate(() => times.push(s.now()));
    assert.deepEqual(times.slice(0, 10), Array.from({length: 10}, (_, i) => i * 8000));
    assert.equal(times[10] - times[9], 60000);
    const before = s.now();
    await s.click(async () => {});
    assert.equal(s.now() - before, 3000);
    assert.equal(s.store.read('scheduler.json').count, 1);
    return {ok: true};
  }).then(value => assert.equal(value.ok, true));
});

test('in-process and separate-process locks reject overlap, including stale locks', async () => {
  await scheduled(async s => {
    assert.deepEqual(await new Scheduler({store: s.store}).run(() => assert.fail()), {ok: false, status: 'busy'});
    const child = spawnSync(process.execPath, ['-e',
      `const {FixtureStore}=require('./tests/browser_policy_fixture.cjs');
       const {Scheduler}=require('./epoptia_browser_scheduler.cjs');
       new Scheduler({store:new FixtureStore(process.argv[1])}).run(()=>({ok:true}))
         .then(r=>{process.exitCode = r.status === 'busy' ? 0 : 7;});`, s.store.directory], {cwd: path.resolve(__dirname, '..'), stdio: 'ignore'});
    assert.equal(child.status, 0);
    assert.equal(child.error, undefined);
    return {ok: true};
  }).then(value => assert.equal(value.ok, true));
});

test('403, 429, every 5xx open durable circuit; no navigation or transport retry', async () => {
  for (const code of [403, 429, ...Array.from({length: 100}, (_, i) => 500 + i)]) {
    const value = await scheduled(async s => {
      assert.equal(s.observe(code).status, 'circuit_open');
      assert.equal((await s.navigate(() => assert.fail())).status, 'circuit_open');
      assert.equal((await s.localTransport(() => assert.fail())).status, 'circuit_open');
      assert.equal(s.store.read('scheduler.json').circuit, 'site_denied');
      return {ok: true};
    });
    assert.equal(value.ok, true);
  }
});

test('bounded backoff only for local IPC, login redirect invalidates session', async () => {
  const value = await scheduled(async s => {
    let attempts = 0;
    await s.localTransport(async () => { attempts++; throw {localIPC: true, code: 'EPIPE'}; });
    assert.equal(attempts, 3); assert.equal(s.now(), 3000);
    attempts = 0;
    await s.localTransport(async () => { attempts++; throw {code: 'ECONNRESET', message: 'SYNTHETIC'}; });
    assert.equal(attempts, 1);
    assert.equal(s.observe(302).status, 'login_required');
    assert.deepEqual(s.store.read('invalid.json'), {invalid: true});
    return {ok: true};
  });
  assert.equal(value.ok, true);
});

test('private atomic cookie persistence, expiry, invalid marker, permissions, and symlink refusal', async () => {
  const value = await scheduled(async s => {
    const store = s.store;
    store.saveSession(state, origin, 10000, 0);
    assert.equal(fs.statSync(store.file('session.json')).mode & 0o777, 0o600);
    assert.equal(store.session(origin, 1).state.cookies[0].value, 'SYNTHETIC');
    const inode = fs.statSync(store.file('session.json')).ino;
    store.saveSession(state, origin, 12000, 0);
    assert.notEqual(fs.statSync(store.file('session.json')).ino, inode);
    assert.equal(store.session(origin, 12000).status, 'login_required');
    store.saveSession(state, origin, 10000, 0);
    assert.equal(store.read('invalid.json'), null);
    fs.chmodSync(store.file('session.json'), 0o644);
    assert.throws(() => store.session(origin, 1));
    assert.throws(() => store.saveSession(state, origin, 10000, 0));
    fs.chmodSync(store.file('session.json'), 0o600);
    assert.throws(() => store.saveSession({...state, origins: [{origin, localStorage: []}]}, origin, 10000, 0));
    store.remove('session.json');
    fs.symlinkSync('missing', store.file('session.json'));
    assert.throws(() => store.read('session.json'));
    assert.throws(() => store.write('session.json', {}));
    assert.ok(!fs.readdirSync(store.directory).some(name => name.endsWith('.tmp')));
    assert.throws(() => new PrivateStore(path.join(store.directory, '..')).prepare());
    return {ok: true};
  });
  assert.equal(value.ok, true);
});

test('loopback allowlist, fail-closed commands, verified lifecycle and timeout cleanup', async () => {
  for (const host of ['0.0.0.0', '::', 'localhost', '127.0.0.1.evil', '192.0.2.1']) {
    assert.equal(loopback(host), false); assert.throws(() => new LoginSupervisor({host}));
  }
  for (const action of ['start', 'status', 'stop', 'finalize'])
    assert.equal((await command(action)).status, 'login_not_ready');
  const value = await scheduled(async s => {
    let stopped = 0, timeout;
    const backend = {start: async options => {
      assert.equal(options.host, '127.0.0.1');
      assert.equal(fs.statSync(options.secretFile).mode & 0o777, 0o600);
      return {storageState: async () => state};
    }, stop: async () => { stopped++; }};
    const supervisor = new LoginSupervisor({store: s.store, scheduler: s, origin, backend, verify: async () => true,
      timer: cb => { timeout = cb; return 1; }, cancel: () => {}});
    assert.equal((await supervisor.start()).status, 'awaiting_login');
    const output = await supervisor.finalize();
    assert.equal(output.status, 'enrolled');
    assert.equal(stopped, 1); assert.equal(s.store.read('console.secret'), null);
    assert.ok(!JSON.stringify(output).includes('SYNTHETIC'));
    await supervisor.start(); await timeout();
    // Timer triggers an asynchronous cleanup; wait for its microtasks.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(s.store.read('console.secret'), null);
    assert.equal(supervisor.status().status, 'ready_not_enrolled');
    return {ok: true};
  });
  assert.equal(value.ok, true);
});


test('visible denial pages yield only a numeric status, never body text', async () => {
  for (const [body, expected] of [['Error 403 SYNTHETIC', 403], ['Too many requests', 429],
    ['HTTP 502', 502], ['Service unavailable', 503], ['Ordinary page', 0]]) {
    const page = {evaluate: async fn => {
      global.document = {body: {innerText: body}};
      try { return fn(); } finally { delete global.document; }
    }};
    assert.equal(await visibleDenial(page), expected);
  }
});


test('failed verification and failed launch remove the console secret; unsafe directory never launches', async () => {
  const value = await scheduled(async s => {
    let closed = 0, launched = 0;
    const backend = {start: async () => { launched++; return {storageState: () => assert.fail()}; },
      stop: async () => { closed++; }};
    const supervisor = new LoginSupervisor({store: s.store, scheduler: s, origin, backend, verify: async () => false});
    await supervisor.start();
    assert.equal((await supervisor.finalize()).status, 'login_required');
    assert.equal(s.store.read('session.json'), null);
    assert.equal(s.store.read('console.secret'), null);
    backend.start = async () => { throw Error('SYNTHETIC'); };
    assert.equal((await supervisor.start()).status, 'login_unavailable');
    assert.equal(s.store.read('console.secret'), null);
    fs.chmodSync(s.store.directory, 0o755);
    try { await assert.rejects(() => supervisor.start()); }
    finally { fs.chmodSync(s.store.directory, 0o700); }
    assert.equal(launched, 1); assert.equal(closed, 2);
    return {ok: true};
  });
  assert.equal(value.ok, true);
});

test('client validates options and sanitizes unavailable IPC with synthetic preparation', async () => {
  const original = PrivateStore.prototype.prepare;
  PrivateStore.prototype.prepare = () => {}; // No real enrollment state access.
  const invoke = (action, options) => command(action, options, {connect: () => {
    const socket = new (require('node:events').EventEmitter)();
    socket.setTimeout = () => {}; socket.destroy = () => {};
    queueMicrotask(() => socket.emit('error', Error('fixture unavailable')));
    return socket;
  }});
  try {
    for (const action of ['start', 'status', 'stop', 'finalize']) {
      assert.deepEqual(await invoke(action), {ok: false, status: 'login_not_ready'});
      for (const options of [{host: '0.0.0.0'}, {password: 'SYNTHETIC'}, {url: 'SYNTHETIC'}, []])
        assert.deepEqual(await invoke(action, options), {ok: false, status: 'invalid_action'});
    }
    for (const ttl_minutes of [0, 6, true, null, '5', 1.5])
      assert.equal((await invoke('start', {ttl_minutes})).status, 'invalid_action');
    for (const ttl_minutes of [1, 5])
      assert.equal((await invoke('start', {ttl_minutes})).status, 'login_not_ready');
    assert.equal((await invoke('finalize', {ttl_minutes: 1})).status, 'invalid_action');
  } finally { PrivateStore.prototype.prepare = original; }
});
