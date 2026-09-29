'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const aa = require('../epoptia_login_apparmor.cjs');
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
function fixture() {
  const files = new Map();
  const info = (directory, mode = directory ? 0o40755 : 0o100644) => ({uid: 0, gid: 0, nlink: 1, mode,
    dev: 1, ino: 2, mtimeMs: 1, ctimeMs: 1, size: 0,
    isFile: () => !directory, isDirectory: () => directory, isSymbolicLink: () => false});
  const add = (p, data, mode) => {
    const attributes = info(data === null, mode);
    files.set(p, {attributes, data: data === null ? null : Buffer.from(data)});
  };
  add(aa.ROOT, null);
  add(aa.ROOT + '/chrome-linux64', null);
  add(aa.EXECUTABLE, '\x7fELFsynthetic', 0o100755);
  add(aa.ROOT + '/chrome-linux64/resources.pak', 'resources');
  const stat = p => {
    const f = files.get(p);
    return f ? {...f.attributes, size: f.data?.length || 0} : info(true);
  };
  const io = {
    lstatSync: stat, fstatSync: stat, realpathSync: p => p,
    openSync: p => p, closeSync: () => {},
    readFileSync: p => files.get(p).data,
    readdirSync: p => [...files.keys()].filter(k => k.startsWith(p + '/') && !k.slice(p.length + 1).includes('/')).map(k => k.slice(p.length + 1)),
  };
  const options = {executable: aa.EXECUTABLE, capabilities: () => {}, admission: () => {}};
  const env = {};
  Object.defineProperty(env, 'EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE', {get: () => options.executable});
  const isolated = require('./login_test_loader.cjs').load('epoptia_login_apparmor.cjs', {
    'node:fs': Object.assign(io, {constants: require('node:fs').constants}),
    'node:child_process': {execFileSync: (command, args, config) => {
      if (args.at(-1) === 'ready-check') {
        assert.equal(command, '/usr/bin/python3');
        assert.deepEqual(args, ['-I', '-B', '/home/ermis/projects/epoptia-bridge/admin_bootstrap/login_bootstrap.py', 'ready-check']);
        assert.equal(config.stdio, 'ignore');
        return options.admission();
      }
      return options.capabilities();
    }},
  }, env);
  const chromium = {executablePath() { throw Error('package fallback forbidden'); }};
  const current = isolated.identity(chromium, io, 1000, options);
  add(aa.PROFILE, aa.profile(current));
  const policyIdentity = aa.profile(current).match(/\nprofile "([^"]+)"/)[1];
  add(aa.RECEIPT, JSON.stringify({...current, profile_sha256: hash(aa.profile(current)),
    policy_identity: policyIdentity, policy_sha256: policyIdentity.split('-').at(-1)}));
  return {files, io, options, chromium, add, current, verify: () => isolated.verify(), isolated, env};
}
test('exact version-pinned literal profile permits only userns', () => {
  const f = fixture();
  assert.equal(aa.profile(f.current).split('{\n')[1], '  userns,\n}\n');
  assert.ok(aa.profile(f.current).includes(`profile "ermis-epoptia-login-chromium-1243-${hash(`abi <abi/4.0>,\nattachment "${aa.EXECUTABLE}" flags=(unconfined) {\n  userns,\n}\n`)}" "${aa.EXECUTABLE}" flags=(unconfined)`));
  for (const executable of [aa.EXECUTABLE + '\n', aa.EXECUTABLE + '"', aa.EXECUTABLE.replace('1243', '*'), '/home/ermis/.cache/chrome'])
    assert.throws(() => aa.profile({...f.current, executable}));
  assert.deepEqual(f.verify(), {executablePath: aa.EXECUTABLE});
});
test('explicit selection has no package cache or PATH fallback', () => {
  for (const executable of ['', '/root/.cache/chrome', '/home/ermis/.cache/chrome', aa.EXECUTABLE.replace('1243', '1242'), aa.EXECUTABLE + '\n', aa.EXECUTABLE.replace('chrome-linux64', 'chrome-linux')]) {
    const f = fixture(); f.options.executable = executable;
    assert.throws(f.verify, {code: 'B_LOGIN_APPARMOR'});
  }
});
test('full tree, owner, mode, receipt, capability and symlink drift fail closed', () => {
  for (const mutate of [
    f => { f.files.get(aa.EXECUTABLE).data[0] ^= 1; },
    f => { f.files.get(aa.ROOT + '/chrome-linux64/resources.pak').data = Buffer.from('drift'); },
    f => { f.files.get(aa.EXECUTABLE).attributes.uid = 1000; },
    f => { f.files.get(aa.EXECUTABLE).attributes.mode = 0o104755; },
    f => { f.files.get(aa.EXECUTABLE).attributes.mode = 0o102755; },
    f => { f.files.get(aa.EXECUTABLE).attributes.mode = 0o100644; },
    f => { f.files.get(aa.EXECUTABLE).attributes.nlink = 2; },
    f => { f.files.get(aa.ROOT).attributes.uid = 1000; },
    f => { f.files.get(aa.ROOT).attributes.mode = 0o40777; },
    f => { f.files.get(aa.ROOT).attributes.isSymbolicLink = () => true; },
    f => { f.io.realpathSync = () => '/elsewhere'; },
    f => { f.files.get(aa.RECEIPT).data = Buffer.from('{}'); },
    f => { f.files.get(aa.PROFILE).data = Buffer.from('network,'); },
    f => { f.options.capabilities = () => { throw Error(); }; },
    f => { f.add(aa.ROOT + '/bad\nname', 'fixture'); },
    f => { f.files.get(aa.EXECUTABLE).attributes.isFile = () => false; },
  ]) {
    const f = fixture(); mutate(f);
    assert.throws(f.verify, {code: 'B_LOGIN_APPARMOR', message: 'B_LOGIN_APPARMOR'});
  }
});
test('backend and probe diagnostics retain only bounded blocker', () => {
  const backend = require('../epoptia_login_backend.cjs');
  assert.equal(backend.classifyFailure({code: 'B_LOGIN_APPARMOR', message: 'private'}), 'B_LOGIN_APPARMOR');
  assert.equal(require('../epoptia_login_sandbox.cjs').sanitizeProbe({failure_class: 'B_LOGIN_APPARMOR'}).failure_class, 'B_LOGIN_APPARMOR');
  assert.equal(new backend.Backend().resolve, undefined);
});

