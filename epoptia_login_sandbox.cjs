'use strict';
// No caller-provided target, command, path, environment or deadline.
const fs = require('node:fs');
const path = require('node:path');
const {fork} = require('node:child_process');
const {Worker, isMainThread, workerData, parentPort} = require('node:worker_threads');
const HARD_TIMEOUT_MS = 20000, CLEANUP_RESERVE_MS = 2000, ASSEMBLY_RESERVE_MS = 50;
const OPERATION_MS = HARD_TIMEOUT_MS - CLEANUP_RESERVE_MS;
const DWELL_MS = 10000, CLOSE_GRACE_MS = 500;
// Shared by every controller/probe instance in this daemon process.
let inFlight = false;
const OFFLINE_ARGS = Object.freeze([
  '--host-resolver-rules=MAP * ~NOTFOUND', '--proxy-server=http://offline.invalid:9',
  '--proxy-bypass-list=<-loopback>', '--disable-component-update',
  '--disable-domain-reliability', '--disable-client-side-phishing-detection',
  '--disable-breakpad', '--no-pings',
]);
const ENUMS = Object.freeze({
  cleanup_class: ['none', 'completed', 'refused', 'failed'],
  failure_class: ['none', 'B_LOGIN_APPARMOR', 'deadline', 'cancelled', 'spawn_error', 'readiness_failure', 'classifier_exception', 'shutdown_failure', 'unknown'],
  sandbox_class: ['userns_lsm_denied', 'unit_namespace_denied', 'kernel_userns_disabled',
    'seccomp_clone_denied', 'setuid_helper_unusable', 'root_identity_refused',
    'proc_namespace_incompatible', 'runtime_fs_denied', 'sandbox_check_other', 'none', 'unknown'],
  user_namespace: ['success', 'permission_denied', 'unavailable', 'unknown'],
  apparmor_restriction: ['enabled', 'disabled', 'unavailable', 'unknown'],
  kernel_userns: ['enabled', 'disabled', 'unavailable', 'unknown'],
  apparmor_confinement: ['confined', 'unconfined', 'unavailable', 'unknown'],
  setuid_helper: ['valid', 'invalid', 'absent', 'unavailable', 'unknown'],
  child_exit_class: ['none', 'spawn_error', 'nonzero', 'signal', 'clean', 'unknown'],
  signal_class: ['sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys', 'sigtrap', 'sighup', 'sigterm', 'other', 'none', 'unknown'],
  elapsed_bucket: ['under_5s', '5_to_10s', '10_to_15s', '15_to_20s', 'timeout', 'unknown'],
});
const BOOLS = ['command_accepted', 'spawn_returned', 'ready', 'clean_close', 'nonroot', 'timed_out'];
function sanitizeProbe(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) value = {};
  const out = {};
  for (const key of BOOLS) out[key] = value[key] === true;
  for (const [key, allowed] of Object.entries(ENUMS)) out[key] = allowed.includes(value[key]) ? value[key] : key === 'cleanup_class' ? 'none' : 'unknown';
  return out;
}
function classifySandbox(text) {
  if (typeof text !== 'string') return 'unknown';
  text = text.slice(0, 8192).toLowerCase();
  if (/apparmor.*(?:userns|user namespace).*(?:denied|restrict)|userns.*apparmor.*denied/.test(text)) return 'userns_lsm_denied';
  if (/(?:restrictnamespaces|systemd.*namespace).*(?:denied|not permitted)/.test(text)) return 'unit_namespace_denied';
  if (/(?:unprivileged_userns_clone|user namespaces?).*(?:disabled|=\s*0)/.test(text)) return 'kernel_userns_disabled';
  if (/seccomp.*(?:clone|unshare).*(?:denied|not permitted)|(?:clone|unshare).*seccomp.*denied/.test(text)) return 'seccomp_clone_denied';
  if (/running as root.*without/.test(text)) return 'root_identity_refused';
  if (/(?:suid|setuid|chrome-sandbox).*(?:not configured|incorrect|unusable|permission denied)/.test(text)) return 'setuid_helper_unusable';
  if (/proc.*(?:namespace|ns\/).*(?:incompatible|denied|not permitted)/.test(text)) return 'proc_namespace_incompatible';
  if (/(?:profile|singleton|cache|user.data|runtime).*(?:permission denied|read.only file system)/.test(text)) return 'runtime_fs_denied';
  if (/no usable sandbox|sandbox.*(?:fail|denied)|failed.*namespace|zygote.*operation not permitted/.test(text)) return 'sandbox_check_other';
  return 'unknown';
}
// Intercept at the stream boundary, before Playwright attaches its log collector.
// Each fragment is classified immediately, discarded, and never re-emitted.
function discardStderr(stream, record) {
  if (!stream) return;
  const emit = stream.emit;
  stream.emit = function(event, ...args) {
    if (event !== 'data') return emit.call(this, event, ...args);
    const chunk = args[0];
    if (Buffer.isBuffer(chunk)) {
      for (let i = 0; i < chunk.length; i += 8192) {
        const category = classifySandbox(chunk.subarray(i, i + 8192).toString('utf8'));
        if (category !== 'unknown') record(category);
      }
      chunk.fill(0);
    }
    return true;
  };
  stream.resume();
}
function effectiveFacts(executable, files = fs, uid = process.getuid()) {
  const read = name => {
    let fd;
    try {
      fd = files.openSync(name, 'r');
      const data = Buffer.alloc(4096);
      const n = files.readSync(fd, data, 0, data.length, null);
      return data.subarray(0, n).toString('utf8').trim();
    } catch { return null; } finally { if (fd !== undefined) files.closeSync(fd); }
  };
  const toggle = text => text === null ? 'unavailable' : text === '0' ? 'disabled' : text === '1' ? 'enabled' : 'unknown';
  const max = read('/proc/sys/user/max_user_namespaces');
  const clone = read('/proc/sys/kernel/unprivileged_userns_clone');
  const confinement = read('/proc/self/attr/current');
  let helper = 'unavailable';
  try {
    const info = files.lstatSync(path.join(path.dirname(executable), 'chrome-sandbox'));
    helper = info.isFile() && !info.isSymbolicLink() && info.uid === 0 && (info.mode & 0o7777) === 0o4755 ? 'valid' : 'invalid';
  } catch (error) { if (error.code === 'ENOENT') helper = 'absent'; }
  return {nonroot: uid !== 0,
    apparmor_restriction: toggle(read('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')),
    kernel_userns: max === '0' || clone === '0' ? 'disabled' : max !== null && /^[1-9][0-9]*$/.test(max) && (clone === null || clone === '1') ? 'enabled' : 'unknown',
    apparmor_confinement: confinement === null ? 'unavailable' : confinement === 'unconfined' ? 'unconfined' : /\((?:enforce|complain|kill)\)$/.test(confinement) ? 'confined' : 'unknown',
    setuid_helper: helper};
}
// No shell, mount, networking, or persistence. EPERM/EACCES is evidence of denial,
// not attribution to a specific LSM, seccomp filter, or unit directive.
const USERNS_CODE = `import ctypes,errno,os
libc=ctypes.CDLL(None,use_errno=True)
r=libc.unshare(0x10000000)
e=ctypes.get_errno()
os._exit(0 if r==0 else 2 if e in (errno.EPERM,errno.EACCES) else 3 if e in (errno.ENOSYS,errno.EINVAL) else 4)`;
function primitive(launch, env) {
  return new Promise(resolve => {
    let child;
    try { child = launch('/usr/bin/python3', ['-I', '-S', '-B', '-c', USERNS_CODE], {env, stdio: 'ignore', detached: false}); }
    catch { resolve('unavailable'); return; }
    let grace;
    const finish = value => {
      clearTimeout(timer); clearTimeout(grace);
      child.removeListener('error', error);
      child.removeListener('exit', exit);
      child.removeListener('close', close);
      resolve(value);
    };
    const error = () => finish('unavailable');
    const exit = code => finish(({0: 'success', 2: 'permission_denied', 3: 'unavailable'})[code] || 'unknown');
    const close = () => finish('unknown');
    const timer = setTimeout(() => {
      grace = setTimeout(close, 50);
      try { child.kill('SIGKILL'); } catch { close(); }
    }, 1000);
    child.once('error', error);
    child.once('exit', exit);
    child.once('close', close);
  });
}
// Run only in a disposable thread: even a stalled native filesystem call must
// never block the daemon. No recursive rm is queued against a mutable pathname.
function cleanupOwned({root, directory, rootInfo, ownedInfo, deadline}, files = fs,
  now = () => performance.now()) {
  let rootFd, ownedFd, validated = false;
  const check = () => { if (now() >= deadline - ASSEMBLY_RESERVE_MS) throw Error(); };
  const same = (info, expected) => info.isDirectory() && !info.isSymbolicLink() &&
    info.dev === expected.dev && info.ino === expected.ino;
  const anchor = fd => `/proc/self/fd/${fd}`;
  const open = name => files.openSync(name, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
  // Each open descriptor stays live until its last native operation returns.
  // Renaming/replacing the original pathname cannot redirect outstanding work.
  const empty = fd => {
    check();
    for (const name of files.readdirSync(anchor(fd))) {
      check();
      if (name === '.' || name === '..' || path.basename(name) !== name) throw Error();
      const target = `${anchor(fd)}/${name}`;
      const info = files.lstatSync(target);
      check();
      if (info.isDirectory() && !info.isSymbolicLink()) {
        let nested;
        try {
          nested = open(target);
          if (!same(files.fstatSync(nested), info)) throw Error();
          empty(nested);
          check();
          if (!same(files.lstatSync(target), info)) throw Error();
          check();
          files.rmdirSync(target); // Nonrecursive: never follows a replacement.
        } finally { if (nested !== undefined) files.closeSync(nested); }
      } else {
        check();
        files.unlinkSync(target); // Symlinks are unlinked, never traversed.
      }
    }
  };
  try {
    check();
    if (!rootInfo || !ownedInfo || typeof directory !== 'string' || !directory || directory === root ||
      !path.isAbsolute(root) || path.resolve(root) !== root || path.resolve(directory) !== directory ||
      path.dirname(directory) !== root || !/^sandbox-[A-Za-z0-9]+$/.test(path.basename(directory)) ||
      files.realpathSync(root) !== root || files.realpathSync(directory) !== directory) return 'refused';
    check();
    rootFd = open(root);
    if (!same(files.fstatSync(rootFd), rootInfo)) return 'refused';
    const target = `${anchor(rootFd)}/${path.basename(directory)}`;
    check();
    ownedFd = open(target);
    const current = files.fstatSync(ownedFd);
    if (!same(current, ownedInfo) || current.uid !== process.getuid() || (current.mode & 0o777) !== 0o700) return 'refused';
    validated = true;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        empty(ownedFd);
        check();
        if (!same(files.lstatSync(target), ownedInfo)) return 'refused';
        check();
        files.rmdirSync(target);
        return 'completed';
      } catch { check(); }
    }
    return 'failed';
  } catch { return validated || now() >= deadline - ASSEMBLY_RESERVE_MS ? 'failed' : 'refused'; }
  finally {
    if (ownedFd !== undefined) files.closeSync(ownedFd);
    if (rootFd !== undefined) files.closeSync(rootFd);
  }
}

