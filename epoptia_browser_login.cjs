'use strict';
const crypto = require('node:crypto');
const {PrivateStore} = require('./epoptia_browser_session.cjs');
const policy = require('./epoptia_browser_policy.cjs');
const result = status => ({ok: status === 'stopped' || status === 'enrolled', status});
function loopback(host) { return host === '127.0.0.1' || host === '::1'; }
// Persistent ownership is supplied by epoptia_login_daemon.cjs.
class LoginSupervisor {
  constructor({store = new PrivateStore(), backend = null, verify = null, scheduler = null,
    origin = policy.origin, host = '127.0.0.1', ttlMs = 300000,
    timer = setTimeout, cancel = clearTimeout} = {}) {
    if (!loopback(host) || !Number.isFinite(ttlMs) || ttlMs <= 0 || ttlMs > 300000) throw Error('unsafe_login_policy');
    Object.assign(this, {store, backend, verify, scheduler, origin, host, ttlMs, timer, cancel});
    this.surface = null;
  }
  status() { return result(this.surface ? 'awaiting_login' : 'ready_not_enrolled'); }
  async start() {
    if (this.surface) return result('busy');
    if (!this.backend || !this.verify || !this.origin) return this.status();
    // Backend is a trusted source-code dependency, never a caller-provided command.
    // It must create a private disposable profile, disable TCP X11/CDP, authenticate
    // VNC/WebSocket with private disposable authentication, bind loopback, and own cleanup.
    if (!this.scheduler?.held || this.scheduler.store !== this.store || this.scheduler.state.circuit)
      return result('policy_unavailable');
    this.store.prepare();
    this.store.write('console.secret', crypto.randomBytes(32).toString('hex'));
    try {
      this.surface = await this.backend.start({host: this.host, scheduler: this.scheduler, secretFile: this.store.file('console.secret')});
      this.deadline = this.timer(() => this.stop().catch(() => {}), this.ttlMs);
      this.deadline?.unref?.();
      return this.status();
    } catch { await this.stop(); return result('login_unavailable'); }
  }
  async finalize() {
    if (!this.surface || !this.verify) return result('ready_not_enrolled');
    if (!this.scheduler?.held || this.scheduler.state.circuit) {
      await this.stop(); return result('login_required');
    }
    try {
      if (await this.verify(this.surface) !== true) return result('login_required');
      const state = await this.surface.storageState();
      if (await this.verify(this.surface) !== true) return result('login_required');
      this.store.saveSession(state, this.origin, Date.now() + 3600000);
      return result('enrolled');
    } catch { return result('login_unavailable'); }
    finally { await this.stop(); }
  }
  async stop() {
    if (this.deadline) this.cancel(this.deadline);
    try { await this.backend?.stop(); }
    finally { this.surface = null; this.store.remove('console.secret'); }
    return result('stopped');
  }
}
async function command(action, options = {}, dependencies = {}) {
  if (!['start', 'status', 'diagnose', 'stop', 'finalize', 'sandbox_probe'].includes(action) ||
      !options || typeof options !== 'object' || Array.isArray(options) ||
      Object.keys(options).some(key => action !== 'start' || key !== 'ttl_minutes') ||
      (Object.hasOwn(options, 'ttl_minutes') &&
       (!Number.isInteger(options.ttl_minutes) || options.ttl_minutes < 1 || options.ttl_minutes > 5)))
    return result('invalid_action');
  return new Promise(resolve => {
    const net = require('node:net');
    // Prepare private enrollment state before the activation-triggering connect.
    // No session/credential contents are read by prepare().
    try {
      if (action === 'start') (dependencies.prepare || (() => new PrivateStore().prepare()))();
    } catch { resolve(result('login_not_ready')); return; }
    const socket = (dependencies.connect || net.createConnection)('/run/ermis-epoptia-login/control.sock');
    let data = '';
    const finish = value => { socket.destroy(); resolve(value); };
    socket.setTimeout(60000, () => finish({...result('login_not_ready'), ...(action === 'diagnose' ? {ipc_phase: 'timeout'} : {})}));
    socket.on('error', () => finish({...result('login_not_ready'), ...(action === 'diagnose' ? {ipc_phase: 'connect_failed'} : {})}));
    socket.on('connect', () => socket.end(JSON.stringify({action, options}) + '\n'));
    socket.on('data', chunk => { data += chunk; if (data.length > 2048) finish(result('login_unavailable')); });
    socket.on('end', () => {
      try {
        const value = JSON.parse(data);
        if (value.status === 'unavailable') value.status = 'login_unavailable';
        const statuses = ['login_not_ready', 'stopped', 'enrolled', 'awaiting_login', 'ready_not_enrolled', 'busy',
          'policy_unavailable', 'login_required', 'login_unavailable', 'invalid_action', 'sandbox_probe_complete'];
        finish(statuses.includes(value.status) ? {ok: value.ok === true, status: value.status, diagnostic: require('./epoptia_login_backend.cjs').sanitizeDiagnostic(value.diagnostic), ...(value.sandbox_probe ? {sandbox_probe: require('./epoptia_login_sandbox.cjs').sanitizeProbe(value.sandbox_probe)} : {}), ipc_phase: 'response', operational_ready: value.operational_ready === true, source_revision: /^[a-f0-9]{64}$/.test(value.source_revision) ? value.source_revision : null} : result('login_unavailable'));
      } catch { finish(result('login_unavailable')); }
    });
  });
}
module.exports = {LoginSupervisor, loopback, command};
if (require.main === module) Promise.resolve().then(() => command([3, 4].includes(process.argv.length) ? process.argv[2] : null, process.argv.length === 4 ? JSON.parse(process.argv[3]) : {}))
  .then(value => process.stdout.write(JSON.stringify(value)))
  .catch(() => process.stdout.write(JSON.stringify(result('login_unavailable'))));
