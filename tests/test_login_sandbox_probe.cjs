'use strict';
const {Backend} = require('./login_test_loader.cjs');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const sandbox = require('./login_test_loader.cjs').load('epoptia_login_sandbox.cjs', {
  './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: '/SYNTHETIC'})},
});
const {homeEnvironment} = require('../epoptia_login_backend.cjs');
const {Controller, controlServer, SOCKET} = require('./login_test_loader.cjs').load('epoptia_login_daemon.cjs', {
  './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: '/SYNTHETIC'})},
});
const {command} = require('../epoptia_browser_login.cjs');

const fixture = () => fs.mkdtempSync(path.join(__dirname, '.sandbox-probe-'));
const remove = directory => fs.rmSync(directory, {recursive: true, force: true});

// Execute the real guarded cleanup routine with synthetic scheduling for lifecycle
// tests; dedicated tests below exercise actual worker threads without a browser.
class FixtureProbe extends sandbox.SandboxProbe {
  constructor(options = {}) {
    super({cleanupLaunch: identity => {
      const worker = new EventEmitter();
      worker.unref = () => {};
      worker.terminate = async () => {};
      queueMicrotask(() => {
        worker.emit('message', sandbox.cleanupOwned(identity, options.files || fs, options.now));
        worker.emit('exit', 0);
      });
      return worker;
    }, ...options});
  }
}

test('stderr stream never emits raw data, returns only enums, and clears each buffer', () => {
  const stream = new PassThrough();
  const found = [];
  sandbox.discardStderr(stream, value => found.push(value));
  stream.on('data', () => assert.fail('raw stderr escaped'));
  for (const [line, category] of [
    ['apparmor userns denied', 'userns_lsm_denied'],
    ['RestrictNamespaces denied', 'unit_namespace_denied'],
    ['unprivileged_userns_clone = 0', 'kernel_userns_disabled'],
    ['seccomp clone denied', 'seccomp_clone_denied'],
    ['setuid helper not configured', 'setuid_helper_unusable'],
    ['Running as root without sandbox', 'root_identity_refused'],
    ['proc namespace incompatible', 'proc_namespace_incompatible'],
    ['profile Permission denied', 'runtime_fs_denied'],
    ['No usable sandbox', 'sandbox_check_other'],
  ]) {
    const chunk = Buffer.from(line + ' PRIVATE');
    stream.emit('data', chunk);
    assert.equal(found.at(-1), category);
    assert.ok(chunk.every(byte => byte === 0));
  }
  assert.equal(sandbox.classifySandbox('Operation not permitted PRIVATE'), 'unknown');
  stream.emit('data', Buffer.alloc(1024 * 1024, 65));
  for (const value of [null, [], {stderr: 'PRIVATE', sandbox_class: 'PRIVATE', ready: 'PRIVATE'}])
    assert.ok(!JSON.stringify(sandbox.sanitizeProbe(value)).includes('PRIVATE'));
  for (const [key, values] of Object.entries(sandbox.ENUMS)) {
    for (const value of values) assert.equal(sandbox.sanitizeProbe({[key]: value})[key], value);
    for (const value of ['PRIVATE', {}, [], null, 123]) assert.equal(sandbox.sanitizeProbe({[key]: value})[key], key === 'cleanup_class' ? 'none' : 'unknown');
  }
});

test('same backend launch envelope is offline and cannot reach login or auth state', async () => {
  const root = fixture(), directory = fs.mkdtempSync(path.join(root, 'sandbox-'));
  const originalExists = fs.existsSync;
  const page = new EventEmitter();
  page.goto = async target => assert.equal(target, 'about:blank');
  const context = new EventEmitter();
  let blocked = 0, wsBlocked = 0, closed = 0, launches = 0;
  Object.assign(context, {pages: () => [page], close: async () => { closed++; },
    route: async (pattern, handler) => { assert.equal(pattern, '**/*'); await handler({abort: async () => { blocked++; }}); },
    routeWebSocket: async (pattern, handler) => { assert.equal(pattern, '**/*'); handler({close: () => { wsBlocked++; }}); },
    cookies: () => assert.fail('auth state access')});
  const backend = new Backend({directory: root, resolveHome: d => d, setTemp: temp => assert.equal(temp, directory),
    resolve: () => ({executablePath: '/SYNTHETIC'}), health: () => assert.fail('network health probe'),
    launch: (bin, args, options) => {
      launches++; assert.equal(bin, '/usr/bin/Xvfb');
      assert.ok(args.includes('-nolisten')); assert.ok(args.includes('tcp'));
      assert.equal(options.env.HOME, directory);
      const child = new EventEmitter(); child.exitCode = null;
      queueMicrotask(() => child.emit('spawn')); return child;
    }, chromium: {launchPersistentContext: async (profile, options) => {
      assert.equal(profile, path.join(directory, 'profile'));
      assert.equal(options.executablePath, '/SYNTHETIC');
      assert.equal(options.chromiumSandbox, true); assert.equal(options.headless, false);
      assert.equal(options.offline, true); assert.equal(options.serviceWorkers, 'block');
      for (const [key, value] of Object.entries(homeEnvironment(directory))) assert.equal(options.env[key], value);
      for (const flag of sandbox.OFFLINE_ARGS) assert.ok(options.args.includes(flag));
      assert.ok(options.args.includes('--disable-background-networking'));
      assert.ok(!options.args.some(flag => /no-sandbox|disable.*sandbox/.test(flag)));
      return context;
    }}});
  fs.existsSync = name => name === '/tmp/.X11-unix/X91' || originalExists(name);
  try {
    await backend.start({host: '127.0.0.1', offline: true, probeDirectory: directory});
    assert.equal(launches, 1); assert.equal(blocked, 1); assert.equal(wsBlocked, 1);
    assert.deepEqual(fs.readdirSync(directory), ['Xauthority']);
    await backend.stop(); assert.ok(closed); assert.ok(fs.existsSync(directory)); // Parent retains sole deletion ownership.
  } finally { fs.existsSync = originalExists; remove(root); }
});

