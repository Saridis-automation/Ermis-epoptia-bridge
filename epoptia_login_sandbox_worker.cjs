'use strict';
// Dedicated disposable worker: no session store, scheduler, or login supervisor.
const cp = require('node:child_process');
const {sanitizeProbe, discardStderr, effectiveFacts, primitive, DWELL_MS} = require('./epoptia_login_sandbox.cjs');
async function run() {
  const probe = sanitizeProbe({sandbox_class: 'unknown', failure_class: 'none', child_exit_class: 'none', signal_class: 'none'});
  const send = done => { if (process.connected) process.send({...sanitizeProbe(probe), done}); };
  let executable;
  const originalSpawn = cp.spawn;
  const observed = [];
  // Patch only inside this fresh worker, before loading Backend. Playwright uses
  // the same child_process object. Disable new process groups solely to keep all
  // probe descendants inside the parent's killable group; sandbox flags stay on.
  cp.spawn = (bin, args, options) => {
    const child = originalSpawn(bin, args, {...options, detached: false});
    const browser = bin === executable?.executablePath;
    discardStderr(child.stderr, category => {
      if (browser && ['unknown', 'sandbox_check_other'].includes(probe.sandbox_class)) {
        probe.sandbox_class = category; send(false);
      }
    });
    if (browser) {
      probe.spawn_returned = true; send(false);
      const error = () => { probe.child_exit_class = 'spawn_error'; send(false); };
      const exit = (code, signal) => {
        probe.child_exit_class = signal ? 'signal' : code === 0 ? 'clean' : 'nonzero';
        probe.signal_class = require('./epoptia_login_backend.cjs').signalClass(signal);
        send(false);
      };
      child.once('error', error);
      child.once('exit', exit);
      observed.push({child, error, exit});
    }
    return child;
  };
  let watchedPage;
  let healthy = true;
  const unhealthy = () => { healthy = false; };
  try {
    try { executable = require('./epoptia_login_apparmor.cjs').verify(); }
    catch { probe.failure_class = 'B_LOGIN_APPARMOR'; send(true); return; }
    if (executable.status) { send(true); return; }
    Object.assign(probe, effectiveFacts(executable.executablePath));
    const {Backend, homeEnvironment, MANAGED_HOME} = require('./epoptia_login_backend.cjs');
    const directory = process.argv.length === 3 ? process.argv[2] : null;
    const chromium = require('playwright').chromium;
    const backend = new Backend({chromium});
    try {
      probe.user_namespace = await primitive(cp.spawn, {PATH: '/usr/bin:/bin', ...homeEnvironment(MANAGED_HOME)});
      send(false);
      const surface = await backend.start({host: '127.0.0.1', offline: true, probeDirectory: directory});
      probe.ready = true; send(false);
      watchedPage = surface.page;
      watchedPage.once('crash', unhealthy);
      watchedPage.once('close', unhealthy);
      await new Promise(resolve => setTimeout(resolve, DWELL_MS));
      if (!healthy || surface.page.isClosed()) throw Error();
      await backend.context.close();
      await backend.stop();
      probe.clean_close = probe.child_exit_class === 'clean';
      if (probe.clean_close && probe.sandbox_class === 'unknown') probe.sandbox_class = 'none';
    } catch { await backend.stop(); }
    send(true);
  } finally {
    watchedPage?.removeListener('crash', unhealthy);
    watchedPage?.removeListener('close', unhealthy);
    for (const {child, error, exit} of observed) {
      child.removeListener('error', error);
      child.removeListener('exit', exit);
    }
    cp.spawn = originalSpawn;
  }
}
module.exports = {run};
if (require.main === module) run().catch(() => {
  if (process.connected) process.send({...sanitizeProbe({}), done: true});
});