function boundedCleanup(identity, {cleanupLaunch, now, timers}) {
  return new Promise(resolve => {
    let worker, timer, settled = false, result = 'failed';
    const finish = (value, exited = false) => {
      if (settled) return;
      settled = true;
      timers.clearTimeout(timer);
      if (worker) {
        worker.removeListener('message', message);
        worker.removeListener('error', error);
        worker.removeListener('exit', exit);
        // terminate() can itself wait for a native syscall. Never await it; an
        // unreferenced thread cannot hold daemon shutdown or request completion.
        if (!exited) worker.terminate().catch(() => {});
        worker.unref(); // terminate() may ref the worker; unref must come last.
      }
      resolve(value);
    };
    const message = value => { result = ['completed', 'refused', 'failed'].includes(value) ? value : 'failed'; };
    const error = () => finish('failed');
    const exit = code => finish(code === 0 && now() < identity.deadline - ASSEMBLY_RESERVE_MS ? result : 'failed', true);
    // Leave a small final margin for result assembly on the daemon thread.
    const remaining = identity.deadline - ASSEMBLY_RESERVE_MS - now();
    if (remaining <= 0) { finish('failed'); return; }
    timer = timers.setTimeout(() => finish('failed'), remaining);
    try {
      worker = cleanupLaunch(identity);
      worker.on('message', message);
      worker.on('error', error);
      worker.on('exit', exit);
      if (now() >= identity.deadline - ASSEMBLY_RESERVE_MS) finish('failed');
    } catch { finish('failed'); }
  });
}