test('controller/socket exclusivity bypasses session scheduler and sanitizes both IPC boundaries', async () => {
  let finish;
  const forbidden = new Proxy({}, {get() { assert.fail('session artifact access'); }});
  const controller = new Controller({store: forbidden, scheduler: forbidden, backend: {diagnose: () => ({})},
    probe: {run: () => new Promise(resolve => { finish = resolve; })}});
  const running = controller.command('sandbox_probe');
  assert.equal((await controller.command('sandbox_probe')).status, 'busy');
  assert.equal((await controller.command('start')).status, 'busy');
  finish({ok: true, status: 'sandbox_probe_complete', sandbox_probe: {ready: true, sandbox_class: 'none', stderr: 'PRIVATE'}});
  await running;
  assert.equal((await controller.command('diagnose')).sandbox_probe.sandbox_class, 'none');
  controller.supervisor = {};
  assert.equal((await controller.command('sandbox_probe')).status, 'busy');
  controller.supervisor = null;
  assert.equal((await controller.command('sandbox_probe', {url: 'PRIVATE'})).status, 'invalid_action');
  class Wire extends EventEmitter {setTimeout() {} pause() {} destroy() {} }
  const revision = 'a'.repeat(64), client = new Wire(), daemon = new Wire();
  const server = controlServer({command: async action => {
    assert.equal(action, 'sandbox_probe');
    return {ok: true, status: 'sandbox_probe_complete', sandbox_probe: {sandbox_class: 'none', stderr: 'PRIVATE'}};
  }}, revision, () => revision);
  server.emit('connection', daemon);
  daemon.end = (data, done) => { done?.(); assert.ok(!data.includes('PRIVATE')); client.emit('data', data); client.emit('end'); };
  client.end = data => daemon.emit('data', data);
  const result = command('sandbox_probe', {}, {prepare: () => assert.fail('auth prepare'), connect: socket => {
    assert.equal(socket, SOCKET); return client;
  }});
  client.emit('connect');
  assert.equal((await result).sandbox_probe.sandbox_class, 'none');
  server.close();
});

for (const mode of ['success', 'timeout', 'exit', 'error', 'abort', 'apparmor']) {
  test(`worker boundary cleanup and deadline: ${mode}`, async () => {
    const root = fixture();
    let child, deadline, delay, clear = 0, killed = 0, clock = 0;
    const abort = new AbortController();
    const probe = new FixtureProbe({now: () => clock,
      timers: {setTimeout: (fn, ms) => { deadline = fn; delay = ms; return 1; }, clearTimeout: () => { clear++; }},
      kill: (pid, signal) => { assert.equal(pid, 123); assert.equal(signal, 'SIGKILL'); killed++; },
      launch: (file, args, options) => {
        assert.equal(path.basename(file), 'epoptia_login_sandbox_worker.cjs');
        assert.equal(path.dirname(args[0]), root); assert.ok(fs.existsSync(args[0]));
        assert.equal(options.detached, true); assert.deepEqual(options.execArgv, []);
        assert.deepEqual(options.stdio, ['ignore', 'ignore', 'ignore', 'ipc']);
        assert.deepEqual(options.env, {PATH: '/usr/bin:/bin', ...homeEnvironment(root), EPOPTIA_LOGIN_BROWSER_HOME: root, EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE: process.env.EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE});
        child = new EventEmitter(); child.pid = 123; child.disconnect = () => {};
        return child;
      }});
    try {
      const running = probe.run({directory: root, resolveHome: d => d}, abort.signal);
      assert.equal((await probe.run({})).status, 'busy');
      assert.equal(delay, 17500); assert.equal(sandbox.DWELL_MS, 10000);
      child.emit('message', {spawn_returned: true, ready: true, sandbox_class: 'none', stderr: 'PRIVATE'});
      if (mode === 'timeout') { clock = 17500; deadline(); }
      else if (mode === 'abort') abort.abort();
      else if (mode === 'apparmor') child.emit('message', {ready: false, failure_class: 'B_LOGIN_APPARMOR', done: true});
      else if (mode === 'success') { clock = 11000; child.emit('message', {ready: true, clean_close: true, done: true, stderr: 'PRIVATE'}); }
      else child.emit(mode, Error('PRIVATE'));
      child?.emit('close');
      const result = await running;
      assert.equal(result.sandbox_probe.timed_out, mode === 'timeout');
      if (mode === 'apparmor') assert.equal(result.sandbox_probe.failure_class, 'B_LOGIN_APPARMOR');
      assert.equal(result.sandbox_probe.command_accepted, true);
      assert.equal(killed, 1); assert.ok(clear >= 1);
      assert.deepEqual(fs.readdirSync(root), []);
      assert.ok(!JSON.stringify(result).includes('PRIVATE'));
      assert.equal(probe.busy, false);
    } finally { remove(root); }
  });
}

