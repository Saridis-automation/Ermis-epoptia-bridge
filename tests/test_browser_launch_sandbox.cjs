'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const {load} = require('./login_test_loader.cjs');
const launcher = require('../epoptia_browser_launch.cjs');
const safe = {chromiumSandbox: true, args: [], executablePath: '/SYNTHETIC'};

test('production launcher rejects sandbox overrides before calling Playwright', () => {
  let calls = 0;
  const chromium = {launch: () => calls++, launchPersistentContext: () => calls++};
  for (const injected of [
    {chromiumSandbox: false}, {chromiumSandbox: undefined},
    ...['--no-sandbox', '--no-sandbox=true', '--disable-setuid-sandbox',
      '--disable-seccomp-filter-sandbox', '--disable-gpu-sandbox', '--single-process',
      '--no-zygote', '--disable-features=NetworkServiceSandbox', '--unknown'].map(arg => ({args: [arg]})),
    {ignoreDefaultArgs: true}, {ignoreDefaultArgs: ['--sandbox']},
    {extraArgs: ['--no-sandbox']}, {config: {chromiumSandbox: false}},
    ...['CHROMIUM_FLAGS', 'CHROME_DEVEL_SANDBOX', 'NODE_OPTIONS', 'LD_PRELOAD',
      'PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'].map(key => ({env: {[key]: '--no-sandbox'}})),
  ]) {
    for (const directory of [undefined, '/synthetic-profile'])
      assert.throws(() => launcher.launch(chromium, {...safe, ...injected}, directory), {code: 'B_LOGIN_SANDBOX'});
  }
  const inherited = Object.assign(Object.create({chromiumSandbox: true}), {args: []});
  assert.throws(() => launcher.launch(chromium, inherited), {code: 'B_LOGIN_SANDBOX'});
  assert.equal(calls, 0);
});

test('actual production runtime and Playwright argv preserve Chromium sandbox', async () => {
  const root = fs.mkdtempSync(path.join(__dirname, 'launch-argv-'));
  const executable = path.join(root, 'synthetic-chrome');
  fs.writeFileSync(executable, 'synthetic fixture');
  fs.chmodSync(executable, 0o700);
  const originalSpawn = cp.spawn;
  let captured;
  // Intercept the OS boundary: no browser or helper process can execute.
  cp.spawn = (bin, args, options) => {
    captured = {bin, args, options};
    throw Error('synthetic spawn boundary');
  };
  try {
    const subject = load('epoptia_browser_runtime.cjs', {
      './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: executable})},
    }, {CHROMIUM_FLAGS: '--no-sandbox'});
    const io = {mkdirSync() {}, lstatSync: () => ({isDirectory: () => true,
      isSymbolicLink: () => false, uid: process.getuid(), mode: 0o700})};
    const result = await subject.launchBrowser({io});
    assert.equal(result.status, 'launch_failed');
    assert.equal(captured.bin, executable);
    assert.ok(captured.args.includes('--remote-debugging-pipe'));
    assert.ok(!captured.args.some(arg => /no-sandbox|disable.*sandbox|single-process|no-zygote/.test(arg)));
    assert.equal(captured.options.env.CHROMIUM_FLAGS, undefined);
    captured = null;
    await assert.rejects(launcher.launch(require('playwright').chromium,
      {...safe, executablePath: executable}, path.join(root, 'profile')));
    assert.equal(captured.bin, executable);
    assert.ok(!captured.args.some(arg => /no-sandbox|disable.*sandbox/.test(arg)));
  } finally {
    cp.spawn = originalSpawn;
    fs.rmSync(root, {recursive: true});
  }
});

test('actual daemon status/start/probe reject binding drift before dispatch', async () => {
  const {EventEmitter} = require('node:events');
  let drift = false, checks = 0, commands = 0;
  const {controlServer} = load('epoptia_login_daemon.cjs', {
    './epoptia_login_apparmor.cjs': {verify() { checks++; if (drift) throw Error('fixture drift'); }},
  });
  const revision = 'a'.repeat(64);
  const server = controlServer({command: async () => { commands++; return {ok: true, status: 'ready_not_enrolled'}; }}, revision, () => revision);
  class Wire extends EventEmitter {
    setTimeout() {} pause() {} destroy() {} end(data, done) { this.output = JSON.parse(data); done?.(); }
  }
  try {
    for (const action of ['status', 'start', 'sandbox_probe']) {
      for (const changed of [false, true]) {
        drift = changed;
        const before = commands;
        const wire = new Wire();
        server.emit('connection', wire);
        wire.emit('data', JSON.stringify({action, options: {}}) + '\n');
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(wire.output.operational_ready, !changed);
        assert.equal(commands - before, changed ? 0 : 1);
        if (changed) assert.equal(wire.output.status, 'login_not_ready');
      }
    }
    assert.equal(checks, 6);
  } finally { server.close(); }
});
