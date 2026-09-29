'use strict';
// Only reviewed Chromium switches are admitted. Unknown overrides fail closed.
const ARGS = new Set([
  '--disable-quic', '--disable-background-networking', '--disable-sync',
  '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
  '--disable-component-update', '--no-first-run',
  '--host-resolver-rules=MAP * ~NOTFOUND', '--proxy-server=http://offline.invalid:9',
  '--proxy-bypass-list=<-loopback>', '--disable-domain-reliability',
  '--disable-client-side-phishing-detection', '--disable-breakpad', '--no-pings',
]);
const KEYS = new Set(['executablePath', 'headless', 'chromiumSandbox', 'serviceWorkers',
  'timeout', 'env', 'args', 'offline']);
function validate(options) {
  const refuse = () => { throw Object.assign(Error('sandbox_options_rejected'), {code: 'B_LOGIN_SANDBOX'}); };
  // Validate the exact snapshot passed to Playwright, including own properties.
  options = {...options};
  options.args = Array.isArray(options.args) ? [...options.args] : null;
  options.env = {...options.env};
  if (options.chromiumSandbox !== true ||
      Object.keys(options).some(key => !KEYS.has(key)) ||
      !Array.isArray(options.args) || options.args.some(arg =>
        typeof arg !== 'string' || /^--(?:no-sandbox|disable-.*sandbox)(?:=|$)/.test(arg) || !ARGS.has(arg))) refuse();
  // Never inherit browser flags, preload hooks, or wrapper configuration.
  const env = options.env || {};
  if (Object.keys(env).some(key => !/^(PATH|DISPLAY|XAUTHORITY|HOME|TMPDIR|XDG_(CONFIG|CACHE|DATA|STATE|RUNTIME)_HOME|XDG_RUNTIME_DIR)$/.test(key))) refuse();
  return options;
}
function launch(chromium, options, directory) {
  const checked = validate(options);
  return directory === undefined ? chromium.launch(checked) : chromium.launchPersistentContext(directory, checked);
}
module.exports = {validate, launch};