test('primitive is fixed, non-networking and classifies syscall exit only', async () => {
  for (const [code, category] of [[0, 'success'], [2, 'permission_denied'], [3, 'unavailable'], [4, 'unknown']]) {
    const result = await sandbox.primitive((bin, args, options) => {
      assert.equal(bin, '/usr/bin/python3'); assert.deepEqual(args.slice(0, 4), ['-I', '-S', '-B', '-c']);
      assert.ok(args[4].includes('unshare(0x10000000)'));
      assert.ok(!/socket|open\(|exec\(|subprocess/.test(args[4]));
      assert.equal(options.stdio, 'ignore'); assert.equal(options.detached, false);
      const child = new EventEmitter(); queueMicrotask(() => child.emit('exit', code)); return child;
    }, {});
    assert.equal(result, category);
  }
});

test('facts only read bounded nonsecret fixed files and report enums', () => {
  const paths = [];
  const files = {openSync: name => { paths.push(name); return name; }, closeSync: () => {},
    readSync: (fd, buffer) => {
      assert.equal(buffer.length, 4096);
      const value = fd.endsWith('current') ? 'PRIVATE (enforce)' : fd.endsWith('max_user_namespaces') ? '100' : '1';
      buffer.write(value); return value.length;
    }, lstatSync: name => { assert.equal(name, '/SYNTHETIC/chrome-sandbox'); return {uid: 0, mode: 0o104755, isFile: () => true, isSymbolicLink: () => false}; }};
  const facts = sandbox.effectiveFacts('/SYNTHETIC/chrome', files, 1000);
  assert.deepEqual(facts, {nonroot: true, apparmor_restriction: 'enabled', kernel_userns: 'enabled', apparmor_confinement: 'confined', setuid_helper: 'valid'});
  assert.equal(paths.length, 4); assert.ok(paths.every(name => name.startsWith('/proc/')));
  assert.ok(!JSON.stringify(facts).includes('PRIVATE'));
});

test('full synthetic worker intercepts stderr before Playwright, dwells, then closes without auth imports', async () => {
  const vm = require('node:vm');
  const root = fixture(), directory = fs.mkdtempSync(path.join(root, 'sandbox-'));
  const originalExists = fs.existsSync, messages = [], waits = [];
  let browserChild, interceptInstalled = false;
  const page = new EventEmitter();
  page.goto = async target => assert.equal(target, 'about:blank');
  page.isClosed = () => false;
  const context = new EventEmitter();
  Object.assign(context, {pages: () => [page], route: async () => {}, routeWebSocket: async () => {},
    close: async () => { browserChild.emit('exit', 0, null); }});
  const originalSpawn = (bin, args, options) => {
    assert.equal(options.detached, false);
    const child = new EventEmitter(); child.exitCode = null;
    if (bin === '/SYNTHETIC') {
      child.stderr = new PassThrough(); browserChild = child;
      queueMicrotask(() => {
        child.stderr.on('data', () => assert.fail('Playwright collected raw stderr'));
        child.stderr.emit('data', Buffer.from('PRIVATE unknown text'));
      });
    } else assert.equal(bin, '/usr/bin/Xvfb');
    queueMicrotask(() => child.emit('spawn'));
    return child;
  };
  const cp = {spawn: originalSpawn};
  const chromium = {launchPersistentContext: async () => {
    assert.ok(interceptInstalled);
    cp.spawn('/SYNTHETIC', [], {detached: true});
    return context;
  }};
  const backendModule = require('../epoptia_login_backend.cjs');
  class FixtureBackend extends Backend {
    constructor() { super({directory: root, resolveHome: d => d, setTemp: () => {}, chromium,
      launch: (...args) => cp.spawn(...args), resolve: () => ({executablePath: '/SYNTHETIC'})}); }
  }
  const module = {exports: {}};
  const imports = [];
  const mockRequire = name => {
    imports.push(name);
    if (name === 'node:child_process') return cp;
    if (name === './epoptia_login_sandbox.cjs') return {...sandbox,
      effectiveFacts: () => ({nonroot: true}), primitive: async () => 'permission_denied'};
    if (name === 'playwright') {
      interceptInstalled = cp.spawn !== originalSpawn;
      return {chromium};
    }
    if (name === './epoptia_login_apparmor.cjs') return {verify: () => ({executablePath: '/SYNTHETIC'})};
    if (name === './epoptia_login_backend.cjs') return {...backendModule, Backend: FixtureBackend};
    assert.fail('unexpected import');
  };
  fs.existsSync = name => name === '/tmp/.X11-unix/X91' || originalExists(name);
  try {
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../epoptia_login_sandbox_worker.cjs'), 'utf8'), {
      module, require: mockRequire,
      process: {connected: true, argv: ['node', 'worker', directory], send: value => messages.push(value)},
      setTimeout: (fn, ms) => { waits.push(ms); queueMicrotask(fn); },
    });
    await module.exports.run();
    assert.deepEqual(waits, [10000]);
    assert.equal(messages.at(-1).done, true);
    assert.equal(messages.at(-1).sandbox_class, 'none');
    assert.equal(messages.at(-1).ready, true);
    assert.equal(messages.at(-1).clean_close, true);
    assert.equal(messages.at(-1).child_exit_class, 'clean');
    assert.equal(messages.at(-1).user_namespace, 'permission_denied');
    assert.ok(!JSON.stringify(messages).includes('PRIVATE'));
    assert.ok(!imports.some(name => /session|scheduler|supervisor/.test(name)));
    assert.ok(fs.existsSync(directory)); // Parent retains sole deletion ownership.
    assert.equal(cp.spawn, originalSpawn);
    assert.equal(page.listenerCount('crash'), 0);
    assert.equal(page.listenerCount('close'), 0);
    assert.equal(browserChild.listenerCount('exit'), 0);
    assert.equal(browserChild.listenerCount('error'), 0);
  } finally { fs.existsSync = originalExists; remove(root); }
});

