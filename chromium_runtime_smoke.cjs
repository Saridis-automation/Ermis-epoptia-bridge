'use strict';
const {resolveExecutable, reason} = require('./epoptia_browser_runtime.cjs');

async function smoke({load = () => require('playwright'), resolve = resolveExecutable} = {}) {
  let browser;
  let status = 'launch_failed';
  try {
    let chromium;
    try { ({chromium} = load()); }
    catch (error) {
      return {status: error.code === 'MODULE_NOT_FOUND' ? 'playwright_missing' : reason(error)};
    }
    const executable = resolve(chromium);
    if (executable.status) return {status: executable.status};
    browser = await require('./epoptia_browser_launch.cjs').launch(chromium, {chromiumSandbox: true,executablePath: executable.executablePath,
      headless: true, timeout: 10000,
      args: ['--disable-background-networking', '--disable-component-update',
        '--no-first-run', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']});
    const context = await browser.newContext({offline: true, serviceWorkers: 'block'});
    await context.route('**/*', route => route.abort());
    await context.routeWebSocket('**/*', socket => socket.close());
    const page = await context.newPage();
    await page.goto('about:blank', {timeout: 5000});
    status = page.url() === 'about:blank' ? 'completed' : 'launch_failed';
  } catch (error) { status = reason(error); }
  finally {
    if (browser) {
      try { await browser.close(); }
      catch { status = 'launch_failed'; }
    }
  }
  return {status};
}

if (require.main === module) {
  // Parent enforces the total deadline, kills the process group and cleans temp files.
  smoke().then(result => process.stdout.write(JSON.stringify(result)))
    .catch(() => process.stdout.write('{"status":"launch_failed"}'));
}
module.exports = {smoke};
