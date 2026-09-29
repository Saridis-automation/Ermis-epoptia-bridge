'use strict';
const fs = require('node:fs');
const net = require('node:net');
const {Backend, RUNTIME, sanitizeDiagnostic} = require('./epoptia_login_backend.cjs');
const {LoginSupervisor} = require('./epoptia_browser_login.cjs');
const {PrivateStore} = require('./epoptia_browser_session.cjs');
const {Scheduler} = require('./epoptia_browser_scheduler.cjs');
const policy = require('./epoptia_login_policy.cjs');
const {SandboxProbe, sanitizeProbe} = require('./epoptia_login_sandbox.cjs');
const SOCKET = RUNTIME + '/control.sock';
class Controller {
  constructor({store = new PrivateStore(), backend = new Backend(), scheduler = new Scheduler({store}), probe = new SandboxProbe(), fatal = () => process.exit(1)} = {}) {
    Object.assign(this, {store, backend, scheduler, probe, fatal}); this.busy = false;
  }
  async command(action, options = {}, signal = null) {
    if (!['start', 'status', 'diagnose', 'finalize', 'stop', 'sandbox_probe'].includes(action) || !options || typeof options !== 'object' || Array.isArray(options) ||
      Object.keys(options).some(k => action !== 'start' || k !== 'ttl_minutes') ||
      (Object.hasOwn(options, 'ttl_minutes') && (!Number.isInteger(options.ttl_minutes) || options.ttl_minutes < 1 || options.ttl_minutes > 5)))
      return {ok: false, status: 'invalid_action'};
    if (action === 'status' || action === 'diagnose') return {
      ...(this.supervisor?.status() || {ok: true, status: 'ready_not_enrolled'}),
      diagnostic: sanitizeDiagnostic({...this.backend.diagnose?.(), command_accepted: this.startAccepted}),
      ...(this.lastProbe ? {sandbox_probe: sanitizeProbe(this.lastProbe)} : {}),
    };
    if (this.busy || this.probe.busy) return {ok: false, status: 'busy'};
    this.busy = true;
    try {
      if (action === 'sandbox_probe') {
        if (this.supervisor) return {ok: false, status: 'busy'};
        const result = await this.probe.run(this.backend, signal);
        if (result.status === 'busy') return {ok: false, status: 'busy'};
        this.lastProbe = sanitizeProbe(result.sandbox_probe);
        return {...result, sandbox_probe: this.lastProbe};
      }
      if (action === 'start') {
        if (this.supervisor) return {ok: false, status: 'busy'};
        this.startAccepted = true;
        this.backend.clearDiagnostic?.();
        let ready;
        const response = new Promise(resolve => { ready = resolve; });
        this.running = this.scheduler.run(async scheduler => {
          if (signal?.aborted) return {ok: false, status: 'login_unavailable'};
          const released = new Promise(resolve => { this.release = resolve; });
          const supervisor = new LoginSupervisor({store: this.store, backend: this.backend, scheduler,
            origin: policy.origin, verify: policy.authenticated, ttlMs: (options.ttl_minutes || 5) * 60000});
          const stop = supervisor.stop.bind(supervisor);
          supervisor.stop = async () => {
            try { const value = await stop(); this.release(); return value; }
            catch { this.fatal(); throw Error('cleanup_failed'); }
          };
          this.backend.onFailure = () => supervisor.stop().catch(() => {});
          this.supervisor = supervisor;
          let cancelDeadline;
          const cancelStart = () => {
            cancelDeadline = setTimeout(this.fatal, 5000);
            supervisor.stop().finally(() => clearTimeout(cancelDeadline)).catch(() => this.fatal());
          };
          signal?.addEventListener('abort', cancelStart, {once: true});
          // Independent deadline also bounds a hung launch/finalize.
          const hardDeadline = setTimeout(this.fatal, supervisor.ttlMs + 5000);
          const deadline = setTimeout(() => supervisor.stop().catch(() => {}), supervisor.ttlMs);
          // Launch has a shorter independent bound than the enrollment TTL.
          const launchDeadline = setTimeout(this.fatal, 55000);
          try {
            const started = await supervisor.start();
            clearTimeout(launchDeadline);
            if (signal?.aborted) await supervisor.stop();
            ready(signal?.aborted ? {ok: false, status: 'login_unavailable'} : started);
            if (started.status !== 'awaiting_login') await supervisor.stop();
            await released;
          }
          finally { signal?.removeEventListener('abort', cancelStart); clearTimeout(cancelDeadline); clearTimeout(launchDeadline); clearTimeout(hardDeadline); clearTimeout(deadline); this.supervisor = null; }
          return {ok: true, status: 'stopped'};
        }).then(value => {
          if (!['stopped', 'enrolled'].includes(value.status)) this.backend.recordFailure?.(null, 'controller');
          ready(value); return value;
        }).catch(() => {
          this.backend.recordFailure?.(null, 'controller');
          const value = {ok: false, status: 'login_unavailable'}; ready(value); return value;
        });
        const value = await response;
        return {...value, diagnostic: sanitizeDiagnostic({...this.backend.diagnose?.(), command_accepted: this.startAccepted})};
      }
      if (action === 'stop') { this.backend.clearDiagnostic?.(); this.startAccepted = false; }
      if (!this.supervisor) return {ok: false, status: 'ready_not_enrolled'};
      const result = await this.supervisor[action]();
      await this.running;
      return result;
    } catch { return {ok: false, status: 'login_unavailable'}; }
    finally { this.busy = false; }
  }
}
function activationFd({env = process.env, pid = process.pid, uid = process.getuid(),
  gid = process.getgid(), files = fs} = {}) {
  if (uid === 0 || env.LISTEN_PID !== String(pid) || env.LISTEN_FDS !== '1') throw Error('invalid_activation');
  const fd = files.fstatSync(3);
  if (!fd.isSocket()) throw Error('invalid_activation');
  for (const [name, mode, kind] of [[RUNTIME, 0o700, 'isDirectory'], [SOCKET, 0o600, 'isSocket']]) {
    const info = files.lstatSync(name);
    if (!info[kind]() || info.uid !== uid || info.gid !== gid || (info.mode & 0o777) !== mode) throw Error('unsafe_runtime');
  }
  // Verify fd 3 is the exact listening AF_UNIX stream, not TCP or another UDS.
  const entries = files.readFileSync('/proc/net/unix', 'utf8').split('\n').slice(1);
  if (!entries.some(line => {
    const f = line.trim().split(/\s+/);
    return f.length === 8 && f[3] === '00010000' && f[4] === '0001' && f[5] === '01' &&
      f[6] === String(fd.ino) && f[7] === SOCKET;
  })) throw Error('invalid_activation');
  return 3;
}
const revision = () => require('node:crypto').createHash('sha256').update(
  fs.readFileSync(__dirname + '/admin_bootstrap/login_manifest.json')).digest('hex');