test('invalid probe directory fails before launch and is never cleaned as owned', async () => {
  const root = fixture(), unrelated = fixture();
  fs.writeFileSync(path.join(unrelated, 'keep'), 'fixture');
  try {
    const backend = new Backend({directory: root, resolveHome: d => d, setTemp: () => {},
      launch: () => assert.fail('invalid directory launch')});
    for (const directory of [null, unrelated, path.join(root, '..')]) {
      await assert.rejects(backend.start({host: '127.0.0.1', offline: true, probeDirectory: directory}));
      assert.ok(fs.existsSync(path.join(unrelated, 'keep')));
    }
    const link = path.join(root, 'sandbox-link');
    fs.symlinkSync(unrelated, link);
    await assert.rejects(backend.start({host: '127.0.0.1', offline: true, probeDirectory: link}));
    assert.ok(fs.existsSync(path.join(unrelated, 'keep')));
  } finally { remove(root); remove(unrelated); }
});

test('failed group termination reports failure and releases the global guard', async () => {
  const root = fixture(); let child;
  const probe = new FixtureProbe({
    kill: () => { throw Object.assign(Error('PRIVATE'), {code: 'EPERM'}); },
    launch: () => { child = new EventEmitter(); child.pid = 123; return child; },
  });
  try {
    const running = probe.run({directory: root, resolveHome: d => d});
    child.emit('message', {ready: true, clean_close: true, done: true});
    child.emit('close');
    const result = await running;
    assert.equal(result.ok, false);
    assert.equal(result.sandbox_probe.failure_class, 'shutdown_failure');
    assert.equal(probe.busy, false);
  } finally { remove(root); }
});