class SandboxProbe {
  constructor({launch = fork, files = fs, kill = (pid, signal) => process.kill(-pid, signal),
    cleanupLaunch = identity => new Worker(__filename, {workerData: identity, env: {}, execArgv: []}),
    now = () => performance.now(), timers = {setTimeout, clearTimeout}} = {}) {
    Object.assign(this, {launch, files, kill, cleanupLaunch, now, timers});
  }
  get busy() { return inFlight; }
  async run(backend, signal) {
    const started = this.now();
    if (inFlight) return {ok: false, status: 'busy'};
    inFlight = true;
    try {
      let directory, root, rootInfo, ownedInfo, primary;
      let cleanupClass = 'none';
      let probe = sanitizeProbe({command_accepted: true, child_exit_class: 'none', signal_class: 'none', cleanup_class: 'none', failure_class: 'none'});
      const deadline = started + HARD_TIMEOUT_MS;
      const operationDeadline = deadline - CLEANUP_RESERVE_MS;
      try {
        require('./epoptia_login_apparmor.cjs').verify();
        const {homeEnvironment} = require('./epoptia_login_backend.cjs');
        root = backend.resolveHome(backend.directory);
        const info = this.files.lstatSync(root);
        if (this.files.realpathSync(root) !== root || !info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) throw Error();
        rootInfo = info;
        directory = this.files.mkdtempSync(path.join(root, 'sandbox-'));
        ownedInfo = this.files.lstatSync(directory);
        this.files.chmodSync(directory, 0o700);
        await new Promise(resolve => {
          let child, timer, grace, stopping = false, settled = false, closed = false;
          const settle = () => {
            if (settled) return;
            settled = true;
            this.timers.clearTimeout(timer);
            this.timers.clearTimeout(grace);
            signal?.removeEventListener('abort', cancel);
            if (child) {
              child.removeListener('message', message);
              child.removeListener('error', error);
              child.removeListener('exit', exit);
              child.removeListener('close', close);
              if (child.connected) { try { child.disconnect(); } catch {} }
            }
            resolve();
          };
          const finish = (failure = null) => {
            if (stopping) return;
            stopping = true;
            this.timers.clearTimeout(timer);
            if (failure) { probe.failure_class = failure; probe.clean_close = false; }
            if (failure === 'deadline') probe.timed_out = true;
            // Termination begins before the hard limit, reserving bounded close grace.
            // Only the dedicated group created by this invocation may be signalled.
            if (child?.pid) {
              try { this.kill(child.pid, 'SIGKILL'); }
              catch (error) {
                if (error.code !== 'ESRCH') {
                  probe.clean_close = false;
                  if (probe.failure_class === 'none') probe.failure_class = 'shutdown_failure';
                }
              }
            }
            if (!child || closed) { settle(); return; }
            grace = this.timers.setTimeout(() => {
              probe.clean_close = false;
              if (probe.failure_class === 'none') probe.failure_class = 'shutdown_failure';
              settle();
            }, Math.max(0, Math.min(CLOSE_GRACE_MS, operationDeadline - this.now())));
          };
          const cancel = () => finish('cancelled');
          const message = value => {
            if (stopping) return;
            try {
              const next = sanitizeProbe({...probe, ...value, command_accepted: true});
              const done = value?.done === true;
              probe = next;
              if (done) finish(probe.failure_class === 'B_LOGIN_APPARMOR' ? 'B_LOGIN_APPARMOR' :
                probe.ready ? null : 'readiness_failure');
            } catch { finish('classifier_exception'); }
          };
          const error = () => { if (!stopping) { probe.child_exit_class = 'spawn_error'; finish('spawn_error'); } };
          const exit = (code, childSignal) => {
            if (stopping) return;
            probe.child_exit_class = childSignal ? 'signal' : code === 0 ? 'clean' : 'nonzero';
            probe.signal_class = require('./epoptia_login_backend.cjs').signalClass(childSignal);
            finish(probe.ready ? null : 'readiness_failure');
          };
          const close = () => { closed = true; if (!stopping) finish('readiness_failure'); else settle(); };
          // The same absolute budget includes synchronous profile setup and every
          // worker phase (primitive, launch, dwell, shutdown), without phase resets.
          const remaining = operationDeadline - CLOSE_GRACE_MS - this.now();
          if (remaining <= 0) { finish('deadline'); return; }
          if (signal?.aborted) { cancel(); return; }
          timer = this.timers.setTimeout(() => finish('deadline'), remaining);
          try {
            child = this.launch(path.join(__dirname, 'epoptia_login_sandbox_worker.cjs'), [directory], {
              execArgv: [], detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
              env: {PATH: '/usr/bin:/bin', ...homeEnvironment(root), EPOPTIA_LOGIN_BROWSER_HOME: root,
                EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE: process.env.EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE},
            });
            child.on('message', message);
            child.on('error', error);
            child.on('exit', exit);
            child.on('close', close);
            signal?.addEventListener('abort', cancel, {once: true});
            if (signal?.aborted) cancel();
            else if (this.now() >= operationDeadline - CLOSE_GRACE_MS) finish('deadline');
          } catch { error(); }
        });
      } catch (error) { probe.failure_class = error?.code === 'B_LOGIN_APPARMOR' ? 'B_LOGIN_APPARMOR' : 'unknown'; }
      finally {
        // Freeze all primary evidence before any deletion or cleanup callbacks.
        if (this.now() >= operationDeadline) { probe.timed_out = true; probe.failure_class = 'deadline'; }
        primary = Object.freeze(sanitizeProbe(probe));
        if (directory) {
          // Capture only immutable created-directory identities; no late callback
          // may select another invocation's directory or extend this deadline.
          const identity = Object.freeze({root, directory, deadline,
            rootInfo: rootInfo && Object.freeze({dev: rootInfo.dev, ino: rootInfo.ino}),
            ownedInfo: ownedInfo && Object.freeze({dev: ownedInfo.dev, ino: ownedInfo.ino})});
          cleanupClass = await boundedCleanup(identity, this);
        }
      }
      const elapsed = this.now() - started;
      const elapsed_bucket = primary.timed_out ? 'timeout' : elapsed < 5000 ? 'under_5s' : elapsed < 10000 ? '5_to_10s' : elapsed < 15000 ? '10_to_15s' : '15_to_20s';
      return {ok: primary.ready && primary.clean_close && !primary.timed_out && cleanupClass === 'completed',
        status: 'sandbox_probe_complete', sandbox_probe: {...primary, cleanup_class: cleanupClass, elapsed_bucket}};
    } finally { inFlight = false; }
  }
}
module.exports = {SandboxProbe, sanitizeProbe, classifySandbox, discardStderr, effectiveFacts, primitive,
  ENUMS, BOOLS, OFFLINE_ARGS, HARD_TIMEOUT_MS, OPERATION_MS, CLEANUP_RESERVE_MS, ASSEMBLY_RESERVE_MS, DWELL_MS,
  cleanupOwned, boundedCleanup};
if (!isMainThread && require.main === module) parentPort.postMessage(cleanupOwned(workerData));
