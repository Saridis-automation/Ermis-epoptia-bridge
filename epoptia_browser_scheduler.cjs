'use strict';
const fs = require('node:fs');
const {PrivateStore} = require('./epoptia_browser_session.cjs');
const active = new Set();
const stopped = status => ({ok: false, status});
class Scheduler {
  constructor({store = new PrivateStore(), now = Date.now,
    sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), navigationMs = 8000,
    settleMs = 3000, cooldownMs = 60000} = {}) {
    if (![navigationMs, settleMs, cooldownMs].every(Number.isFinite) ||
        navigationMs < 8000 || settleMs < 3000 || cooldownMs < 60000) throw Error('unsafe_timing');
    Object.assign(this, {store, now, sleep, navigationMs, settleMs, cooldownMs});
    this.held = false;
  }
  async run(operation, {allowStopped = false} = {}) {
    const key = this.store.file('operation.lock');
    if (active.has(key)) return stopped('busy');
    active.add(key);
    let fd;
    try {
      this.store.prepare();
      // Exclusive persistent lock file: never steal a stale lock automatically.
      fd = fs.openSync(key, 'wx', 0o600);
      this.held = true;
      this.state = this.store.read('scheduler.json') || {count: 0, next: 0, circuit: null};
      if (!Number.isInteger(this.state.count) || this.state.count < 0 || this.state.count > 10 ||
          !Number.isFinite(this.state.next) || ![null, 'site_denied', 'login_required'].includes(this.state.circuit))
        return stopped('policy_state_invalid');
      if (this.state.circuit && !allowStopped) return stopped(this.state.circuit === 'login_required' ? 'login_required' : 'circuit_open');
      return await operation(this);
    } catch (e) { return stopped(e.code === 'EEXIST' ? 'busy' : 'policy_unavailable'); }
    finally {
      this.held = false;
      if (fd !== undefined) { fs.closeSync(fd); this.store.remove('operation.lock'); }
      active.delete(key);
    }
  }
  requireLock() { if (!this.held) throw Error('scheduler_lock_required'); }
  persist() { this.store.write('scheduler.json', this.state); }
  async navigate(operation) {
    this.requireLock();
    if (this.state.circuit) return stopped(this.state.circuit === 'login_required' ? 'login_required' : 'circuit_open');
    await this.sleep(Math.max(0, this.state.next - this.now()));
    if (this.state.count === 10) this.state.count = 0;
    this.state.count++;
    this.state.next = this.now() + (this.state.count === 10 ? Math.max(this.cooldownMs, this.navigationMs) : this.navigationMs);
    this.persist();
    try { return await operation(); }
    finally {
      if (this.state.count === 10) {
        this.state.next = Math.max(this.state.next, this.now() + this.cooldownMs);
        this.persist();
      }
    }
  }
  observe(code, login = false) {
    this.requireLock();
    if (code === 403 || code === 429 || (code >= 500 && code <= 599)) this.state.circuit = 'site_denied';
    else if (!this.state.circuit && (login || code === 401 || (code >= 300 && code < 400))) {
      this.state.circuit = 'login_required'; this.store.invalidate();
    }
    if (this.state.circuit) this.persist();
    return this.state.circuit ? stopped(this.state.circuit === 'login_required' ? 'login_required' : 'circuit_open') : null;
  }
  async click(operation) {
    this.requireLock();
    if (this.state.circuit) return stopped('circuit_open');
    try { return await operation(); } finally { await this.sleep(this.settleMs); }
  }
  async localTransport(operation) {
    this.requireLock();
    for (let attempt = 0; attempt < 3; attempt++) {
      if (this.state.circuit) return stopped('circuit_open');
      try { return await operation(); }
      catch (e) {
        // Only structured local IPC errors, never Playwright/net/status-page errors.
        if (e.localIPC !== true || !['EPIPE', 'ECONNRESET'].includes(e.code) || attempt === 2)
          return stopped('transport_stopped');
        await this.sleep(1000 * 2 ** attempt);
      }
    }
  }
}
async function visibleDenial(page) {
  return page.evaluate(() => {
    const text = (document.body?.innerText || '').slice(0, 8192);
    const match = text.match(/(?:^|\n)\s*(403|429|5\d\d)\b|\b(?:HTTP|error|status)\s*[: -]?\s*(403|429|5\d\d)\b/i);
    if (match) return Number(match[1] || match[2]);
    if (/forbidden|access denied/i.test(text)) return 403;
    if (/too many requests|rate limit exceeded/i.test(text)) return 429;
    if (/internal server error|bad gateway|service unavailable|gateway timeout/i.test(text)) return 503;
    return 0;
  });
}
module.exports = {Scheduler, visibleDenial};
