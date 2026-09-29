'use strict';
const {Backend} = require('./login_test_loader.cjs');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {classifyFailure, sanitizeDiagnostic} = require('../epoptia_login_backend.cjs');
const {Controller, controlServer} = require('./login_test_loader.cjs').load('epoptia_login_daemon.cjs', {
  './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: '/SYNTHETIC'})},
});
const {command} = require('../epoptia_browser_login.cjs');
const {scheduled} = require('./browser_policy_fixture.cjs');

test('failure categories never retain exception text or arbitrary fields', () => {
  for (const [error, expected] of [
    [{code: 'ENOENT'}, 'executable_missing'], [{code: 'EACCES'}, 'spawn_error'],
    [{code: 'EADDRINUSE'}, 'bind_conflict'], [Error('shared libraries PRIVATE'), 'shared_library_missing'],
    [Error('No usable sandbox PRIVATE'), 'sandbox_denied'], [Error('Missing X server PRIVATE'), 'display_unavailable'],
    [Error('Timeout PRIVATE'), 'readiness_timeout'], [Error('PRIVATE'), 'unknown'],
  ]) assert.equal(classifyFailure(error), expected);
  for (const value of [null, [], {failure_class: 'PRIVATE', stderr: 'PRIVATE', command_accepted: 'PRIVATE'}])
    assert.ok(!JSON.stringify(sanitizeDiagnostic(value)).includes('PRIVATE'));
});

test('ready -> failed child spawn retains sanitized diagnosis through cleanup and clears on stop', async () => {
  await scheduled(async s => {
    const backend = new Backend({resolveHome: directory => directory, setTemp: () => {}, directory: s.store.directory, launch: () => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit('error', Object.assign(Error('PRIVATE'), {code: 'ENOENT'})));
      return child;
    }});
    const controller = new Controller({store: s.store, backend, scheduler: {run: fn => fn(s)}});
    assert.equal((await controller.command('status')).status, 'ready_not_enrolled');
    assert.equal((await controller.command('start')).status, 'login_unavailable');
    await controller.running;
    const status = await controller.command('status');
    const diagnosis = await controller.command('diagnose');
    assert.deepEqual(status.diagnostic, diagnosis.diagnostic);
    assert.deepEqual(diagnosis.diagnostic, {command_accepted: true, child_launch_attempted: true,
      failure_stage: 'display', failure_class: 'executable_missing', child_exit_class: 'spawn_error', signal_class: 'none',
      spawn_returned: true, error_event: true, exit_event: false, close_event: false, readiness_timeout: false, cleanup_started: true});
    assert.ok(!JSON.stringify(diagnosis).includes('PRIVATE'));
    await controller.command('stop');
    assert.equal((await controller.command('diagnose')).diagnostic.failure_class, 'none');
    assert.equal((await controller.command('diagnose', {path: 'PRIVATE'})).status, 'invalid_action');
    return {ok: true};
  });
});

class Wire extends EventEmitter {
  setTimeout(_, fn) { this.expire = fn; }
  pause() {}
  destroy() {}
  end(data, done) { this.output = data; done?.(); }
}
test('diagnose socket protocol passes allowlisted fields and classifies connect failure', async () => {
  const revision = 'a'.repeat(64);
  const server = controlServer({command: async action => {
    assert.equal(action, 'diagnose');
    return {ok: true, status: 'ready_not_enrolled', diagnostic: {
      command_accepted: true, child_launch_attempted: true, failure_stage: 'browser',
      failure_class: 'sandbox_denied', child_exit_class: 'unknown', stderr: 'PRIVATE'}};
  }}, revision, () => revision);
  const daemon = new Wire();
  server.emit('connection', daemon);
  daemon.emit('data', '{"action":"diagnose","options":{}}\n');
  await new Promise(resolve => setImmediate(resolve));
  const client = new Wire();
  const pending = command('diagnose', {}, {connect: () => client});
  client.emit('connect');
  client.emit('data', daemon.output);
  client.emit('end');
  const result = await pending;
  assert.equal(result.ipc_phase, 'response');
  assert.equal(result.diagnostic.failure_class, 'sandbox_denied');
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  for (const event of ['error', 'timeout']) {
    const wire = new Wire();
    const pending = command('diagnose', {}, {connect: () => wire});
    if (event === 'error') wire.emit('error', Error('PRIVATE')); else wire.expire();
    assert.equal((await pending).ipc_phase, event === 'error' ? 'connect_failed' : 'timeout');
  }
  const legacy = new Wire();
  const pendingLegacy = command('start', {}, {prepare: () => {}, connect: () => legacy});
  legacy.emit('data', JSON.stringify({ok: false, status: 'unavailable', operational_ready: true,
    source_revision: revision, diagnostic: result.diagnostic}));
  legacy.emit('end');
  const mapped = await pendingLegacy;
  assert.equal(mapped.status, 'login_unavailable');
  assert.equal(mapped.diagnostic.failure_class, 'sandbox_denied');
  server.close();
});

