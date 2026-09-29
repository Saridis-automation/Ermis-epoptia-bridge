'use strict';
const {Scheduler, visibleDenial} = require('./epoptia_browser_scheduler.cjs');
const policy = require('./epoptia_browser_policy.cjs');
const {loadSession, classify} = require('./epoptia_browser.cjs');
const {resolveExecutable} = require('./epoptia_browser_runtime.cjs');

async function launchBrowser() {
  const {chromium} = require('playwright');
  const executable = resolveExecutable(chromium);
  if (executable.status) return {};
  // Parent supplies disposable project-local HOME/TMPDIR/cache directories.
  return {browser: await require('./epoptia_browser_launch.cjs').launch(chromium, {chromiumSandbox: true,executablePath: executable.executablePath,
    headless: true, timeout: 10000, args: ['--disable-background-networking',
      '--disable-component-update', '--no-first-run',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']})};
}

const result = status => ({ok: status === 'authenticated', status});
// Exactly one reviewed document, no assets, API calls, queries or redirects.
function allowed(url, method, type, p = policy) {
  return url === p.origin + p.pages.production_report && type === 'document' &&
    !!classify(url, method, type, p);
}

async function check({p = policy, session = loadSession, launch = launchBrowser,
  authenticatedEvidence = async () => false, scheduler} = {}) {
  let browser, context;
  let status = 'navigation_failed';
  try {
    const saved = session(p);
    if (!saved.state) return result(saved.status === 'auth_material_missing' ?
      'session_missing' : 'access_denied');
    if (!scheduler) return new Scheduler().run(s => check({p, session: () => saved, launch, authenticatedEvidence, scheduler: s}));
    const target = p.origin + p.pages.production_report;
    if (!allowed(target, 'GET', 'document', p)) return result('access_denied');
    ({browser} = await launch());
    if (!browser) return result('navigation_failed');
    context = await browser.newContext({storageState: saved.state,
      javaScriptEnabled: false, serviceWorkers: 'block', acceptDownloads: false,
      permissions: []});
    await context.routeWebSocket('**/*', socket => socket.close());
    const page = await context.newPage();
    let used = false, documentAccepted = false;
    await context.route('**/*', async route => {
      const request = route.request();
      if (request.isNavigationRequest() && request.frame() === page.mainFrame() && request.url() !== target) {
        scheduler.observe(0, true); status = 'login_required'; await route.abort(); return;
      }
      if (scheduler.state.circuit || used || !allowed(request.url(), request.method(), request.resourceType(), p) ||
          !request.isNavigationRequest() || request.frame() !== page.mainFrame()) {
        await route.abort(); return;
      }
      used = true;
      let response;
      try {
        response = await route.fetch({maxRedirects: 0, maxRetries: 0, timeout: 5000});
        const code = response.status();
        const stop = scheduler.observe(code);
        if (stop) { status = stop.status; await route.abort(); return; }
        if (code === 401) status = 'login_required';
        else if (code === 403) status = 'access_denied';
        // Redirect destinations are never inspected or followed.
        if (code !== 200) { await route.abort(); return; }
        const headers = response.headers();
        const size = Number(headers['content-length']);
        if (!Number.isInteger(size) || size < 0 || size > 512 * 1024 ||
            !(headers['content-type'] || '').startsWith('text/html')) {
          await route.abort(); return;
        }
        const body = await response.body();
        if (body.length > 512 * 1024) { await route.abort(); return; }
        await route.fulfill({status: 200, body, headers: {'content-type': 'text/html',
          'x-dns-prefetch-control': 'off',
          'content-security-policy': "default-src 'none'; form-action 'none'; base-uri 'none'; sandbox"}});
        documentAccepted = true;
      } catch (error) {
        status = error?.name === 'TimeoutError' ? 'timeout' : 'navigation_failed';
        await route.abort().catch(() => {});
      } finally { if (response) await response.dispose(); }
    });
    try {
      await scheduler.navigate(() => page.goto(target, {waitUntil: 'domcontentloaded', timeout: 10000}));
      const visibleCode = await visibleDenial(page);
      if (Number.isInteger(visibleCode) && scheduler.observe(visibleCode)) status = 'circuit_open';
      else if (await page.locator('input[autocomplete="one-time-code"]').count()) status = 'mfa_required';
      else if (await page.locator('input[type="password"]').count()) { status = 'login_required'; scheduler.observe(0, true); }
      // Only reviewed, positive evidence may establish authentication. The
      // production default remains false: no evidence is configured today.
      else if (documentAccepted && await authenticatedEvidence(page, p) === true)
        status = 'authenticated';
    } catch (error) {
      if (error?.name === 'TimeoutError') status = 'timeout';
    }
  } catch (error) {
    status = error?.name === 'TimeoutError' ? 'timeout' : 'navigation_failed';
  } finally {
    try { if (context) await context.close(); }
    catch { status = 'navigation_failed'; }
    try { if (browser) await browser.close(); }
    catch { status = 'navigation_failed'; }
  }
  if (scheduler?.state.circuit) return scheduler.observe(0);
  return result(status);
}

module.exports = {allowed, check};
if (require.main === module) {
  check().then(value => process.stdout.write(JSON.stringify(value)))
    .catch(() => process.stdout.write(JSON.stringify(result('navigation_failed'))));
}
