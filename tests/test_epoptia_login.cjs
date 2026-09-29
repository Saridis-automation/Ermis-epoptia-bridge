'use strict';
const {Backend} = require('./login_test_loader.cjs');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const policy = require('../epoptia_login_policy.cjs');
const {commands} = require('../epoptia_login_backend.cjs');
const {Controller} = require('../epoptia_login_daemon.cjs');
const {PrivateStore} = require('../epoptia_browser_session.cjs');
const {scheduled} = require('./browser_policy_fixture.cjs');
test('exact origin, scheme, explicit port and off-origin redirect rejection', () => {
  assert.equal(policy.origin, 'https://app.epoptia.com');
  assert.ok(policy.sameOrigin(policy.origin + '/dashboard'));
  for (const url of ['http://app.epoptia.com/dashboard', 'https://app.epoptia.com:443/dashboard',
    'https://app.epoptia.com:444/dashboard', 'https://app.epoptia.com.evil/dashboard',
    'https://other.invalid/dashboard', 'https://user@app.epoptia.com/dashboard']) assert.equal(policy.sameOrigin(url), false);
  for (const target of ['//evil.invalid/x', 'http://app.epoptia.com/x', 'https://other.invalid/'])
    assert.equal(policy.sameOrigin(new URL(target, policy.origin).href), false);
});
test('finalize needs authenticated path and unique visible shell, rechecks URL', async () => {
  let url = policy.origin + '/dashboard', visible = true, count = 1, password = false;
  const surface = {page: {url: () => url, locator: selector => ({
    count: async () => count, isVisible: async () => selector.includes('password') ? password : visible,
  })}};
  assert.equal(await policy.authenticated(surface), true);
  for (const value of ['/login', '/', '/dashboard/other']) {
    url = policy.origin + value; assert.equal(await policy.authenticated(surface), false);
  }
  url = policy.origin + '/dashboard'; visible = false;
  assert.equal(await policy.authenticated(surface), false);
  visible = true; count = 2; assert.equal(await policy.authenticated(surface), false);
  count = 1; password = true; assert.equal(await policy.authenticated(surface), false);
  password = false; surface.denied = true; assert.equal(await policy.authenticated(surface), false);
});
test('fixed display and VNC transports have no public bind or TCP display', async () => {
  const plans = commands('/synthetic');
  assert.ok(plans[0][1].includes('-auth')); assert.ok(!plans[0][1].includes('-ac'));
  assert.deepEqual(plans[0][1].slice(4, 6), ['-nolisten', 'tcp']);
  assert.equal(plans[1][1][plans[1][1].indexOf('-listen') + 1], '127.0.0.1');
  assert.deepEqual(plans[2][1].slice(-2), ['127.0.0.1:6091', '127.0.0.1:5991']);
  await assert.rejects(new Backend().start({host: '0.0.0.0'}));
});
test('persistent controller serializes start and releases scheduler on stop', async () => {
  const value = await scheduled(async s => {
    let closed = 0;
    const backend = {start: async () => ({}), stop: async () => { closed++; }};
    // Own a synthetic scheduler without taking a second filesystem lock.
    const scheduler = {run: async fn => fn(s)};
    const controller = new Controller({store: s.store, backend, scheduler});
    assert.equal((await controller.command('start', {ttl_minutes: 1})).status, 'awaiting_login');
    assert.equal((await controller.command('start')).status, 'busy');
    assert.equal((await controller.command('status')).status, 'awaiting_login');
    assert.equal((await controller.command('stop')).status, 'stopped');
    assert.equal(closed, 1); assert.equal(controller.supervisor, null);
    assert.equal(s.store.read('console.secret'), null);
    assert.equal((await controller.command('start', {host: 'SYNTHETIC'})).status, 'invalid_action');
    return {ok: true};
  }); assert.equal(value.ok, true);
});
test('redirect validation preserves explicit authority before URL normalization', () => {
  for (const location of ['https://app.epoptia.com:443/dashboard', '//app.epoptia.com:443/dashboard', '//evil.invalid/x'])
    assert.equal(policy.redirectAllowed(location, policy.origin + '/login'), false);
  assert.equal(policy.redirectAllowed('/dashboard', policy.origin + '/login'), true);
});
test('session promotion is staged and leaves no pending artifact', () => {
  const fs = require('node:fs'), path = require('node:path');
  const directory = fs.mkdtempSync(path.join(__dirname, '.login-session-'));
  fs.chmodSync(directory, 0o700);
  const store = new PrivateStore(directory);
  store.prepare = () => {}; // Fixture parent is a collaborative checkout (0775).
  try {
    store.saveSession({cookies: [{name: 'session', value: 'fixture', domain: 'app.epoptia.com',
      path: '/', expires: -1, httpOnly: true, secure: true, sameSite: 'Lax'}], origins: []},
      policy.origin, Date.now() + 60000);
    assert.equal(fs.existsSync(path.join(directory, 'session.pending.json')), false);
    assert.equal(store.session(policy.origin).state.cookies.length, 1);
  } finally { fs.rmSync(directory, {recursive: true}); }
});
test('backend request gate blocks redirects before follow and cleanup owns only synthetic children', async () => {
  const fs = require('node:fs'), path = require('node:path');
  const {EventEmitter} = require('node:events');
  const directory = fs.mkdtempSync(path.join(__dirname, '.login-backend-'));
  const exists = fs.existsSync;
  let handler, closed = 0, killed = 0, parentTemp;
  const bounded = options => {
    for (const key of ['HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
      'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR', 'XAUTHORITY']) {
      assert.ok(options.env[key].startsWith(directory + '/surface-'));
      assert.ok(fs.existsSync(options.env[key]));
    }
    assert.deepEqual(Object.keys(options.env).sort(), ['PATH', 'HOME', 'DISPLAY', 'XAUTHORITY',
      'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'].sort());
    assert.equal(parentTemp, directory);
  };
  const page = {mainFrame: () => page, goto: async () => {}};
  const context = {pages: () => [page], route: async (_, fn) => { handler = fn; },
    routeWebSocket: async () => {}, on: () => {}, close: async () => { closed++; }};
  const backend = new Backend({resolveHome: directory => directory, setTemp: root => { parentTemp = root; }, directory, health: async () => {}, resolve: () => ({executablePath: '/synthetic'}),
    chromium: {launchPersistentContext: async (profile, options) => {
      bounded(options);
      assert.equal(profile, path.join(options.env.HOME, 'profile'));
      assert.ok(options.args.every(arg => !/no-sandbox|remote-debugging|disable-setuid-sandbox/.test(arg)));
      assert.equal(options.chromiumSandbox, true); assert.equal(options.serviceWorkers, 'block'); return context;
    }}, launch: (_, args, options) => {
      bounded(options);
      assert.equal(options.stdio, 'ignore'); assert.ok(!args.includes('SYNTHETIC'));
      const child = new EventEmitter(); child.pid = 123; child.exitCode = null;
      child.kill = () => { killed++; child.exitCode = 0; queueMicrotask(() => child.emit('exit')); };
      queueMicrotask(() => child.emit('spawn')); return child;
    }});
  fs.existsSync = p => p === '/tmp/.X11-unix/X91' || exists(p);
  try {
    await backend.start({host: '127.0.0.1'});
    let fetched = 0, aborted = 0, fulfilled = 0;
    const route = {request: () => ({url: () => policy.origin + '/login', isNavigationRequest: () => true, frame: () => page}),
      fetch: async options => { fetched++; assert.equal(options.maxRedirects, 0);
        return {headers: () => ({location: 'https://other.invalid/SYNTHETIC'}), status: () => 302}; },
      abort: async () => { aborted++; }, fulfill: async () => { fulfilled++; }};
    await handler(route);
    assert.equal(fetched, 1); assert.equal(aborted, 1); assert.equal(fulfilled, 0);
    assert.equal(backend.surface.denied, true);
    await handler({...route, request: () => ({url: () => 'https://other.invalid/'})});
    assert.equal(fetched, 1); assert.equal(aborted, 2);
    await backend.stop();
    assert.equal(closed, 1); assert.equal(killed, 3);
    assert.deepEqual(fs.readdirSync(directory), []);
    backend.health = async () => { throw Error('fixture readiness failure'); };
    await assert.rejects(backend.start({host: '127.0.0.1'}), /backend_unavailable/);
    assert.equal(closed, 2); assert.equal(killed, 6);
    assert.deepEqual(fs.readdirSync(directory), []);
  } finally { fs.existsSync = exists; await backend.stop(); fs.rmdirSync(directory); }
});

