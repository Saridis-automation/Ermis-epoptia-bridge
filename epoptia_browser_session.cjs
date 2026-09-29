'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const DEFAULT_DIRECTORY = path.join(os.userInfo().homedir, '.local/state/epoptia-browser');
const names = new Set(['session.json', 'session.pending.json', 'invalid.json', 'scheduler.json', 'operation.lock', 'console.secret']);
class PrivateStore {
  constructor(directory = DEFAULT_DIRECTORY) { this.directory = directory; }
  prepare() {
    const absolute = path.resolve(this.directory);
    let current = path.parse(absolute).root;
    for (const part of absolute.slice(current.length).split('/')) {
      current = path.join(current, part);
      try { fs.mkdirSync(current, {mode: 0o700}); } catch (e) { if (e.code !== 'EEXIST') throw e; }
      const s = fs.lstatSync(current);
      if (!s.isDirectory() || s.isSymbolicLink() || (s.mode & 0o022) ||
          (current === absolute && (s.uid !== process.getuid() || (s.mode & 0o777) !== 0o700)))
        throw Error('unsafe_storage');
    }
  }
  file(name) { if (!names.has(name)) throw Error('invalid_artifact'); return path.join(this.directory, name); }
  read(name) {
    this.prepare();
    let fd;
    try {
      fd = fs.openSync(this.file(name), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
      const s = fs.fstatSync(fd);
      if (!s.isFile() || s.nlink !== 1 || s.uid !== process.getuid() ||
          (s.mode & 0o777) !== 0o600 || s.size > 524288) throw Error('unsafe_storage');
      return JSON.parse(fs.readFileSync(fd, 'utf8'));
    } catch (e) { if (e.code === 'ENOENT') return null; throw e; }
    finally { if (fd !== undefined) fs.closeSync(fd); }
  }
  write(name, value) {
    if (Buffer.byteLength(JSON.stringify(value)) > 524288) throw Error('artifact_too_large');
    this.prepare();
    // Check an existing destination before replacing it, including dangling links.
    try {
      const s = fs.lstatSync(this.file(name));
      if (!s.isFile() || s.nlink !== 1 || s.uid !== process.getuid() || (s.mode & 0o777) !== 0o600)
        throw Error('unsafe_storage');
    } catch (e) { if (e.code !== 'ENOENT') throw e; }
    const temp = this.file(name) + '.' + crypto.randomBytes(16).toString('hex') + '.tmp';
    let fd;
    try {
      fd = fs.openSync(temp, 'wx', 0o600);
      fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
      fs.renameSync(temp, this.file(name));
      const dir = fs.openSync(this.directory, 'r');
      try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
      try { fs.unlinkSync(temp); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    }
  }
  remove(name) { this.prepare(); try { fs.unlinkSync(this.file(name)); } catch (e) { if (e.code !== 'ENOENT') throw e; } }
  saveSession(state, origin, expiresAt, now = Date.now()) {
    const host = new URL(origin).hostname;
    if (new URL(origin).origin !== origin || !origin.startsWith('https://') ||
        !Number.isFinite(expiresAt) || expiresAt <= now || expiresAt > now + 86400000 ||
        !Array.isArray(state.cookies) || !state.cookies.length || state.origins?.length)
      throw Error('invalid_session');
    // Cookies only: never retain form fields, credentials, localStorage or IndexedDB.
    const keys = ['name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'];
    const cookies = state.cookies.map(c => {
      if (c.domain !== host || c.secure !== true || typeof c.name !== 'string' ||
          /password|username|passwd/i.test(c.name) || typeof c.value !== 'string' ||
          !Number.isFinite(c.expires) || (c.expires !== -1 && c.expires * 1000 <= now)) throw Error('invalid_session');
      return Object.fromEntries(keys.map(k => [k, c[k]]));
    });
    const record = {origin, expiresAt, state: {cookies, origins: []}};
    // Preserve the last usable session until the complete candidate is durable.
    this.remove('session.pending.json');
    this.write('session.pending.json', record);
    const staged = this.read('session.pending.json');
    if (JSON.stringify(staged) !== JSON.stringify(record)) throw Error('invalid_session');
    try {
      const current = fs.lstatSync(this.file('session.json'));
      if (!current.isFile() || current.isSymbolicLink() || current.nlink !== 1 ||
          current.uid !== process.getuid() || (current.mode & 0o777) !== 0o600) throw Error('unsafe_storage');
    } catch (e) { if (e.code !== 'ENOENT') throw e; }
    fs.renameSync(this.file('session.pending.json'), this.file('session.json'));
    const dir = fs.openSync(this.directory, 'r');
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
    this.remove('invalid.json');
  }
  session(origin, now = Date.now()) {
    if (this.read('invalid.json')) return {status: 'login_required'};
    const record = this.read('session.json');
    if (!record) return {status: 'auth_material_missing'};
    if (record.origin !== origin || !Number.isFinite(record.expiresAt) || record.expiresAt <= now ||
        !Array.isArray(record.state?.cookies) || !record.state.cookies.length ||
        !Array.isArray(record.state.origins) || record.state.origins.length ||
        record.state.cookies.some(c => !Number.isFinite(c.expires) || /password|username|passwd/i.test(c.name) || c.domain !== new URL(origin).hostname || c.secure !== true ||
          (c.expires !== -1 && c.expires * 1000 <= now))) {
      this.invalidate(); return {status: 'login_required'};
    }
    return {state: record.state};
  }
  invalidate() { this.write('invalid.json', {invalid: true}); }
}
module.exports = {PrivateStore, DEFAULT_DIRECTORY};