test('scheduler rejection resolves start with retained controller classification', async () => {
  const backend = new Backend();
  const controller = new Controller({store: {}, backend, scheduler: {run: async () => { throw Error('PRIVATE'); }}});
  assert.equal((await controller.command('start')).status, 'login_unavailable');
  assert.equal((await controller.command('diagnose')).diagnostic.failure_stage, 'controller');
});

// Exercise both IPC sanitizers with the real controller and backend, without
// binding a listener or touching any installed enrollment state.
async function roundTrip(controller, action) {
  const revision = 'b'.repeat(64);
  const server = controlServer(controller, revision, () => revision);
  const daemon = new Wire(), client = new Wire();
  server.emit('connection', daemon);
  daemon.end = (data, done) => { done?.(); client.emit('data', data); client.emit('end'); };
  client.end = data => daemon.emit('data', data);
  const pending = command(action, {}, {prepare: () => {}, connect: () => client});
  client.emit('connect');
  try { return await pending; } finally { server.close(); }
}

const {browserLifecycle} = require('../epoptia_login_backend.cjs');
const signals = ['sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys', 'sigtrap', 'sighup', 'sigterm'];
const cases = [
  ['signal_exit', Error('<launched> PRIVATE\n<process did exit: exitCode=null, signal=SIGUSR1>')],
  ['signal_exit', Error('<launched> PRIVATE\n<process did exit: exitCode=null, signal=PRIVATE>')],
  ['signal_exit', Error('<launched> PRIVATE\n<process did exit: exitCode=null, signal=9>')],
  ...signals.map(signal => [classifyFailure(Error(`<process did exit: exitCode=null, signal=${signal}>`)),
    Error(`<launched> PRIVATE\n<process did exit: exitCode=null, signal=${signal.toUpperCase()}>`)]),
  ['executable_missing', Object.assign(Error('PRIVATE'), {code: 'ENOENT'})],
  ['executable_not_executable', Error('spawn PRIVATE EACCES')],
  ['shared_library_missing', Error('error while loading shared libraries PRIVATE')],
  ['sandbox_denied', Error('No usable sandbox PRIVATE')],
  ['display_unavailable', Error('Missing X server or $DISPLAY PRIVATE')],
  ['profile_locked', Error('user data directory is already in use PRIVATE')],
  ['profile_permission', Error('profile: Permission denied PRIVATE')],
  ['cache_permission', Error('Unable to create cache PRIVATE')],
  ['spawn_error', Object.assign(Error('PRIVATE'), {code: 'EAGAIN'})],
  ['immediate_nonzero_exit', Error('[pid=123] <process did exit: exitCode=1, signal=null> PRIVATE')],
  ['signal_exit', Error('[pid=123] <process did exit: exitCode=null, signal=SIGTERM> PRIVATE')],
  ['readiness_timeout', Error('Timeout 15000ms exceeded PRIVATE')],
  ['bind_conflict', Object.assign(Error('PRIVATE'), {code: 'EADDRINUSE'})],
  ['browser_crash', Error('<process did exit: exitCode=null, signal=SIGSEGV> PRIVATE')],
  ['protocol_error', Error('Protocol error (Browser.getVersion) PRIVATE')],
  ['unknown', Error('Target page, context or browser has been closed PRIVATE')],
];
for (const [expected, error] of cases) {
  test(`browser classifier and retained ready/start/failure/diagnose: ${expected}`, async () => {
    assert.equal(classifyFailure(error), expected);
    await scheduled(async s => {
      const fs = require('node:fs');
      const exists = fs.existsSync;
      let launches = 0;
      const backend = new Backend({resolveHome: directory => directory, setTemp: () => {}, directory: s.store.directory,
        resolve: () => ({executablePath: '/SYNTHETIC'}),
        chromium: {launchPersistentContext: async (_, options) => {
          launches++;
          assert.equal(options.chromiumSandbox, true);
          throw error;
        }},
        launch: () => {
          const child = new EventEmitter(); child.exitCode = null;
          queueMicrotask(() => child.emit('spawn')); return child;
        }});
      fs.existsSync = p => p === '/tmp/.X11-unix/X91' || exists(p);
      try {
        const controller = new Controller({store: s.store, backend, scheduler: {run: fn => fn(s)}});
        assert.equal((await roundTrip(controller, 'status')).status, 'ready_not_enrolled');
        const start = await roundTrip(controller, 'start');
        assert.equal(start.status, 'login_unavailable');
        assert.equal(launches, 1);
        assert.equal(start.diagnostic.failure_class, expected);
        assert.equal(start.diagnostic.failure_stage, 'browser');
        assert.equal(start.diagnostic.signal_class, browserLifecycle(error).signal_class);
        assert.equal(start.diagnostic.cleanup_started, true);
        await controller.running;
        for (const action of ['status', 'diagnose', 'diagnose']) {
          const result = await roundTrip(controller, action);
          assert.deepEqual(result.diagnostic, start.diagnostic);
          assert.ok(!JSON.stringify(result).includes('PRIVATE'));
        }
      } finally { fs.existsSync = exists; await backend.stop(); }
      return {ok: true};
    });
  });
}
test('bounded lifecycle evidence and sanitization reject all arbitrary fields and types', () => {
  const lifecycle = browserLifecycle(Error('<launched> pid=123\n<process did exit: exitCode=7, signal=null>\n<gracefully close start>'));
  assert.deepEqual(lifecycle, {spawn_returned: true, error_event: false, exit_event: true,
    close_event: true, readiness_timeout: false, cleanup_started: true, child_exit_class: 'nonzero', signal_class: 'none'});
  assert.equal(browserLifecycle(Error('spawn PRIVATE ENOENT')).error_event, true);
  assert.equal(browserLifecycle(Error('Timeout PRIVATE')).readiness_timeout, true);
  assert.equal(browserLifecycle(Error('<process did exit: exitCode=0, signal=null>')).child_exit_class, 'clean');
  const clean = sanitizeDiagnostic({ ...lifecycle, stderr: 'PRIVATE', stdout: 'PRIVATE', path: 'PRIVATE',
    command: 'PRIVATE', env: 'PRIVATE', pid: 123, port: 9999, url: 'PRIVATE', page: 'PRIVATE',
    token: 'PRIVATE', cookie: 'PRIVATE', credentials: 'PRIVATE', close_event: 'true'});
  assert.equal(clean.close_event, false);
  assert.equal(Object.keys(clean).length, 12);
  assert.ok(!JSON.stringify(clean).includes('PRIVATE'));
  for (const key of Object.keys(clean)) {
    for (const value of ['PRIVATE', {}, [], 123, null]) {
      assert.ok(!JSON.stringify(sanitizeDiagnostic({[key]: value})).includes('PRIVATE'));
    }
  }
  assert.equal(classifyFailure(Error('PRIVATE'.repeat(6000) + ' sandbox')), 'unknown');
});
test('specific causes take precedence over generic exit and cleanup text', () => {
  assert.equal(classifyFailure(Error('shared libraries PRIVATE <process did exit: exitCode=127, signal=null>')), 'shared_library_missing');
  assert.equal(classifyFailure(Error('No usable sandbox PRIVATE <process did exit: exitCode=null, signal=SIGABRT>')), 'sandbox_denied');
  assert.equal(classifyFailure(null, 'signal'), 'signal_exit');
  assert.equal(classifyFailure(null, 'nonzero'), 'immediate_nonzero_exit');
  assert.equal(classifyFailure({code: 'EACCES'}, 'none', 'executable'), 'executable_not_executable');
});

