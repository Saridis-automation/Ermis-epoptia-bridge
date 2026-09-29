'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {activationFd, controlServer, SOCKET, Controller} = require('./login_test_loader.cjs').load('epoptia_login_daemon.cjs', {
  './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: '/SYNTHETIC'})},
});
const {command} = require('../epoptia_browser_login.cjs');
const {scheduled} = require('./browser_policy_fixture.cjs');
const {waitForHealth} = require('../epoptia_login_backend.cjs');

test('activation requires exactly one inherited listening Unix stream with private metadata', () => {
  const fixture = (change = {}) => ({
    env: {LISTEN_PID: '123', LISTEN_FDS: '1'}, pid: 123, uid: 1001, gid: 1001,
    files: {
      fstatSync: fd => { assert.equal(fd, 3); return {isSocket: () => true, ino: 42}; },
      lstatSync: name => ({uid: 1001, gid: 1001, mode: name === SOCKET ? 0o600 : 0o700,
        isSocket: () => name === SOCKET, isDirectory: () => name !== SOCKET}),
      readFileSync: name => { assert.equal(name, '/proc/net/unix'); return `header\n0: 00000002 00000000 00010000 0001 01 42 ${SOCKET}\n`; },
    }, ...change,
  });
  assert.equal(activationFd(fixture()), 3);
  for (const env of [{}, {LISTEN_PID: '999', LISTEN_FDS: '1'}, {LISTEN_PID: '123', LISTEN_FDS: '2'}])
    assert.throws(() => activationFd(fixture({env})));
  assert.throws(() => activationFd(fixture({uid: 0})));
  for (const row of ['', `header\n0: 0 0 00000000 0001 01 42 ${SOCKET}`, `header\n0: 0 0 00010000 0002 01 42 ${SOCKET}`,
    `header\n0: 0 0 00010000 0001 01 43 ${SOCKET}`, 'header\n0: 0 0 00010000 0001 01 42 /other']) {
    const f = fixture(); f.files.readFileSync = () => row;
    assert.throws(() => activationFd(f));
  }
  for (const override of [{uid: 0}, {gid: 0}, {mode: 0o666}, {isSocket: () => false}]) {
    const f = fixture(), original = f.files.lstatSync;
    f.files.lstatSync = name => ({...original(name), ...(name === SOCKET ? override : {})});
    assert.throws(() => activationFd(f));
  }
});

class Wire extends EventEmitter {
  setTimeout(value, callback) { this.timeout = value; this.expire = callback; }
  pause() {}
  destroy() { this.destroyed = true; this.emit('close'); }
  end(data, callback) { this.output = data; callback?.(); }
}

test('bounded readiness requires a VNC banner and successful loopback noVNC HTTP', async () => {
  for (const healthy of [true, false]) {
    const ports = [];
    let elapsed = 0;
    const probe = waitForHealth({now: () => elapsed, sleep: async () => { elapsed += 1000; },
      connect: ({host, port}) => {
        assert.equal(host, '127.0.0.1'); ports.push(port);
        const wire = new Wire(); wire.write = data => assert.ok(data.startsWith('GET /vnc.html HTTP/1.0'));
        queueMicrotask(() => {
          wire.emit('connect');
          wire.emit('data', port === 5991 ? 'RFB 003.008\n' : healthy ? 'HTTP/1.0 200 OK\r\n' : 'HTTP/1.0 503 Unavailable\r\n');
        });
        return wire;
      }});
    if (healthy) await probe;
    else await assert.rejects(probe, /surface_unavailable/);
    assert.deepEqual([...new Set(ports)], [5991, 6091]);
    assert.ok(elapsed <= 5000);
  }
});

test('protocol handles fragments, rejects extra requests and unknown fields, and cancels disconnection', async () => {
  const calls = [];
  const server = controlServer({command: async (...args) => { calls.push(args); return {ok: true, status: 'ready_not_enrolled'}; }}, 'a'.repeat(64), () => 'a'.repeat(64));
  const socket = new Wire(); server.emit('connection', socket);
  socket.emit('data', '{"action":'); socket.emit('data', '"status"}\n');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.equal(JSON.parse(socket.output).operational_ready, true);
  for (const invalid of ['[]\n', '{"action":"start","command":"PRIVATE"}\n', '{}\n{}\n', 'x'.repeat(513)]) {
    const bad = new Wire(); server.emit('connection', bad); bad.emit('data', invalid);
    assert.equal(bad.destroyed, true);
  }
  assert.equal(calls.length, 1);
  const pending = new Wire(); server.emit('connection', pending);
  pending.emit('data', '{"action":"start"}\n'); pending.destroy();
  assert.equal(calls[1][2].aborted, true);
  server.close();
});

test('client prepares before activation and transmits only the typed start request', async () => {
  const order = [], socket = new Wire();
  const pending = command('start', {ttl_minutes: 2}, {
    prepare: () => order.push('prepare'),
    connect: path => { order.push('connect'); assert.equal(path, SOCKET); return socket; },
  });
  assert.deepEqual(order, ['prepare', 'connect']);
  socket.emit('connect');
  assert.equal(socket.output, '{"action":"start","options":{"ttl_minutes":2}}\n');
  socket.emit('data', '{"ok":true,"status":"awaiting_login","operational_ready":true,"source_revision":"' + 'a'.repeat(64) + '"}');
  socket.emit('end');
  assert.equal((await pending).status, 'awaiting_login');
  assert.equal((await command('start', {}, {prepare: () => { throw Error('fixture'); }, connect: () => assert.fail('connected before preparation')})).status, 'login_not_ready');
});

test('disconnect during launch cleans only its enrollment and releases the scheduler', async () => {
  await scheduled(async s => {
    let release, stopped = 0;
    const backend = {start: () => new Promise(resolve => { release = resolve; }), stop: async () => { stopped++; release?.({}); }};
    const controller = new Controller({store: s.store, backend, scheduler: {run: fn => fn(s)}, fatal: () => assert.fail('fatal cleanup')});
    const abort = new AbortController();
    const pending = controller.command('start', {}, abort.signal);
    abort.abort();
    assert.equal((await pending).status, 'login_unavailable');
    await controller.running;
    assert.ok(stopped >= 1);
    assert.equal(controller.supervisor, null);
    return {ok: true};
  });
});