const {resolveManagedHome, homeEnvironment, MANAGED_HOME} = require('../epoptia_login_backend.cjs');
test('managed home resolver accepts only the exact runtime root and refuses launch without fallback', async () => {
  assert.equal(resolveManagedHome(MANAGED_HOME), MANAGED_HOME);
  for (const value of Object.values(homeEnvironment(MANAGED_HOME))) assert.equal(value, MANAGED_HOME);
  for (const value of [undefined, null, '', '/tmp', '/home/ermis', '.', MANAGED_HOME + '/',
    MANAGED_HOME + '/child', MANAGED_HOME + '/../enrollment', MANAGED_HOME + '\n', {}]) {
    assert.throws(() => resolveManagedHome(value), /^Error: managed_home_invalid$/);
    const backend = new Backend({resolveHome: () => resolveManagedHome(value),
      setTemp: () => assert.fail('temp configuration before validation'),
      launch: () => assert.fail('helper launched'),
      resolve: () => assert.fail('browser resolver called'),
      chromium: {launchPersistentContext: () => assert.fail('browser launched')}});
    await assert.rejects(backend.start({host: '127.0.0.1'}), /^Error: backend_unavailable$/);
    assert.equal(backend.diagnose().failure_class, 'managed_home_invalid');
    assert.equal(backend.diagnose().failure_stage, 'prepare');
    assert.equal(backend.diagnose().child_launch_attempted, false);
  }
  assert.throws(() => resolveManagedHome(MANAGED_HOME, '/tmp'), /managed_home_invalid/);
});