test('launch flags do not become causes and recordFailure retains a closed schema', () => {
  assert.equal(classifyFailure(Error('Target closed\n<launching> /PRIVATE/sandbox --crash-dumps-dir=/PRIVATE --timeout=15\n[pid=12] <process did exit: exitCode=1, signal=null>')), 'immediate_nonzero_exit');
  assert.equal(classifyFailure(Error('ProcessSingleton: Permission denied PRIVATE')), 'profile_permission');
  const backend = new Backend();
  backend.recordFailure(Error('PRIVATE'), 'PRIVATE', 'PRIVATE');
  assert.ok(!JSON.stringify(backend.diagnostic).includes('PRIVATE'));
});

const {signalClass} = require('../epoptia_login_backend.cjs');
test('signal normalization is exhaustive and never coerces malicious values', () => {
  for (const signal of signals) {
    for (const value of [signal, signal.toUpperCase(), 'SiG' + signal.slice(3)]) {
      assert.equal(signalClass(value), signal);
      assert.equal(browserLifecycle(Error(`<process did exit: exitCode=null, signal=${value}>`)).signal_class, signal);
    }
  }
  for (const [value, expected] of [[null, 'none'], [undefined, 'unknown'], [9, 'unknown'],
    ['SIGUSR1', 'other'], ['PRIVATE', 'unknown'], ['SIGTERM PRIVATE', 'unknown'],
    ['SIGTERM;PRIVATE', 'unknown'], ['', 'unknown'], [{toString() { throw Error('PRIVATE'); }}, 'unknown']]) {
    assert.equal(signalClass(value), expected);
    assert.ok(!JSON.stringify(sanitizeDiagnostic({signal_class: value})).includes('PRIVATE'));
  }
  for (const value of [...signals, 'other', 'none', 'unknown'])
    assert.equal(sanitizeDiagnostic({signal_class: value}).signal_class, value);
  assert.equal(browserLifecycle(Error('<process did exit: exitCode=0, signal=null>')).signal_class, 'none');
  assert.equal(browserLifecycle(Error('<process did exit: exitCode=null>')).signal_class, 'unknown');
  assert.equal(browserLifecycle(Error('<process did exit: exitCode=null, signal=SIGTERM>\n<process did exit: exitCode=null, signal=SIGKILL>')).signal_class, 'sigterm');
});

