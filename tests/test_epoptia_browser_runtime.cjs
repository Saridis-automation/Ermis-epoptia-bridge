'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {load} = require('./login_test_loader.cjs');
const runtime = require('../epoptia_browser_runtime.cjs');
test('shared resolver never uses package executable or caller supplied filesystem', () => {
  const aa = load('epoptia_login_apparmor.cjs');
  const subject = load('epoptia_browser_runtime.cjs', {'./epoptia_login_apparmor.cjs': aa});
  const forbidden = new Proxy({}, {get() { assert.fail('fallback accessed'); }});
  assert.throws(() => subject.resolveExecutable(forbidden, forbidden), {code: 'B_LOGIN_APPARMOR'});
});
test('bounded runtime categories', () => {
  assert.equal(runtime.reason({code: 'B_LOGIN_APPARMOR'}), 'B_LOGIN_APPARMOR');
  assert.equal(runtime.reason(Error('Operation not permitted PRIVATE')), 'runtime_permission_denied');
  assert.equal(runtime.reason(Error('PRIVATE')), 'launch_failed');
  assert.equal(runtime.reason(Error('error while loading shared libraries: PRIVATE')), 'chromium_dependencies_missing');
});
test('missing Playwright remains distinct', async () => {
  assert.deepEqual(await runtime.launchBrowser({load: () => {
    throw Object.assign(Error('PRIVATE'), {code: 'MODULE_NOT_FOUND'});
  }}), {status: 'playwright_missing'});
});
test('launch uses explicit test-only resolver fixture and validates temp directory', async () => {
  const subject = load('epoptia_browser_runtime.cjs', {
    './epoptia_login_apparmor.cjs': {verify: () => ({executablePath: '/SYNTHETIC'})},
  });
  const browser = {};
  let launched = 0;
  const loader = () => ({chromium: {executablePath: () => assert.fail('package fallback'),
    launch: async options => { launched++; assert.equal(options.executablePath, '/SYNTHETIC'); return browser; }}});
  const io = {mkdirSync() {}, lstatSync: () => ({isDirectory: () => true,
    isSymbolicLink: () => false, uid: process.getuid(), mode: 0o700})};
  assert.deepEqual(await subject.launchBrowser({load: loader, io}), {browser});
  assert.equal(launched, 1);
  assert.deepEqual(await subject.launchBrowser({load: loader, io: {...io,
    lstatSync: () => ({isDirectory: () => false})}}), {status: 'runtime_permission_denied'});
  assert.equal(launched, 1);
});