for (const mode of ['clean', 'sigtrap', 'signal', 'nonzero', 'spawn_throw', 'spawn_error',
  'readiness_failure', 'timeout', 'cancel', 'disconnect', 'classifier_exception', 'kill_failure', 'cleanup_retry', 'cleanup_failure']) {
  test(`sole owner removes transient artifacts: ${mode}`, async () => {
    const root = fixture();
    const session = path.join(root, 'persisted-session');
    fs.mkdirSync(session, {mode: 0o700});
    fs.writeFileSync(path.join(session, 'keep'), 'synthetic');
    let child, directory;
    const clock = clockFixture();
    const abort = new AbortController();
    const files = Object.create(fs);
    let removals = 0;
    if (mode === 'cleanup_retry' || mode === 'cleanup_failure') files.rmdirSync = (...args) => {
      removals++;
      if (mode === 'cleanup_retry' && removals === 1) throw Error('PRIVATE');
      fs.rmdirSync(...args);
      if (mode === 'cleanup_failure') throw Error('PRIVATE');
    };
    const probe = new FixtureProbe({files, ...clock,
      kill: () => { if (mode === 'kill_failure') throw Error('PRIVATE'); },
      launch: (_file, args) => {
        directory = args[0];
        assert.equal(fs.statSync(directory).mode & 0o777, 0o700);
        fs.mkdirSync(path.join(directory, 'profile'));
        fs.writeFileSync(path.join(directory, 'profile', 'temporary'), 'synthetic');
        // Recursive removal must unlink this entry without touching its target.
        fs.symlinkSync(session, path.join(directory, 'session-link'));
        if (mode === 'spawn_throw') throw Error('PRIVATE');
        child = new EventEmitter(); child.pid = 123;
        return child;
      }});
    try {
      let running;
      if (mode === 'disconnect') {
        class Wire extends EventEmitter {setTimeout() {} pause() {} end() {} }
        const wire = new Wire();
        const server = controlServer({command: (_action, _options, signal) => {
          running = probe.run({directory: root, resolveHome: d => d}, signal);
          return running;
        }}, 'revision', () => 'revision');
        server.emit('connection', wire);
        wire.emit('data', '{"action":"sandbox_probe","options":{}}\n');
        wire.emit('close'); server.close();
      } else {
        running = probe.run({directory: root, resolveHome: d => d}, abort.signal);
        if (child) {
          child.emit('message', {sandbox_class: 'userns_lsm_denied'});
          if (mode === 'classifier_exception') child.emit('message', {get ready() { throw Error('PRIVATE'); }});
          else if (mode === 'timeout') clock.advance(17500);
          else if (mode === 'cancel') abort.abort();
          else if (mode === 'spawn_error') child.emit('error', Error('PRIVATE'));
          else if (['sigtrap', 'signal', 'nonzero'].includes(mode)) child.emit('exit', 1, mode === 'sigtrap' ? 'SIGTRAP' : mode === 'signal' ? 'SIGTERM' : null);
          else child.emit('message', {sandbox_class: 'userns_lsm_denied', ready: mode !== 'readiness_failure',
            clean_close: mode !== 'readiness_failure', done: true});
        }
      }
      child?.emit('close');
      const result = await running;
      assert.equal(clock.pending.size, 0);
      assert.equal(child?.eventNames().length || 0, 0);
      assert.equal(require('node:events').getEventListeners(abort.signal, 'abort').length, 0);
      assert.equal(result.sandbox_probe.cleanup_class, mode === 'cleanup_failure' ? 'failed' : 'completed');
      if (!['spawn_throw', 'disconnect'].includes(mode)) assert.equal(result.sandbox_probe.sandbox_class, 'userns_lsm_denied');
      if (mode === 'sigtrap') assert.equal(result.sandbox_probe.signal_class, 'sigtrap');
      assert.equal(fs.existsSync(directory), false);
      assert.equal(fs.readFileSync(path.join(session, 'keep'), 'utf8'), 'synthetic');
      assert.equal(result.ok, ['clean', 'cleanup_retry'].includes(mode));
      if (mode === 'cleanup_failure') {
        assert.equal(result.sandbox_probe.clean_close, true);
        assert.equal(result.sandbox_probe.failure_class, 'none');
      }
      assert.ok(!JSON.stringify(result).includes('PRIVATE'));
      assert.ok(!JSON.stringify(result).includes(root));
    } finally { remove(root); }
  });
}

for (const mode of ['empty', 'root', 'traversal', 'session', 'symlink', 'replacement']) {
  test(`deletion refuses unowned target: ${mode}`, async () => {
    const root = fixture(), outside = fixture();
    const session = path.join(root, 'persisted-session');
    fs.mkdirSync(session, {mode: 0o700});
    fs.writeFileSync(path.join(session, 'keep'), 'synthetic');
    fs.writeFileSync(path.join(outside, 'keep'), 'synthetic');
    const files = Object.create(fs);
    let directory, child, deleted = false;
    files.mkdtempSync = prefix => {
      if (mode === 'empty') return '';
      if (mode === 'root') return root;
      if (mode === 'session') return session;
      if (mode === 'traversal') return root + '/../' + path.basename(outside);
      directory = fs.mkdtempSync(prefix); return directory;
    };
    files.rmdirSync = () => { deleted = true; assert.fail('unsafe deletion'); };
    const probe = new FixtureProbe({files, kill: () => {}, launch: () => {
      child = new EventEmitter(); child.pid = 123; return child;
    }});
    try {
      const running = probe.run({directory: root, resolveHome: d => d});
      if (mode === 'symlink' || mode === 'replacement') {
        fs.renameSync(directory, directory + '-retained');
        if (mode === 'symlink') fs.symlinkSync(outside, directory);
        else fs.mkdirSync(directory, {mode: 0o700});
      }
      child?.emit('message', {sandbox_class: 'userns_lsm_denied', done: true});
      child?.emit('close');
      const result = await running;
      assert.equal(deleted, false);
      assert.equal(result.ok, false);
      assert.equal(result.sandbox_probe.cleanup_class, mode === 'empty' ? 'none' : 'refused');
      assert.equal(fs.readFileSync(path.join(session, 'keep'), 'utf8'), 'synthetic');
      assert.equal(fs.readFileSync(path.join(outside, 'keep'), 'utf8'), 'synthetic');
    } finally { remove(root); remove(outside); }
  });
}

