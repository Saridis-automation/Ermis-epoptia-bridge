'use strict';
// Real installed Chromium, synthetic responses only; never visits Epoptia.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {launchBrowser} = require('../epoptia_browser_runtime.cjs');
const {inspectContext} = require('../epoptia_browser.cjs');
const p = {...require('../epoptia_browser_policy.cjs'), origin: 'https://example.invalid'};
const temp = path.resolve(__dirname, '../.cache/epoptia-browser-smoke');
fs.mkdirSync(temp, {recursive: true, mode: 0o700});
process.env.TMPDIR = temp;
let stage = 'launch';

async function run() {
  const {browser, status} = await launchBrowser();
  if (!browser) {
    console.error(`FAIL: local inspector Chromium smoke test; reason=${status}`);
    process.exitCode = 1;
    return;
  }
  stage = 'inspection';
  try {
    async function scenario(mode) {
      const context = await browser.newContext({serviceWorkers: 'block', acceptDownloads: false});
      const sent = [];
      const original = context.route.bind(context);
      // Substitute the upstream transport only. Real Chromium creates all requests;
      // production routing/DOM extraction and redaction run unchanged.
      context.route = (glob, handler) => original(glob, route => handler(new Proxy(route, {
        get(target, prop) {
          if (prop === 'fetch') return async options => {
            assert.equal(options.maxRedirects, 0);
            const req = route.request();
            sent.push({url: req.url(), method: req.method()});
            const html = `<title>PRIVATE</title><input name="eta" value="PRIVATE">
              <input name="PRIVATE" value="PRIVATE"><table><tr><th>PRIVATE</th></tr></table>
              ${mode === 'login' ? '<input type="password">' : ''}
              <form action="/workorders" method="get"><input name="page" value="1"></form>
              <script>
                fetch('/workorders?page=1');
                const xhr = new XMLHttpRequest(); xhr.open('GET', '/workorderlines'); xhr.send();
                for (const method of ['POST','PUT','PATCH','DELETE'])
                  fetch('/workorders', {method, body:'PRIVATE'}).catch(()=>{});
                fetch('/logout').catch(()=>{});
                fetch('/workorders?token=PRIVATE').catch(()=>{});
                fetch('https://outside.invalid/workorders').catch(()=>{});
                new WebSocket('wss://example.invalid/socket');
                document.querySelector('form').submit();
              </script>`;
            const document = req.resourceType() === 'document';
            const body = Buffer.from(document ? html : JSON.stringify({data: [{eta: 'PRIVATE',
              due_date: 'PRIVATE', token: 'PRIVATE', PRIVATE: 'PRIVATE'}]}));
            return {status: () => mode === 'redirect' ? 302 : 200,
              headers: () => ({'content-type': document ? 'text/html' : 'application/json',
                'content-length': String(body.length), location: 'https://outside.invalid/PRIVATE',
                'set-cookie': 'PRIVATE'}), body: async () => body, dispose: async () => {}};
          };
          const value = Reflect.get(target, prop);
          return typeof value === 'function' ? value.bind(target) : value;
        },
      })));
      const result = await inspectContext(context, 'production_report', p);
      assert.ok(!JSON.stringify(result).includes('PRIVATE'));
      assert.ok(sent.every(r => r.method === 'GET' && r.url.startsWith(p.origin + '/')));
      return {result, sent};
    }
    const {result, sent} = await scenario('normal');
    stage = 'normal_assertions';
    assert.equal(result.status, 'ok');
    assert.deepEqual(result.dom.fields, ['eta']);
    assert.equal(sent.length, 3);
    assert.ok(result.blocked_requests >= 6);
    const responses = result.network.filter(n => n.schema);
    assert.equal(responses.length, 2);
    assert.deepEqual(responses[0].schema.fields.map(f => f.name), ['data', 'due_date', 'eta']);
    stage = 'login';
    assert.equal((await scenario('login')).result.status, 'authentication_required');
    stage = 'redirect';
    const redirect = await scenario('redirect');
    assert.equal(redirect.result.status, 'authentication_required');
    assert.equal(redirect.sent.length, 1);
    console.log('PASS: installed Chromium; GET/XHR/schema/DOM, write blocking, redaction, login and redirect guards');
  } finally { await browser.close(); }
}
run().catch(error => {
  const message = String(error.message);
  const reason = /Operation not permitted|Permission denied/.test(message) ? 'permission_denied' :
    /Executable doesn.t exist/.test(message) ? 'executable_missing' :
    /error while loading shared libraries/.test(message) ? 'library_missing' : 'check_failed';
  console.error(`FAIL: local inspector Chromium smoke test; stage=${stage}; reason=${reason}`);
  process.exitCode = 1;
});