function controlServer(controller, sourceRevision, currentRevision = revision) {
  return net.createServer({allowHalfOpen: true}, socket => {
    const abort = new AbortController();
    let replied = false;
    socket.on('close', () => { if (!replied) abort.abort(); });
    socket.setTimeout(60000, () => socket.destroy());
    let data = '';
    let dispatched = false;
    socket.on('data', chunk => {
      if (dispatched) return socket.destroy();
      data += chunk;
      if (Buffer.byteLength(data) > 512) return socket.destroy();
      if (!data.includes('\n')) return;
      dispatched = true;
      socket.pause();
      let request;
      try {
        request = JSON.parse(data);
        if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).some(k => !['action', 'options'].includes(k))) throw Error();
      } catch { socket.destroy(); return; }
      let bindingsReady = false;
      try { require('./epoptia_login_apparmor.cjs').verify(); bindingsReady = true; } catch {}
      if (!bindingsReady || currentRevision() !== sourceRevision) {
        socket.end(JSON.stringify({ok: false, status: 'login_not_ready', operational_ready: false, source_revision: sourceRevision}));
        return;
      }
      controller.command(request.action, request.options, abort.signal).then(value => socket.end(JSON.stringify({...value, ...(value.diagnostic ? {diagnostic: sanitizeDiagnostic(value.diagnostic)} : {}), ...(value.sandbox_probe ? {sandbox_probe: sanitizeProbe(value.sandbox_probe)} : {}), operational_ready: true, source_revision: sourceRevision}), () => { replied = true; }))
        .catch(() => socket.end(JSON.stringify({ok: false, status: 'login_unavailable'})));
    });
    socket.on('error', () => {});
  });
}
async function serve() {
  process.umask(0o077);
  if (!fs.readFileSync('/proc/self/cgroup', 'utf8').split('\n').some(line => line.endsWith('/ermis-epoptia-login.service'))) throw Error('unmanaged_runtime');
  const fd = activationFd();
  delete process.env.LISTEN_PID; delete process.env.LISTEN_FDS; delete process.env.LISTEN_FDNAMES;
  const controller = new Controller();
  const sourceRevision = revision();
  const server = controlServer(controller, sourceRevision);
  server.on('error', () => process.exit(1));
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({fd}, () => {
        server.removeListener('error', reject);
        resolve();
    });
  });
  const stop = async () => { server.close(); await controller.supervisor?.stop(); process.exit(0); };
  // Source upgrades retire this owned process; the socket remains available.
  setInterval(() => { if (revision() !== sourceRevision) stop().catch(() => process.exit(1)); }, 1000).unref();
  process.once('SIGTERM', stop); process.once('SIGINT', stop);
}
module.exports = {Controller, SOCKET, serve, activationFd, controlServer};
if (require.main === module) serve().catch(() => process.exit(1));
