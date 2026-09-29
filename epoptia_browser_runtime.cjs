'use strict';
const fs = require('node:fs');
const path = require('node:path');

// Never serialize browser exceptions: they may contain paths or process details.
function reason(error) {
  if (error?.code === 'B_LOGIN_APPARMOR') return 'B_LOGIN_APPARMOR';
  const message = String(error?.message || '');
  if (['EACCES', 'EPERM'].includes(error?.code) ||
      /permission denied|operation not permitted|EACCES|EPERM/i.test(message)) {
    return 'runtime_permission_denied';
  }
  if (/error while loading shared libraries/i.test(message)) return 'chromium_dependencies_missing';
  return 'launch_failed';
}

// All callers share the installed unit's receipt-bound selection; no discovery.
function resolveExecutable() {
  return require('./epoptia_login_apparmor.cjs').verify();
}

async function launchBrowser({load = () => require('playwright'), io = fs} = {}) {
  let chromium;
  try { ({chromium} = load()); }
  catch (error) {
    return {status: error.code === 'MODULE_NOT_FOUND' ? 'playwright_missing' : reason(error)};
  }
  try {
    const executable = resolveExecutable(chromium, io);
    if (executable.status) return executable;
    // Use a private project-local temp directory, independent of service HOME/TMPDIR.
    const cache = path.join(__dirname, '.cache');
    const temp = path.join(cache, 'epoptia-browser-tmp');
    for (const dir of [cache, temp]) {
      try { io.mkdirSync(dir, {mode: 0o700}); }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      const stat = io.lstatSync(dir);
      if (!stat.isDirectory() || stat.isSymbolicLink() || stat.uid !== process.getuid() ||
          (stat.mode & (dir === temp ? 0o077 : 0o022))) {
        return {status: 'runtime_permission_denied'};
      }
    }
    process.env.TMPDIR = temp;
    const browser = await require('./epoptia_browser_launch.cjs').launch(chromium, {chromiumSandbox: true,executablePath: executable.executablePath,
      headless: true, args: ['--disable-background-networking',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']});
    return {browser};
  } catch (error) { return {status: reason(error)}; }
}

module.exports = {reason, resolveExecutable, launchBrowser};