function clockFixture() {
  let time = 0, serial = 0;
  const pending = new Map();
  return {now: () => time, pending,
    timers: {setTimeout(fn, delay) { const id = ++serial; pending.set(id, {at: time + delay, fn}); return id; },
      clearTimeout(id) { pending.delete(id); }},
    advance(ms) {
      const end = time + ms;
      while (true) {
        const next = [...pending].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        time = next[1].at; pending.delete(next[0]); next[1].fn();
      }
      time = end;
    }};
}

for (const closes of [true, false]) test(`one global guard and absolute deadline, close=${closes}`, async () => {
  const root = fixture(), clock = clockFixture();
  let child, spawns = 0, creates = 0, kills = 0, directory;
  const files = Object.create(fs);
  files.mkdtempSync = prefix => { creates++; clock.advance(2000); return fs.mkdtempSync(prefix); };
  const abort = new AbortController();
  const {getEventListeners} = require('node:events');
  const probe = new FixtureProbe({...clock, files,
    launch: (_file, args) => { spawns++; directory = args[0]; child = new EventEmitter(); child.pid = 123; return child; },
    kill: (pid, signal) => {
      assert.equal(pid, 123); assert.equal(signal, 'SIGKILL'); kills++;
      if (closes) clock.timers.setTimeout(() => child.emit('close'), 100);
    }});
  try {
    const first = probe.run({directory: root, resolveHome: d => d}, abort.signal);
    const second = new FixtureProbe({launch: () => assert.fail('second spawn'),
      files: {mkdtempSync: () => assert.fail('second profile')}});
    assert.deepEqual(await second.run({}), {ok: false, status: 'busy'});
    assert.equal(spawns, 1); assert.equal(creates, 1);
    child.emit('message', {sandbox_class: 'userns_lsm_denied', signal_class: 'sigtrap', child_exit_class: 'signal'});
    let settled = false; first.then(() => { settled = true; });
    clock.advance(15500);
    await Promise.resolve(); assert.equal(settled, false); assert.equal(kills, 1);
    clock.advance(500);
    const result = await first;
    assert.ok(clock.now() <= 20000);
    assert.equal(result.sandbox_probe.timed_out, true);
    assert.equal(result.sandbox_probe.failure_class, 'deadline');
    assert.equal(result.sandbox_probe.sandbox_class, 'userns_lsm_denied');
    assert.equal(result.sandbox_probe.signal_class, 'sigtrap');
    assert.equal(result.sandbox_probe.child_exit_class, 'signal');
    assert.equal(result.sandbox_probe.elapsed_bucket, 'timeout');
    assert.equal(clock.pending.size, 0);
    assert.equal(child.eventNames().length, 0);
    assert.equal(getEventListeners(abort.signal, 'abort').length, 0);
    assert.equal(fs.existsSync(directory), false);
    assert.equal(second.busy, false);
  } finally { remove(root); }
});

for (const mode of ['failed', 'refused']) test(`cleanup ${mode} preserves frozen primary evidence`, async () => {
  const root = fixture(), clock = clockFixture();
  let child, directory, attempts = 0;
  const files = Object.create(fs);
  files.rmdirSync = () => {
    attempts++;
    child.emit('message', {sandbox_class: 'PRIVATE', signal_class: 'none'});
    throw Error('PRIVATE');
  };
  const probe = new FixtureProbe({...clock, files, kill: () => {},
    launch: (_file, args) => { directory = args[0]; child = new EventEmitter(); child.pid = 123; return child; }});
  try {
    const running = probe.run({directory: root, resolveHome: d => d});
    if (mode === 'refused') fs.chmodSync(directory, 0o755);
    child.emit('message', {sandbox_class: 'userns_lsm_denied', failure_class: 'readiness_failure',
      signal_class: 'sigtrap', child_exit_class: 'signal', done: true});
    child.emit('close');
    const result = await running;
    assert.equal(result.sandbox_probe.cleanup_class, mode);
    for (const [key, value] of Object.entries({sandbox_class: 'userns_lsm_denied',
      failure_class: 'readiness_failure', signal_class: 'sigtrap', child_exit_class: 'signal'}))
      assert.equal(result.sandbox_probe[key], value);
    assert.equal(attempts, mode === 'failed' ? 2 : 0);
    assert.equal(probe.busy, false);
    assert.equal(clock.pending.size, 0); assert.equal(child.eventNames().length, 0);
    assert.ok(!JSON.stringify(result).includes('PRIVATE'));
    assert.ok(!JSON.stringify(result).includes(root));
    // Permanent refusal/failure cannot promise deletion. The fixture owner repairs
    // it explicitly, then proves no residual remains without changing evidence.
    fs.chmodSync(directory, 0o700); remove(directory);
    assert.equal(fs.existsSync(directory), false);
  } finally { remove(root); }
});

