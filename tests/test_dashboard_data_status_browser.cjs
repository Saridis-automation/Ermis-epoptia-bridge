// Test the real response validation and badge with deterministic synthetic models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const badge = {dataset: {}};
let model;
const context = vm.createContext({
  document: {getElementById: () => badge},
  AbortController, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async () => ({ok: true, json: async () => model})
});
const source = fs.readFileSync('dashboard/static/dashboard.js', 'utf8');
vm.runInContext(source.split('el("logo").addEventListener')[0] + '\nrender = () => {};', context);
(async () => {
  for (const state of ['online', 'partial', 'offline']) {
    model = {schema_version: 1, data_status: state, observed_at: new Date().toISOString()};
    await vm.runInContext('refresh()', context);
    assert.equal(badge.dataset.state, state);
    assert.equal(typeof badge.textContent, 'string');
  }
  for (const status of ['render_required', 'calendar_auth_missing']) {
    model = {schema_version: 1, data_status: 'online', observed_at: new Date().toISOString(),
      calendar_target_dates: {status}, field_status: {target_date: status, deadline: 'available'}};
    await vm.runInContext('refresh()', context);
    assert.equal(badge.dataset.state, 'online');
    assert.equal(vm.runInContext('deadlineLabel("2026-07-31")', context), '31/07/2026');
  }
  console.log('5 dashboard data-status browser checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