test('login, offline backend, probe parent and shared runtime reject invalid selection with zero children', async () => {
  const {load} = require('./login_test_loader.cjs');
  const values = [undefined, '', 'chrome', './chrome', '/usr/bin/chromium',
    '/home/ermis/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',
    aa.EXECUTABLE.replace('1243', '1242'), aa.EXECUTABLE.replace('chrome-linux64', 'chrome-linux'),
    aa.EXECUTABLE + '/', aa.EXECUTABLE + '\n'];
  for (const value of values) {
    let spawned = 0, discovered = 0, reads = 0;
    const chromium = {executablePath() { discovered++; return '/tempting/cache/chrome'; },
      launch() { spawned++; }, launchPersistentContext() { spawned++; }};
    const env = {PATH: '/tempting/bin', PLAYWRIGHT_BROWSERS_PATH: '/tempting/cache',
      HOME: '/tempting/home', EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE: value};
    const verify = load('epoptia_login_apparmor.cjs', {
      'node:fs': new Proxy({}, {get() { reads++; throw Error('unexpected IO'); }}),
      'node:child_process': {execFileSync() { spawned++; }},
    }, env);
    // Public production verify cannot accept an executable/io override.
    assert.throws(() => verify.verify(chromium, {}, 1000, {executable: aa.EXECUTABLE}), {code: 'B_LOGIN_APPARMOR'});
    const overrides = {'./epoptia_login_apparmor.cjs': verify, playwright: {chromium}};
    const {Backend} = load('epoptia_login_backend.cjs', overrides, env);
    for (const offline of [false, true]) {
      const backend = new Backend({resolveHome: () => '/unused', launch: () => { spawned++; },
        resolve: () => ({executablePath: '/tempting/override'}), chromium});
      await assert.rejects(backend.start({host: '127.0.0.1', offline}), /backend_unavailable/);
      assert.equal(backend.diagnose().failure_class, 'B_LOGIN_APPARMOR');
      assert.equal(backend.diagnose().child_launch_attempted, false);
    }
    const probe = load('epoptia_login_sandbox.cjs', overrides, env);
    const result = await new probe.SandboxProbe({launch: () => { spawned++; }}).run({});
    assert.equal(result.sandbox_probe.failure_class, 'B_LOGIN_APPARMOR');
    const shared = load('epoptia_browser_runtime.cjs', overrides, env);
    assert.throws(() => shared.resolveExecutable(chromium), {code: 'B_LOGIN_APPARMOR'});
    assert.equal((await shared.launchBrowser()).status, 'B_LOGIN_APPARMOR');
    assert.equal(spawned, 0); assert.equal(discovered, 0); assert.equal(reads, 0);
  }
});