test('profile creation consumes the shared deadline and cannot launch after expiry', async () => {
  const root = fixture(), clock = clockFixture(), files = Object.create(fs);
  files.mkdtempSync = prefix => { clock.advance(17500); return fs.mkdtempSync(prefix); };
  try {
    const result = await new FixtureProbe({...clock, files,
      launch: () => assert.fail('launch after deadline')}).run({directory: root, resolveHome: d => d});
    assert.equal(result.sandbox_probe.failure_class, 'deadline');
    assert.equal(result.sandbox_probe.cleanup_class, 'completed');
    assert.equal(clock.pending.size, 0);
    assert.deepEqual(fs.readdirSync(root), []);
  } finally { remove(root); }
});

for (const mode of ['slow', 'hung', 'termination_hung', 'error', 'launch_throw']) {
  test(`absolute cleanup budget and frozen evidence: ${mode}`, async () => {
    const root = fixture(), clock = clockFixture(), abort = new AbortController();
    let child, worker, identity, terminated = 0, unref = 0;
    const probe = new FixtureProbe({...clock, kill: () => {},
      launch: () => { child = new EventEmitter(); child.pid = 123; return child; },
      cleanupLaunch: value => {
        identity = value;
        if (mode === 'launch_throw') throw Error('PRIVATE');
        worker = new EventEmitter();
        worker.unref = () => {
          if (mode !== 'slow') assert.equal(terminated, 1);
          unref++;
        };
        worker.terminate = () => {
          terminated++;
          return mode === 'termination_hung' ? new Promise(() => {}) : Promise.resolve();
        };
        return worker;
      }});
    try {
      const running = probe.run({directory: root, resolveHome: d => d}, abort.signal);
      child.emit('message', {sandbox_class: 'userns_lsm_denied', signal_class: 'sigtrap', child_exit_class: 'signal'});
      clock.advance(17500); // Shutdown grace remains inside the operation budget.
      assert.equal(identity, undefined);
      clock.advance(500);
      await Promise.resolve();
      assert.equal(clock.now(), sandbox.OPERATION_MS);
      assert.equal(identity.deadline, sandbox.HARD_TIMEOUT_MS);
      assert.equal(identity.deadline - clock.now(), sandbox.CLEANUP_RESERVE_MS);
      assert.ok(Object.isFrozen(identity) && Object.isFrozen(identity.ownedInfo) && Object.isFrozen(identity.rootInfo));
      assert.equal((await new FixtureProbe().run({})).status, 'busy');
      if (mode === 'slow') {
        clock.advance(1949);
        worker.emit('message', sandbox.cleanupOwned(identity, fs, clock.now));
        worker.emit('exit', 0);
      } else if (mode === 'error') worker.emit('error', Error('PRIVATE'));
      else clock.advance(1950);
      const result = await running;
      assert.ok(clock.now() <= 20000 - sandbox.ASSEMBLY_RESERVE_MS);
      assert.equal(result.sandbox_probe.cleanup_class, mode === 'slow' ? 'completed' : 'failed');
      assert.equal(result.sandbox_probe.sandbox_class, 'userns_lsm_denied');
      assert.equal(result.sandbox_probe.signal_class, 'sigtrap');
      assert.equal(result.sandbox_probe.child_exit_class, 'signal');
      assert.equal(result.sandbox_probe.failure_class, 'deadline');
      assert.equal(clock.pending.size, 0);
      assert.equal(child.eventNames().length, 0);
      assert.equal(require('node:events').getEventListeners(abort.signal, 'abort').length, 0);
      assert.equal(worker?.eventNames().length || 0, 0);
      assert.equal(terminated, ['hung', 'termination_hung', 'error'].includes(mode) ? 1 : 0);
      assert.equal(unref, worker ? 1 : 0);
      worker?.emit('message', {sandbox_class: 'none'});
      assert.equal(result.sandbox_probe.sandbox_class, 'userns_lsm_denied');
      assert.equal(probe.busy, false);
      if (mode === 'slow') assert.deepEqual(fs.readdirSync(root), []);
    } finally { remove(root); }
  });
}