for (const order of [['exit', 'close'], ['close', 'exit']]) {
  for (const signal of [...signals, 'SIGUSR1', null, undefined, 9, 'PRIVATE']) {
    test(`first child failure persists across ${order.join('/')} and cleanup: ${signalClass(signal)}`, async () => {
      await scheduled(async s => {
        const backend = new Backend({resolveHome: directory => directory, setTemp: () => {}, directory: s.store.directory, launch: () => {
          const child = new EventEmitter();
          queueMicrotask(() => {
            child.emit(order[0], signal == null ? 1 : null, signal);
            child.emit(order[1], null, 'SIGKILL');
            child.emit('error', Error('PRIVATE'));
          });
          return child;
        }});
        const controller = new Controller({store: s.store, backend, scheduler: {run: fn => fn(s)}});
        const start = await roundTrip(controller, 'start');
        assert.equal(start.diagnostic.signal_class, signalClass(signal));
        assert.equal(start.diagnostic.failure_class, signal == null ? 'immediate_nonzero_exit' : 'signal_exit');
        assert.equal(start.diagnostic.child_exit_class, signal == null ? 'nonzero' : 'signal');
        assert.equal(start.diagnostic[order[0] + '_event'], true);
        // The first event starts cleanup synchronously; later events cannot rewrite it.
        assert.equal(start.diagnostic[order[1] + '_event'], false);
        await controller.running;
        await backend.stop();
        for (const action of ['status', 'diagnose'])
          assert.deepEqual((await roundTrip(controller, action)).diagnostic, start.diagnostic);
        assert.equal(start.diagnostic.cleanup_started, true);
        assert.ok(!JSON.stringify(start).includes('PRIVATE'));
        await controller.command('stop');
        assert.equal(backend.diagnose().signal_class, 'none');
        return {ok: true};
      });
    });
  }
}

for (const order of [['exit', 'close'], ['close', 'exit']]) {
  test(`first signal wins before cleanup in ${order.join('/')} order`, async () => {
    await scheduled(async s => {
      const backend = new Backend({resolveHome: directory => directory, setTemp: () => {}, directory: s.store.directory, launch: () => {
        const child = new EventEmitter();
        queueMicrotask(() => {
          child.emit(order[0], null, 'sIgTeRm');
          child.emit(order[1], null, 'SIGKILL');
          child.emit('error', Error('PRIVATE'));
        });
        return child;
      }});
      await assert.rejects(backend.start({host: '127.0.0.1'}), /backend_unavailable/);
      const diagnostic = backend.diagnose();
      assert.equal(diagnostic.signal_class, 'sigterm');
      assert.equal(diagnostic.failure_class, 'signal_exit');
      assert.equal(diagnostic.child_exit_class, 'signal');
      assert.equal(diagnostic.exit_event, true);
      assert.equal(diagnostic.close_event, true);
      assert.equal(diagnostic.cleanup_started, true);
      await backend.stop();
      assert.deepEqual(backend.diagnose(), diagnostic);
      return {ok: true};
    });
  });
}
