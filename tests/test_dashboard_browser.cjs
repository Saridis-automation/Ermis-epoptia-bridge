// Actual / assets and production API serializer; synthetic input only at reader boundaries.
// Minimal DOM simulation, not a real browser. No network or production cache access.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
// Generate this fixture with the embedded Python block and pipe it to this harness.
const fixtureSource = String.raw`
import asyncio, json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from dashboard.orders import OrderCensus
from dashboard.provider import LocalEpoptiaProvider
from dashboard.server import create_app
now = [datetime(2026, 9, 14, 12, tzinfo=timezone.utc)]
mode = [1]
async def read(tool):
    if (mode[0] == 3 and tool == 'production_overview') or (mode[0] == 4 and tool == 'workstation_wip'):
        raise ValueError('synthetic failure')
    if tool == 'production_overview':
        c = OrderCensus('workorder.deadline', 'synthetic test contract') if mode[0] == 6 else OrderCensus()
        c.consume([dict(id=900+i, production_status='production', target_day='2000-01-01',
            workorder=dict(id=i, code='ORDER-'+str(i), deadline='2026-09-01', progress=(0 if mode[0] == 1 else None if mode[0] == 4 else 60)),
            client=dict(name='Synthetic')) for i in range(1, (2 if mode[0] == 1 else 1 if mode[0] == 5 else 3))])
        return dict(ok=True, dashboard_orders=c.result(True, now[0]))
    return dict(ok=True, complete=True, wol_rows=[dict(id=1, production_status='production', erp_routing=[
        dict(id=i, workstationName='LASER', status=status) for i,status in enumerate(
            ['started'] * mode[0] + ['paused', 'waiting'])])])
import sys
sys.path.insert(0, 'tests')
from test_dashboard_connection import ConnectionTests
boundary = ConnectionTests()
boundary.setUp()
p = boundary.provider  # Actual default direct path; only settings/HTTP fixtures.
p.clock = lambda: now[0]
# Disable only background thread launch; refresh through the real injected reader below.
with patch('dashboard.server.Thread'):
    app = create_app(p, clock=lambda:now[0])
client = app.test_client()
with client.get('/') as response:
    html = response.get_data(as_text=True)
import re
scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
styles = re.findall(r'<link[^>]+href="([^"]+)"', html)
images = re.findall(r'<img[^>]+src="([^"]+)"', html)
assert not re.search(r'<img\b[^>]*id="logo"[^>]*\bhidden', html)
assert '<span id="logo-placeholder" hidden>' in html
assert images == ['/static/saridis-logo-smooth.png']
assets = []
for path in scripts:
    with client.get(path) as response:
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assets.append(response.get_data(as_text=True))
for path in styles:
    with client.get(path) as response:
        assert response.status_code == 200
for path in ['/static/assets/saridis-logo.png', '/static/assets/saridis-logo.png?v=another-cache-key']:
    with client.get(path) as response:
        assert response.status_code == 200
        assert response.mimetype == 'image/png'
        assert response.headers['Cache-Control'] == 'no-store'
        from pathlib import Path
        assert response.data == Path('dashboard/static/assets/saridis-logo.png').read_bytes()
models=[]
times=[]
for value in range(1,7):
    mode[0]=value
    if value in (3,4): now[0] += timedelta(seconds=121)
    boundary.failed_source = 'orders' if value == 3 else 'stations' if value == 4 else None
    boundary.orders = [[dict(id=900+i, production_status='production', target_day='2000-01-01',
        workorder=dict(id=i, code='ORDER-'+str(i), progress=(0 if value == 1 else None if value == 4 else 60)),
        client=dict(name='Synthetic')) for i in range(1, (2 if value == 1 else 1 if value == 5 else 3))], []]
    # Failures are deliberately on page two, so use a valid second page otherwise.
    boundary.orders[1] = boundary.orders[0][-1:]
    if boundary.orders[1]:
        boundary.orders[1] = [dict(boundary.orders[1][0], id=999)]
    boundary.order_count = 2 if boundary.orders[0] else 0
    boundary.stations = [[dict(id=1, production_status='production', erp_routing=[
        dict(id=i, workstationName='LASER', status=status) for i,status in enumerate(
            ['started'] * value + ['paused', 'waiting'])])],
        [dict(id=2+i, production_status=status, erp_routing=[dict(id=1, workstationName='LASER', status='started'), dict(id=2, workstationName='HISTORY ONLY', status='paused')]) for i, status in enumerate(('archive', 'archived', 'completed', 'cancelled', 'canceled'))]]
    if value == 6:
        # Explicit verified-deadline injection remains supported, never the default.
        p.read = read
    asyncio.run(p.snapshot())
    models.append(client.get('/api/dashboard').json)
    times.append(now[0].isoformat())
boundary.doCleanups()
print(json.dumps(dict(html=html, scripts=scripts, assets=assets, models=models, times=times)))
`;
const fixtureInput = fs.readFileSync(0, 'utf8');
assert.ok(fixtureInput.trim(), 'Missing fixture: run sh dashboard/validate.sh');
const fixture = JSON.parse(fixtureInput);
class Element {
  constructor() { this.children = []; this.style = {}; this.dataset = {}; this.listeners = {}; this.value = ''; }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.value = ''; this.children = items; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  get lastElementChild() { return this.children.at(-1); }
}
const elements = new Map([...fixture.html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
const document = {
  getElementById: id => elements.get(id) ?? null,
  createElement: () => new Element(), createElementNS: () => new Element(),
  querySelectorAll: () => [], createTextNode: text => ({textContent: text}),
};
let model = fixture.models[0], fail = false, fetchCount = 0;
const timers = [];
class Clock extends Date {
  static now() { return Date.parse(fixture.times[fixture.models.indexOf(model)]); }
}
const context = vm.createContext({document, AbortController, Date: Clock,
  setTimeout: (fn, delay) => {timers.push({fn, delay}); return timers.length;}, clearTimeout: () => {}, setInterval: () => {},
  fetch: async (url, options) => {
    assert.equal(url, '/api/dashboard'); assert.equal(options.cache, 'no-store'); fetchCount++;
    if (fail) throw new Error('synthetic disconnect');
    return {ok: true, json: async () => model};
  },
});
const visible = id => elements.get(id).textContent;
let cases = 0;
async function test(name, fn) { await fn(); cases++; console.log('PASS ' + name); }
(async () => {
  // Execute complete script(s) loaded by /, including logo, clock and initial polling.
  for (const source of fixture.assets) vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  await test('initial partial payload: real counts, native zero, unknown optional fields', () => {
    assert.equal(model.sources.production_overview.transport, 'direct_python');
    // Station projection reuses the orders worker's HTTP scan, so it need not
    // have a separate transport/read counter of its own.
    assert.equal(model.sources.workstation_wip.shared_wol_scan_id,
      model.canonical_orders.deadline_scan.scan_id);
    assert.ok(model.sources.workstation_wip.shared_wol_scan_id);
    assert.equal(model.sources.production_overview.read_attempts, 1);
    assert.equal(fetchCount, 1); assert.equal(visible('active'), '1'); assert.equal(visible('overall'), '100%');
    assert.match(visible('orders'), /ORDER-1.*Synthetic.*0%.*Μη επαληθευμένη/);
    assert.match(visible('orders-title'), /5 ΠΙΟ ΕΠΕΙΓΟΥΣΕΣ/);
    assert.equal(elements.get('order-ranking').hidden, false);
    assert.match(visible('stations'), /Εκκρεμείς εργασίες 3/);
    assert.doesNotMatch(visible('stations'), /Εκτέλεση|Παύση|μελλοντικά|Άγνωστη κατάσταση/);
    assert.equal(visible('overdue'), '—');
    assert.ok(!elements.has('completed')); assert.ok(!elements.has('completed-status'));
    assert.doesNotMatch(fixture.html, /Ολοκληρώθηκαν σήμερα|Δεν διατίθεται επαληθευμένο ιστορικό ολοκλήρωσης/);
    assert.match(fixture.html, /ΦΟΡΤΟΣ ΠΑΡΑΓΩΓΗΣ/);
    assert.match(fixture.html, /Συνολικά προϊόντα προς παραγωγή<\/dt><dd id="daily-kpi">—/);
    assert.match(visible('overdue-status'), /προθεσμίες/);
    const card = elements.get('stations').children[0];
    assert.equal(card.children.find(child => child.className === 'load').textContent, '100%');
    assert.doesNotMatch(visible('stations'), /HISTORY ONLY/);
    assert.doesNotMatch(visible('orders'), /2000-01-01/);
  });
  await test('unverified production total stays unknown with diagnostics outside the UI', () => {
    const unsupported = {...model, today: {...model.today, production_wols: 999},
      production_wols: {total: 999, status: 'available', reason: 'UNVERIFIED_FLOW_DIAGNOSTIC'}};
    vm.runInContext(`render(${JSON.stringify(unsupported)})`, context);
    assert.equal(visible('daily-kpi'), '—');
    assert.ok(!elements.get('daily-kpi').title);
    assert.doesNotMatch(fixture.html, /UNVERIFIED_FLOW_DIAGNOSTIC|Παραγγελίας εμπορίου|Παραγγελία εμπορίου/);
    for (const id of elements.keys()) assert.doesNotMatch(visible(id), /UNVERIFIED_FLOW_DIAGNOSTIC/);
  });
  await test('scheduled second poll changes visible values', async () => {
    model = fixture.models[1];
    const poll = timers.find(t => t.delay > 5000); assert.ok(poll); await poll.fn();
    assert.equal(visible('active'), '2'); assert.equal(visible('overall'), '40%');
    assert.match(visible('stations'), /Εκκρεμείς εργασίες 4/); assert.match(visible('orders'), /60%/);
  });
  await test('stale failed orders retain last-good while stations update', async () => {
    model = fixture.models[2]; await vm.runInContext('refresh()', context);
    assert.equal(visible('active'), '2'); assert.match(visible('orders'), /60%/);
    assert.match(visible('orders-status'), /Σφάλμα ενημέρωσης.*πριν 2 λεπτά/);
    assert.match(visible('stations'), /Εκκρεμείς εργασίες 5/);
    assert.doesNotMatch(visible('stations-status'), /Σφάλμα|Παλαιά/);
  });
  await test('stale failed stations retain last-good while orders update to unknown progress', async () => {
    model = fixture.models[3]; await vm.runInContext('refresh()', context);
    assert.equal(visible('active'), '2'); assert.equal(visible('overall'), '—');
    assert.doesNotMatch(visible('orders'), /60%/); assert.match(visible('stations'), /Εκκρεμείς εργασίες 5/);
    assert.match(visible('stations-status'), /Σφάλμα ενημέρωσης/);
    assert.doesNotMatch(visible('orders-status'), /Σφάλμα|Παλαιά/);
  });
  await test('verified empty census renders zero, not unknown or fake urgency', async () => {
    model = fixture.models[4]; await vm.runInContext('refresh()', context);
    assert.equal(visible('active'), '0'); assert.equal(visible('overdue'), '0');
    assert.match(visible('orders'), /Δεν υπάρχουν ενεργές/);
    assert.match(visible('orders-title'), /5 ΠΙΟ ΕΠΕΙΓΟΥΣΕΣ/);
  });
  await test('verified deadlines retain existing urgent table semantics', async () => {
    model = fixture.models[5]; await vm.runInContext('refresh()', context);
    assert.match(visible('orders-title'), /ΕΠΕΙΓΟΥΣΕΣ/);
    assert.match(visible('orders'), /ORDER-1.*60%.*01\/09\/2026/);
    assert.equal(visible('overdue'), '2');
    assert.equal(model.urgent_orders[0].deadline, '2026-09-01');
  });
  await test('calendar dates format without timezone shifts; missing dates stay unknown', () => {
    for (const [input, expected] of [['2026-01-02', '02/01/2026'], ['2028-02-29', '29/02/2028'],
      ['2026-12-31', '31/12/2026'], [null, 'Μη επαληθευμένη'], ['', 'Μη επαληθευμένη']]) {
      assert.equal(vm.runInContext(`deadlineLabel(${JSON.stringify(input)})`, context), expected);
    }
  });
  await test('exactly seven approved stations with ten discrete bottom-up blocks and independent stars', () => {
    const synthetic = {...model, workstations: [
      {name:'LASER', pending_steps:7, load_percent:70, last_change_at:null},
      {name:'PUNCHING', pending_steps:20, load_percent:100},
      {name:' εισαγωγή παραγγελίας ', pending_steps:999, load_percent:100},
      {name:'ΠΑΡΑΛΑΒΗ ΠΑΡΑΓΓΕΛΙΑΣ'}, {name:'HISTORY ONLY'}]};
    const before = JSON.stringify(synthetic);
    vm.runInContext(`render(${before})`, context);
    const cards = elements.get('stations').children;
    assert.equal(cards.length, 7);
    assert.deepEqual(cards.map(card => card.children[0].children[0].textContent),
      ['LASER','ΚΟΠΗ ΨΑΛΙΔΙ','ΣΤΡΑΝΤΖΑ','ΜΟΝΤΑΖ 1','ΜΟΝΤΑΖ 2','ΜΟΝΤΑΖ ΤΖΑΜΙΑ','ΨΥΚΤΙΚΑ']);
    for (const card of cards) {
      assert.equal(card.children[0].children[1].children[0].viewBox, '0 0 24 24');
      assert.ok(card.children[0].children[1].children[0].children[0].d);
      assert.ok(card.children[1].children[0].d);
      assert.equal(card.children[0].children[1].dataset.state, 'neutral');
      assert.equal(card.children[1].class, 'station-icon');
      assert.equal(card.children[2].children[0].children.length, 10);
      assert.equal(card.children.length, 4);
    }
    const blocks = cards[0].children[2].children[0].children;
    assert.deepEqual(blocks.map(b => b.dataset.filled), Array(7).fill('true').concat(Array(3).fill('false')));
    assert.equal(cards[0].children[2].children[1].textContent, '70%');
    assert.equal(cards[2].children[2].children[1].textContent, '—');
    assert.equal(JSON.stringify(synthetic), before);
  });
  await test('station unknown, zero and normalized percentage rendering', () => {
    for (const value of [null, undefined, NaN, Infinity, -1, 101, '100', 0, 25, 100]) {
      context.stationValue = value;
      vm.runInContext('render({workstations:[{name:"LASER", pending_steps:7, load_percent:stationValue}]})', context);
      const card = elements.get('stations').children[0];
      const load = card.children[2], bar = load.children[0];
      const valid = typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
      assert.equal(load.children[1].textContent, valid ? `${value}%` : '—');
      assert.equal(bar.children.length, 10);
      assert.equal(bar.children.filter(b => b.dataset.filled === 'true').length, valid ? Math.ceil(value / 10) : 0);
      assert.equal(bar['aria-valuenow'], valid ? String(value) : undefined);
      if (!valid) assert.equal(bar['aria-valuetext'], 'Μη διαθέσιμο');
      assert.match(bar['aria-label'], /Σχετικός αριθμός/);
      assert.equal(card.children[3].children[1].textContent, '7');
    }
  });
  await test('star boundaries, future timestamps and missing history', () => {
    const now = Date.parse('2026-09-14T12:00:00Z');
    for (const [age, expected] of [[-1,'neutral'],[0,'gold'],[3599999,'gold'],[3600000,'neutral'],[7200000,'neutral'],[7200001,'red']]) {
      const at = new Date(now - age).toISOString();
      assert.equal(vm.runInContext(`starState(${JSON.stringify(at)}, ${now})`, context), expected);
    }
    assert.equal(vm.runInContext('starState(null)', context), 'neutral');
    assert.equal(vm.runInContext('starState("invalid")', context), 'neutral');
  });
  await test('invalid load, station alias and distinct welding SVG icons', () => {
    vm.runInContext(`render(${JSON.stringify({...model, workstations:[
      {name:'LASER',load_percent:140,pending_steps:99},
      {name:'ΣΤΡΑΤΖΑ',load_percent:20,pending_steps:2}]})})`, context);
    const cards = elements.get('stations').children;
    assert.equal(cards[0].children[2].children[1].textContent, '—');
    assert.ok(cards[0].children[2].children[0].children.every(block => block.dataset.filled === 'false'));
    assert.equal(cards[0].children[3].children[1].textContent, '99');
    assert.equal(cards[2].children[2].children[1].textContent, '20%');
    assert.notEqual(cards[3].children[1].children[0].d, cards[4].children[1].children[0].d);
    assert.equal(visible('daily-kpi'), '—');
  });
  await test('gold neutral and red stars all render independently of load', () => {
    for (const [minutes, expected] of [[30,'gold'],[60,'neutral'],[120,'neutral'],[121,'red']]) {
      const last_change_at = new Date(Clock.now() - minutes*60000).toISOString();
      vm.runInContext(`render(${JSON.stringify({...model, workstations:[{name:'LASER',load_percent:0,last_change_at}]})})`, context);
      const cards = elements.get('stations').children;
      assert.equal(cards.length, 7);
      assert.equal(cards[0].children[0].children[1].dataset.state, expected);
      assert.ok(cards.every(card => card.children[0].children[1].children[0].children[0].d));
    }
  });
  await test('urgent orders contain progress bars and red overdue dates', () => {
    vm.runInContext(`render(${JSON.stringify(model)})`, context);
    const row = elements.get('orders').children[0];
    assert.equal(row.children[2].children[0].className, 'order-progress');
    assert.equal(row.children[2].children[0].children[0].style.width, '60%');
    assert.equal(row.lastElementChild.className, 'overdue-deadline');
    assert.equal(row.lastElementChild.textContent, '01/09/2026');
  });
  await test('urgent panel caps at five truthful rows and keeps future dates neutral', () => {
    const orders = Array.from({length:7}, (_, index) => ({id:String(index), code:'ORDER-'+index,
      customer:'Synthetic', completion_percent: index === 0 ? null : 0, deadline:'2099-01-01'}));
    vm.runInContext(`render(${JSON.stringify({...model, urgent_orders:orders})})`, context);
    const rendered = elements.get('orders').children;
    assert.equal(rendered.length, 5);
    assert.equal(rendered[0].children[2].textContent, '—');
    assert.equal(rendered[1].children[2].textContent, '0%');
    assert.ok(rendered.every(row => row.lastElementChild.className !== 'overdue-deadline'));
    assert.equal(rendered[0].lastElementChild.textContent, '01/01/2099');
  });
  await test('HH:MM clock, section icons and accurate green-to-red load pointer', () => {
    vm.runInContext('tick()', context);
    assert.match(visible('clock'), /^\d{2}:\d{2}$/);
    assert.match(fixture.html, /warning-icon/); assert.match(fixture.html, /calendar-icon/);
    assert.match(fixture.html, /offset="0%" stop-color="#39c7a4"/);
    assert.match(fixture.html, /offset="50%" stop-color="#eed05a"/);
    assert.match(fixture.html, /offset="100%" stop-color="#f15c4e"/);
    assert.match(fixture.html, /offset="75%" stop-color="#eea343"/);
    assert.match(fixture.html, /0% χαμηλός φόρτος, 100% υψηλός φόρτος/);
    assert.doesNotMatch(fixture.html, /ΜΕΣΗ ΠΡΟΟΔΟΣ/);
    for (const [value, expected] of [[0,100], [38,62], [25.5,74.5], [50,50], [100,0],
      [-1,100], [101,0], [null,null], ['38',null], [undefined,null], [NaN,null], [Infinity,null],
      [-Infinity,null], [true,null], [false,null]]) {
      // Neither a legacy overall value nor station load may replace native
      // progress, even when that progress is missing or invalid.
      context.gaugeModel = {...model, overall_progress_percent:12,
        workstations:[{name:'LASER', load_percent:91, pending_steps:7}],
        native_mean_order_progress_percent:value};
      vm.runInContext('render(gaugeModel)', context);
      assert.equal(visible('overall'), expected == null ? '—' : `${Math.round(expected)}%`);
      assert.equal(elements.get('progress-needle').visibility, expected == null ? 'hidden' : 'visible');
      assert.equal(elements.get('progress-needle').transform, `rotate(${expected == null ? 0 : expected * 1.8} 160 142)`);
      assert.match(elements.get('production-load')['aria-label'], /ΦΟΡΤΟΣ ΠΑΡΑΓΩΓΗΣ/);
      assert.ok(elements.get('production-load')['aria-label'].includes(expected == null ? 'Μη διαθέσιμο' : `${Math.round(expected)}%`));
      assert.equal(context.gaugeModel.native_mean_order_progress_percent, value);
      const card = elements.get('stations').children[0];
      assert.equal(card.children[2].children[1].textContent, '91%');
      assert.equal(card.children[3].children[1].textContent, '7');
    }
  });
  await test('HTTP failure retains displayed data and announces disconnect', async () => {
    const before = visible('stations'); fail = true; await vm.runInContext('refresh()', context);
    assert.equal(visible('stations'), before); assert.equal(visible('active'), '2');
    assert.match(visible('data-status'), /Χωρίς σύνδεση/);
  });
  await test('missing logo fallback is visible and handles image errors', () => {
    elements.get('logo').listeners.error();
    assert.equal(elements.get('logo').src, '/static/assets/saridis-logo.png');
    elements.get('logo').listeners.error();
    assert.equal(elements.get('logo').hidden, true);
    assert.equal(elements.get('logo-placeholder').hidden, false);
  });
  console.log('DOM harness: ' + cases + ' cases passed (not a real browser).');
})().catch(error => {console.error(error); process.exitCode = 1;});
