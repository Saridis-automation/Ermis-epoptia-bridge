'use strict';
// No package/cache/PATH resolution: the root-owned unit selects this one literal.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const PROFILE = '/etc/apparmor.d/ermis-epoptia-login-chromium';
const RECEIPT = '/usr/local/libexec/.ermis-epoptia-login-apparmor-receipt';
const ROOT = '/opt/ermis/epoptia-browser/chromium-1243';
const EXECUTABLE = ROOT + '/chrome-linux64/chrome';
const GRAMMAR = /^\/opt\/ermis\/epoptia-browser\/chromium-(1243)\/chrome-linux64\/chrome$/;
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const matches = (pattern, value) => typeof value === 'string' && pattern.exec(value)?.[0] === value;
function policyDigest(r) {
  return hash(`abi <abi/4.0>,\nattachment "${r.executable}" flags=(unconfined) {\n  userns,\n}\n`);
}
function profile(r) {
  if (r.schema !== 2 || r.executable !== EXECUTABLE || r.revision !== '1243' || r.layout !== 'chrome-linux64/chrome' ||
      r.playwright !== '1.63.0' || r.chromium !== '153.0.8010.12' ||
      !matches(/^[0-9a-f]{64}$/, r.sha256) || !matches(/^[0-9a-f]{64}$/, r.tree_sha256)) throw Error('B_LOGIN_APPARMOR');
  return `abi <abi/4.0>,\n# ermis-epoptia-login owned v2\n# playwright=${r.playwright} chromium=${r.chromium} revision=${r.revision} sha256=${r.sha256} tree_sha256=${r.tree_sha256}\nprofile "ermis-epoptia-login-chromium-${r.revision}-${policyDigest(r)}" "${r.executable}" flags=(unconfined) {\n  userns,\n}\n`;
}
function parents(file, io) {
  if (io.realpathSync(file) !== file) throw Error();
  for (let p = path.dirname(file); ; p = path.dirname(p)) {
    const s = io.lstatSync(p);
    if (!s.isDirectory() || s.isSymbolicLink() || s.uid !== 0 || s.gid !== 0 || (s.mode & 0o022)) throw Error();
    if (p === '/') break;
  }
}
function read(file, io, mode, limit = 1024 * 1024 * 1024) {
  const fd = io.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const s = io.fstatSync(fd);
    if (!s.isFile() || s.uid !== 0 || s.gid !== 0 || s.nlink !== 1 || (s.mode & 0o7777) !== mode || s.size > limit) throw Error();
    const data = io.readFileSync(fd);
    const after = io.fstatSync(fd), current = io.lstatSync(file);
    for (const k of ['dev', 'ino', 'size', 'mtimeMs', 'ctimeMs', 'mode', 'uid', 'gid'])
      if (s[k] !== after[k] || after[k] !== current[k]) throw Error();
    if (data.length !== s.size) throw Error();
    return data;
  } finally { io.closeSync(fd); }
}
function noCapabilities(paths) {
  // Node has no xattr API. Fixed isolated Python code only reads capability xattrs;
  // it does not import project code or execute browser/package files.
  cp.execFileSync('/usr/bin/python3', ['-I', '-c',
    'import os,sys,json,errno\nfor p in json.load(sys.stdin):\n try: os.getxattr(p,"security.capability",follow_symlinks=False)\n except OSError as e:\n  if e.errno != errno.ENODATA: raise\n else: raise ValueError()'],
  {input: JSON.stringify(paths), stdio: ['pipe', 'ignore', 'ignore'], timeout: 20000, env: {PATH: '/usr/bin:/bin'}});
}
function identity(_chromium, io = fs, _uid, options = {}) {
  const selected = process.env.EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE;
  if (selected !== EXECUTABLE) throw Error('B_LOGIN_APPARMOR');
  parents(EXECUTABLE, io);
  const records = [], paths = [];
  let executableHash;
  function walk(file, relative) {
    const s = io.lstatSync(file);
    if (s.isSymbolicLink() || s.uid !== 0 || s.gid !== 0 || (s.mode & 0o7022)) throw Error();
    paths.push(file);
    if (s.isDirectory()) {
      if ((s.mode & 0o7777) & ~0o755) throw Error();
      records.push([relative, 'd', 0o755, '']);
      const names = io.readdirSync(file).sort();
      for (const name of names) {
        if (!matches(/^[A-Za-z0-9_.+-]+$/, name) || ['.', '..'].includes(name)) throw Error();
        walk(file + '/' + name, relative ? relative + '/' + name : name);
      }
      if (JSON.stringify(names) !== JSON.stringify(io.readdirSync(file).sort())) throw Error();
    } else {
      const mode = s.mode & 0o111 ? 0o755 : 0o644;
      const data = read(file, io, mode);
      const digest = hash(data);
      if (file === EXECUTABLE) {
        if (mode !== 0o755 || data.subarray(0, 4).toString('hex') !== '7f454c46') throw Error();
        executableHash = digest;
      }
      records.push([relative, 'f', mode, digest]);
    }
  }
  walk(ROOT, '');
  (options.capabilities || noCapabilities)(paths);
  if (!executableHash) throw Error();
  records.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  return {schema: 2, executable: EXECUTABLE, revision: '1243', layout: 'chrome-linux64/chrome',
    playwright: '1.63.0', chromium: '153.0.8010.12', sha256: executableHash, tree_sha256: hash(JSON.stringify(records))};
}
function ownedRead(file, io) {
  parents(file, io);
  return read(file, io, 0o644, 16384).toString('utf8');
}
function verify() {
  const io = fs;
  try {
    let paths;
    const current = identity(undefined, io, undefined, {capabilities: value => { paths = value; }});
    const record = JSON.parse(ownedRead(RECEIPT, io));
    const expected = {...current, profile_sha256: hash(profile(current)),
      policy_identity: `ermis-epoptia-login-chromium-${current.revision}-${policyDigest(current)}`,
      policy_sha256: policyDigest(current)};
    if (Object.keys(record).length !== Object.keys(expected).length ||
        Object.entries(expected).some(([k, v]) => record[k] !== v) || ownedRead(PROFILE, io) !== profile(current)) throw Error();
    noCapabilities(paths);
    // Fixed offline admission check binds source and both installed receipts to
    // the final commit, and refuses while an installer holds its exclusive lock.
    cp.execFileSync('/usr/bin/python3', ['-I', '-B',
      '/home/ermis/projects/epoptia-bridge/admin_bootstrap/login_bootstrap.py', 'ready-check'],
      {stdio: 'ignore', timeout: 20000, env: {PATH: '/usr/bin:/bin'}});
    return {executablePath: EXECUTABLE};
  } catch { throw Object.assign(Error('B_LOGIN_APPARMOR'), {code: 'B_LOGIN_APPARMOR'}); }
}
module.exports = {identity, verify, profile, GRAMMAR, PROFILE, RECEIPT, EXECUTABLE, ROOT};