test('exact receipt-bound /opt identity is shared by login and probe; drift blocks both before spawn', async () => {
  const {load} = require('./login_test_loader.cjs');
  for (const drift of [false, true]) {
    const f = fixture();
    if (drift) f.files.get(aa.ROOT + '/chrome-linux64/resources.pak').data = Buffer.from('changed');
    let reached = 0, spawns = 0;
    const overrides = {'./epoptia_login_apparmor.cjs': f.isolated,
      'node:fs': {lstatSync() { reached++; throw Error('fixture boundary after accepted preflight'); }}};
    const {Backend} = load('epoptia_login_backend.cjs', overrides);
    for (const offline of [false, true]) {
      const backend = new Backend({resolveHome: () => '/fixture', launch: () => { spawns++; }});
      await assert.rejects(backend.start({host: '127.0.0.1', offline}));
      assert.equal(backend.diagnose().failure_class, drift ? 'B_LOGIN_APPARMOR' : 'unknown');
    }
    assert.equal(reached, drift ? 0 : 2); assert.equal(spawns, 0);
    const shared = load('epoptia_browser_runtime.cjs', overrides);
    if (drift) assert.throws(() => shared.resolveExecutable(f.chromium), {code: 'B_LOGIN_APPARMOR'});
    else assert.deepEqual(shared.resolveExecutable(f.chromium), {executablePath: aa.EXECUTABLE});
  }
});

test('installed Playwright discovery and launch methods are never used for absent selection', async t => {
  const chromium = require('playwright').chromium;
  let discovered = 0, spawned = 0;
  t.mock.method(chromium, 'executablePath', () => { discovered++; return '/tempting/cache/chrome'; });
  t.mock.method(chromium, 'launch', () => { spawned++; });
  t.mock.method(chromium, 'launchPersistentContext', () => { spawned++; });
  const {load} = require('./login_test_loader.cjs');
  const env = {PATH: '/tempting/bin', PLAYWRIGHT_BROWSERS_PATH: '/tempting/cache'};
  const validator = load('epoptia_login_apparmor.cjs', {}, env);
  const overrides = {'./epoptia_login_apparmor.cjs': validator};
  const shared = load('epoptia_browser_runtime.cjs', overrides, env);
  assert.equal((await shared.launchBrowser()).status, 'B_LOGIN_APPARMOR');
  const {Backend} = load('epoptia_login_backend.cjs', overrides, env);
  for (const offline of [false, true]) {
    const backend = new Backend({chromium, resolveHome: () => '/unused', launch: () => { spawned++; }});
    await assert.rejects(backend.start({host: '127.0.0.1', offline}));
    assert.equal(backend.diagnose().failure_class, 'B_LOGIN_APPARMOR');
  }
  assert.equal(discovered, 0); assert.equal(spawned, 0);
});


test('runtime rejects missing, stale or locked final readiness before returning executable', () => {
  for (const reason of ['missing', 'source-drift', 'receipt-drift', 'installer-locked']) {
    const f = fixture();
    let checked = 0;
    f.options.admission = () => { checked++; throw Error(reason); };
    assert.throws(f.verify, {code: 'B_LOGIN_APPARMOR', message: 'B_LOGIN_APPARMOR'});
    assert.equal(checked, 1);
  }
});
