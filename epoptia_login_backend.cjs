'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const policy = require('./epoptia_login_policy.cjs');
const RUNTIME = '/run/ermis-epoptia-login';
const MANAGED_HOME = RUNTIME + '/enrollment';
// Exact match deliberately rejects relative paths, aliases, and traversal.
function resolveManagedHome(value, directory = MANAGED_HOME) {
  if (value !== MANAGED_HOME || directory !== MANAGED_HOME) throw Error('managed_home_invalid');
  return value;
}
function homeEnvironment(root) {
  return {HOME: root, XDG_CONFIG_HOME: root, XDG_CACHE_HOME: root,
    XDG_DATA_HOME: root, XDG_STATE_HOME: root, XDG_RUNTIME_DIR: root, TMPDIR: root};
}
// Only this closed schema may survive a launch attempt or cross IPC.
const diagnosticEnums = {
  failure_stage: ['none', 'prepare', 'display', 'vnc', 'websocket', 'executable', 'browser', 'navigation', 'health', 'controller'],
  failure_class: ['none', 'unknown', 'B_LOGIN_APPARMOR', 'managed_home_invalid', 'executable_missing', 'executable_not_executable', 'shared_library_missing', 'sandbox_denied', 'display_unavailable', 'profile_locked', 'profile_permission', 'cache_permission', 'spawn_error', 'immediate_nonzero_exit', 'signal_exit', 'readiness_timeout', 'bind_conflict', 'browser_crash', 'protocol_error', 'cancelled'],
  child_exit_class: ['none', 'spawn_error', 'nonzero', 'signal', 'clean', 'unknown'],
  signal_class: ['sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys', 'sigtrap', 'sighup', 'sigterm', 'other', 'none', 'unknown'],
};
// Node signal names only; never coerce arbitrary values into strings.
function signalClass(signal) {
  if (signal === null) return 'none';
  if (typeof signal !== 'string') return 'unknown';
  const name = signal.toLowerCase();
  if (diagnosticEnums.signal_class.includes(name) && name.startsWith('sig')) return name;
  return /^sig[a-z0-9]+$/.test(name) ? 'other' : 'unknown';
}
function sanitizeDiagnostic(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) value = {};
  const clean = {};
  for (const key of ['command_accepted', 'child_launch_attempted', 'spawn_returned', 'error_event',
    'exit_event', 'close_event', 'readiness_timeout', 'cleanup_started']) clean[key] = value[key] === true;
  for (const [key, allowed] of Object.entries(diagnosticEnums))
    clean[key] = allowed.includes(value?.[key]) ? value[key] : key === 'failure_stage' ? 'none' : 'unknown';
  return clean;
}
// Playwright owns Chromium's ChildProcess. Inspect its bounded launch exception
// transiently; no exception, output, identifiers or extracted strings are retained.
function launchText(error) {
  return typeof error?.message === 'string' ? error.message.slice(0, 32768).toLowerCase() : '';
}
function classifyFailure(error, exit = 'none', stage = 'browser') {
  if (error?.code === 'B_LOGIN_APPARMOR') return 'B_LOGIN_APPARMOR';
  // Commands contain flags such as sandbox/crash and are not failure evidence.
  const message = launchText(error).split('\n').filter(line => !line.includes('<launching>')).join('\n');
  const code = error?.code;
  if (message === 'managed_home_invalid') return 'managed_home_invalid';
  if (/shared librar|error while loading|cannot open shared object/.test(message)) return 'shared_library_missing';
  if (/no usable sandbox|sandbox.*(fail|denied|not permitted)|failed.*sandbox|running as root.*--no-sandbox|zygote.*operation not permitted/.test(message)) return 'sandbox_denied';
  if (/address already in use|server is already active/.test(message) || code === 'EADDRINUSE') return 'bind_conflict';
  if (/missing x server|cannot open display|display_unavailable|failed to open.*display/.test(message)) return 'display_unavailable';
  if (/(cache.*(permission denied|access denied)|unable to (create|move).*cache)/.test(message)) return 'cache_permission';
  if (/(profile|user.data|singleton).*(permission denied|access denied)|cannot create.*user data/.test(message)) return 'profile_permission';
  if (/processsingleton|singletonlock|profile.*(in use|locked)|user data directory is already in use/.test(message)) return 'profile_locked';
  if (code === 'ENOENT' || code === 'MODULE_NOT_FOUND' || /chromium_unavailable|executable doesn't exist|spawn .*enoent/.test(message)) return 'executable_missing';
  if ((stage === 'executable' && ['EACCES', 'EPERM'].includes(code)) || /spawn .*eacces|executable.*(permission denied|not executable)/.test(message)) return 'executable_not_executable';
  if (/launch_cancelled/.test(message)) return 'cancelled';
  if (/timeout|timed out|surface_unavailable/.test(message) || error?.name === 'TimeoutError') return 'readiness_timeout';
  if (/browser.*crash|page.*crash|segmentation fault|sigsegv|sigabrt|sigill|sigbus/.test(message)) return 'browser_crash';
  if (exit === 'signal' || /process did exit:.*signal=(?!null)[a-z0-9]+/.test(message)) return 'signal_exit';
  if (exit === 'nonzero' || /process did exit: exitcode=(?!0(?:,|\s))\d+/.test(message)) return 'immediate_nonzero_exit';
  if (/protocol error|invalid protocol|invalid message/.test(message)) return 'protocol_error';
  if (exit === 'spawn_error' || ['EACCES', 'EPERM', 'EAGAIN', 'ENOMEM', 'ENOEXEC'].includes(code) || /failed to launch|spawn .*error/.test(message)) return 'spawn_error';
  return 'unknown';
}
function browserLifecycle(error) {
  const message = launchText(error);
  const entry = message.match(/<process did exit:([^>\n]*)>/);
  const exit = !!entry;
  const signal = entry?.[1].match(/(?:^|,\s*)signal=([^,\s]+)(?=\s*$)/)?.[1];
  const signal_class = exit ? signalClass(signal === 'null' ? null : signal) : 'none';
  const errorEvent = /spawn .*e(?:noent|acces|again|nomem)|failed to launch/.test(message);
  return {
    spawn_returned: /<launched>/.test(message) || errorEvent,
    error_event: errorEvent,
    exit_event: exit,
    // The pinned Playwright emits this launch-log entry from ChildProcess.close.
    close_event: exit,
    readiness_timeout: classifyFailure(error) === 'readiness_timeout',
    cleanup_started: /<gracefully close start>|starting temporary directories cleanup/.test(message),
    signal_class,
    child_exit_class: exit ? (signal !== undefined && signal !== 'null' ? 'signal' :
      /exitcode=0(?:,|\s)/.test(message) ? 'clean' : /exitcode=\d+/.test(message) ? 'nonzero' : 'unknown') : errorEvent ? 'spawn_error' : 'none',
  };
}
async function waitForHealth({connect = require('node:net').createConnection,
  now = Date.now, sleep = ms => new Promise(resolve => setTimeout(resolve, ms))} = {}) {
  const probe = port => new Promise(resolve => {
    const socket = connect({host: '127.0.0.1', port});
    let data = '';
    const finish = ok => { clearTimeout(timer); socket.destroy(); resolve(ok); };
    const timer = setTimeout(() => finish(false), 250);
    socket.on('error', () => finish(false));
    socket.on('connect', () => {
      if (port === 6091) socket.write('GET /vnc.html HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n');
    });
    socket.on('data', chunk => {
      data += chunk;
      if (port === 5991 && data.includes('\n')) finish(/^RFB 003\.[0-9]{3}\n/.test(data));
      else if (data.includes('\r\n')) finish(/^HTTP\/1\.[01] 200 /.test(data));
      else if (data.length > 256) finish(false);
    });
    socket.on('end', () => finish(false));
  });
  const deadline = now() + 5000;
  do {
    if (await probe(5991) && await probe(6091)) return;
    await sleep(100);
  } while (now() < deadline);
  throw Error('surface_unavailable');
}
const commands = dir => [
  ['/usr/bin/Xvfb', [':91', '-screen', '0', '1280x800x24', '-nolisten', 'tcp', '-auth', path.join(dir, 'Xauthority')]],
  ['/usr/bin/x11vnc', ['-display', ':91', '-auth', path.join(dir, 'Xauthority'), '-listen', '127.0.0.1', '-rfbport', '5991', '-no6', '-forever', '-shared', '-passwdfile', path.join(dir, 'vnc-password')]],
  ['/usr/bin/websockify', ['--web=/usr/share/novnc', '127.0.0.1:6091', '127.0.0.1:5991']],
];
class Backend {
  constructor({launch = spawn, chromium = null, directory = RUNTIME + '/enrollment', health = waitForHealth,
    resolveHome = directory => resolveManagedHome(process.env.EPOPTIA_LOGIN_BROWSER_HOME, directory),
    setTemp = root => { process.env.TMPDIR = root; }} = {}) {
    Object.assign(this, {launch, chromium, directory, health, resolveHome, setTemp}); this.children = []; this.clearDiagnostic();
  }
  clearDiagnostic() { this.diagnostic = sanitizeDiagnostic({failure_stage: 'none', failure_class: 'none', child_exit_class: 'none', signal_class: 'none'}); }
  diagnose() { return sanitizeDiagnostic(this.diagnostic); }
  recordFailure(error, stage, exit = 'none', signal = this.diagnostic.signal_class) {
    if (this.diagnostic.failure_class !== 'none') return;
    this.diagnostic = sanitizeDiagnostic({...this.diagnostic, failure_stage: stage,
      failure_class: classifyFailure(error, exit, stage), child_exit_class: exit, signal_class: signal});
  }
  async start({host, offline = false, probeDirectory = null}) {
    this.clearDiagnostic();
    let stage = 'prepare';
    try {
      if (host !== '127.0.0.1') throw Error('unsafe_bind');
      const root = this.resolveHome(this.directory);
      stage = 'executable';
      const executable = require('./epoptia_login_apparmor.cjs').verify();
      stage = 'prepare';
      const info = fs.lstatSync(root);
      if (fs.realpathSync(root) !== root || !info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) throw Error('unsafe_runtime');
      // Parent TMPDIR bounds Playwright's own temporary artifacts, not just Chromium.
      if (offline) {
        if (typeof probeDirectory !== 'string' || path.dirname(probeDirectory) !== root ||
            !path.basename(probeDirectory).startsWith('sandbox-')) throw Error('unsafe_runtime');
        const probeInfo = fs.lstatSync(probeDirectory);
        if (fs.realpathSync(probeDirectory) !== probeDirectory || !probeInfo.isDirectory() ||
            probeInfo.isSymbolicLink() || probeInfo.uid !== process.getuid() ||
            (probeInfo.mode & 0o777) !== 0o700) throw Error('unsafe_runtime');
        this.dir = probeDirectory;
        this.probeOwnedByParent = true;
      } else {
        this.dir = fs.mkdtempSync(path.join(root, 'surface-'));
        this.probeOwnedByParent = false;
      }
      this.setTemp(offline ? this.dir : root);
      const childEnv = {PATH: '/usr/bin:/bin', ...homeEnvironment(this.dir),
        DISPLAY: ':91', XAUTHORITY: path.join(this.dir, 'Xauthority')};
      const field = value => { const b = Buffer.from(value); const n = Buffer.alloc(2); n.writeUInt16BE(b.length); return Buffer.concat([n, b]); };
      fs.writeFileSync(path.join(this.dir, 'Xauthority'), Buffer.concat([Buffer.from([1, 0]),
        field(require('node:os').hostname()), field('91'), field('MIT-MAGIC-COOKIE-1'), field(crypto.randomBytes(16))]), {mode: 0o600, flag: 'wx'});
      if (!offline) fs.writeFileSync(path.join(this.dir, 'vnc-password'), crypto.randomBytes(6).toString('base64') + '\n', {mode: 0o600, flag: 'wx'});
      this.stopping = false; this.aborted = false;
      for (const [bin, args] of (offline ? commands(this.dir).slice(0, 1) : commands(this.dir))) {
        stage = ['display', 'vnc', 'websocket'][this.children.length];
        const childStage = stage;
        this.diagnostic.child_launch_attempted = true;
        const child = this.launch(bin, args, {stdio: 'ignore', env: childEnv, detached: false});
        this.diagnostic.spawn_returned = true;
        this.children.push(child);
        for (const event of ['exit', 'close']) child.once(event, (code, signal) => {
          if (!this.stopping && !this.aborted) {
            this.diagnostic[event + '_event'] = true;
            this.recordFailure(null, childStage,
              signal != null ? 'signal' : code === 0 ? 'clean' : Number.isInteger(code) ? 'nonzero' : 'unknown',
              signalClass(signal));
            this.onFailure?.();
          }
        });
        await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', error => { this.diagnostic.error_event = true; this.recordFailure(error, childStage, 'spawn_error'); reject(error); }); });
        if (this.aborted) throw Error('launch_cancelled');
        if (bin === '/usr/bin/Xvfb') {
          for (let attempt = 0; attempt < 50 && !fs.existsSync('/tmp/.X11-unix/X91'); attempt++)
            await new Promise(resolve => setTimeout(resolve, 100));
          if (this.aborted || child.exitCode !== null || !fs.existsSync('/tmp/.X11-unix/X91')) throw Error('display_unavailable');
        }

      }
      stage = 'executable';
      const chromium = this.chromium || require('playwright').chromium;
      // Recheck after helper readiness: receipt/tree drift must block Chromium.
      require('./epoptia_login_apparmor.cjs').verify();
      if (this.aborted) throw Error('launch_cancelled');
      if (executable.status) throw Object.assign(Error('executable_resolution_failed'), {
        code: executable.status === 'runtime_permission_denied' ? 'EACCES' : 'ENOENT'});
      stage = 'browser';
      // Browser lifecycle evidence must not inherit successful helper spawns.
      Object.assign(this.diagnostic, browserLifecycle(null));
      this.context = await require('./epoptia_browser_launch.cjs').launch(chromium, {
        executablePath: executable.executablePath,
        headless: false, chromiumSandbox: true, serviceWorkers: 'block', timeout: 15000,
        env: childEnv,
        args: ['--disable-quic', '--disable-background-networking', '--disable-sync', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
          ...(offline ? require('./epoptia_login_sandbox.cjs').OFFLINE_ARGS : [])],
        ...(offline ? {offline: true} : {}),
      }, path.join(this.dir, 'profile'));
      this.diagnostic.spawn_returned = true;
      if (this.aborted) { await this.context.close(); throw Error('launch_cancelled'); }
      const page = this.context.pages()[0] || await this.context.newPage();
      if (offline) {
        await this.context.route('**/*', route => route.abort());
        await this.context.routeWebSocket('**/*', ws => ws.close());
        this.context.on('page', popup => { if (popup !== page) popup.close().catch(() => {}); });
        await page.goto('about:blank', {timeout: 15000});
        return {page};
      }
      const surface = {page, denied: false, redirects: 0, storageState: async () => ({cookies: await this.context.cookies(policy.origin), origins: []})};
      this.surface = surface;
      await this.context.route('**/*', async route => {
        if (!policy.sameOrigin(route.request().url())) { surface.denied = true; await route.abort(); return; }
        // Fetch manually: Playwright routing alone does not intercept redirect hops.
        try {
          const response = await route.fetch({maxRedirects: 0, timeout: 15000});
          const location = response.headers().location;
          if (location && !policy.redirectAllowed(location, route.request().url())) {
            surface.denied = true; await route.abort(); return;
          }
          if (response.status() >= 300 && response.status() < 400) {
            // Never fulfill a redirect: browser redirect chains may bypass routing.
            // Only a bounded top-level 301/302/303 becomes a fresh guarded GET.
            const request = route.request();
            if (!location || ![301, 302, 303].includes(response.status()) ||
                !request.isNavigationRequest() || request.frame() !== page.mainFrame() || ++surface.redirects > 10) {
              surface.denied = true; await route.abort(); return;
            }
            await route.abort();
            setImmediate(() => {
              if (!this.aborted) page.goto(new URL(location, request.url()).href,
                {waitUntil: 'domcontentloaded', timeout: 20000}).catch(() => { surface.denied = true; });
            });
            return;
          }
          if ([401, 403, 429].includes(response.status()) || response.status() >= 500) surface.denied = true;
          await route.fulfill({response});
        } catch { surface.denied = true; await route.abort().catch(() => {}); }
      });
      await this.context.routeWebSocket('**/*', ws => { surface.denied = true; ws.close(); });
      this.context.on('page', popup => { if (popup !== page) { surface.denied = true; popup.close().catch(() => {}); } });
      stage = 'navigation';
      await page.goto(policy.origin + '/login', {waitUntil: 'domcontentloaded', timeout: 20000}).catch(() => {
        if (!surface.redirects || surface.denied) throw Error('navigation_failed');
      });
      if (this.aborted) throw Error('launch_cancelled');
      stage = 'health';
      await this.health();
      if (this.aborted) throw Error('launch_cancelled');
      this.clearDiagnostic(); this.diagnostic.child_launch_attempted = true; this.diagnostic.spawn_returned = true;
      return surface;
    } catch (error) {
      if (stage === 'browser' && this.diagnostic.failure_class === 'none') {
        const lifecycle = browserLifecycle(error);
        lifecycle.spawn_returned ||= this.diagnostic.spawn_returned;
        Object.assign(this.diagnostic, lifecycle);
      }
      this.recordFailure(error, stage, this.diagnostic.child_exit_class);
      this.diagnostic.readiness_timeout = this.diagnostic.failure_class === 'readiness_timeout';
      await this.stop(); throw Error('backend_unavailable');
    }
  }
  async stop() {
    if (this.cleanup) return this.cleanup;
    this.aborted = true;
    this.cleanup = this.cleanupOwned();
    try { await this.cleanup; } finally { this.cleanup = null; }
  }
  async cleanupOwned() {
    this.diagnostic.cleanup_started = true;
    this.stopping = true;
    try { await this.context?.close(); } catch {}
    this.context = null;
    for (const child of this.children.reverse()) {
      if (child.pid && child.exitCode === null) {
        const exited = new Promise(resolve => child.once('exit', resolve));
        child.kill('SIGTERM');
        const timer = setTimeout(() => child.kill('SIGKILL'), 1000);
        await exited; clearTimeout(timer);
      }
    }
    this.children = [];
    if (this.dir && !this.probeOwnedByParent) fs.rmSync(this.dir, {recursive: true, force: true});
    this.dir = null; this.stopping = false;
  }
}
module.exports = {resolveManagedHome, homeEnvironment, MANAGED_HOME, Backend, commands, RUNTIME, waitForHealth, sanitizeDiagnostic, classifyFailure, browserLifecycle, signalClass};