test('cleanup timeout does not turn successful primary evidence into operation timeout', async () => {
  const root = fixture(), clock = clockFixture();
  let child;
  const worker = new EventEmitter();
  worker.unref = () => {};
  worker.terminate = async () => {};
  try {
    const probe = new FixtureProbe({...clock, cleanupLaunch: () => worker, kill: () => {},
      launch: () => { child = new EventEmitter(); child.pid = 123; return child; }});
    const running = probe.run({directory: root, resolveHome: d => d});
    clock.advance(11000);
    child.emit('message', {sandbox_class: 'none', ready: true, clean_close: true, done: true});
    child.emit('close');
    await Promise.resolve();
    clock.advance(8950);
    const result = await running;
    assert.equal(result.ok, false);
    assert.equal(result.sandbox_probe.cleanup_class, 'failed');
    assert.equal(result.sandbox_probe.sandbox_class, 'none');
    assert.equal(result.sandbox_probe.clean_close, true);
    assert.equal(result.sandbox_probe.failure_class, 'none');
    assert.equal(result.sandbox_probe.timed_out, false);
    assert.equal(clock.pending.size, 0);
  } finally { remove(root); }
});

function cleanupIdentity(root, directory, deadline) {
  const identity = name => { const {dev, ino} = fs.lstatSync(name); return Object.freeze({dev, ino}); };
  return Object.freeze({root, directory, rootInfo: identity(root), ownedInfo: identity(directory), deadline});
}

test('native cleanup expiry closes descriptors and cannot follow replaced created directory', () => {
  for (const expire of [false, true]) {
    const root = fixture(), clock = clockFixture(), files = Object.create(fs);
    const directory = fs.mkdtempSync(path.join(root, 'sandbox-'));
    const identity = cleanupIdentity(root, directory, 20000);
    fs.writeFileSync(path.join(directory, 'temporary'), 'synthetic');
    const descriptors = new Set();
    files.openSync = (...args) => { const fd = fs.openSync(...args); descriptors.add(fd); return fd; };
    files.closeSync = fd => { descriptors.delete(fd); fs.closeSync(fd); };
    files.readdirSync = target => {
      // Simulate a native call returning after replacement and/or deadline.
      fs.renameSync(directory, directory + '-retained');
      fs.mkdirSync(directory, {mode: 0o700});
      fs.writeFileSync(path.join(directory, 'temporary'), 'replacement');
      if (expire) clock.advance(20000);
      return fs.readdirSync(target);
    };
    try {
      assert.equal(sandbox.cleanupOwned(identity, files, clock.now), expire ? 'failed' : 'refused');
      assert.equal(fs.readFileSync(path.join(directory, 'temporary'), 'utf8'), 'replacement');
      assert.equal(fs.existsSync(path.join(directory + '-retained', 'temporary')), expire);
      assert.equal(descriptors.size, 0);
      assert.equal(clock.pending.size, 0);
    } finally { remove(root); }
  }
});

test('real cleanup thread removes only owned fixture and exits without residual handles', async () => {
  const root = fixture(), directory = fs.mkdtempSync(path.join(root, 'sandbox-'));
  const {Worker} = require('node:worker_threads');
  let worker;
  fs.mkdirSync(path.join(directory, 'profile'));
  fs.writeFileSync(path.join(directory, 'profile', 'temporary'), 'synthetic');
  fs.writeFileSync(path.join(root, 'keep'), 'synthetic');
  fs.symlinkSync(root, path.join(directory, 'link'));
  try {
    const result = await sandbox.boundedCleanup(cleanupIdentity(root, directory, performance.now() + 2000), {
      now: () => performance.now(), timers: {setTimeout, clearTimeout},
      cleanupLaunch: identity => {
        worker = new Worker(path.join(__dirname, '../epoptia_login_sandbox.cjs'), {workerData: identity, env: {}, execArgv: []});
        return worker;
      }});
    assert.equal(result, 'completed');
    assert.equal(worker.threadId, -1);
    for (const event of ['message', 'error', 'exit']) assert.equal(worker.listenerCount(event), 0);
    assert.deepEqual(fs.readdirSync(root), ['keep']);
  } finally { remove(root); }
});

test('real stalled cleanup thread is terminated without delaying the fake absolute deadline', async () => {
  const {Worker} = require('node:worker_threads');
  const clock = clockFixture();
  let worker, online;
  const ready = new Promise(resolve => { online = resolve; });
  const result = sandbox.boundedCleanup(Object.freeze({deadline: 20000}), {...clock,
    cleanupLaunch: () => {
      worker = new Worker('require("node:worker_threads").parentPort.postMessage("ready"); Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);',
        {eval: true, env: {}, execArgv: []});
      worker.once('message', online);
      return worker;
    }});
  await ready;
  clock.advance(19950);
  assert.equal(await result, 'failed');
  assert.equal(clock.now(), 19950);
  assert.equal(clock.pending.size, 0);
  assert.equal(worker.listenerCount('message'), 0);
  assert.equal(worker.listenerCount('error'), 0);
  // Test-only bounded observation of actual termination, never browser launch.
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('cleanup worker did not exit')), 2000);
    worker.once('exit', () => { clearTimeout(timeout); resolve(); });
  });
  assert.equal(worker.threadId, -1);
  assert.equal(worker.listenerCount('exit'), 0);
});
