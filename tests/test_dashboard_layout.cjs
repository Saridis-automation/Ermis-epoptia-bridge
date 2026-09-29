// Local assets and synthetic data only; never connects to dashboard services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const css = fs.readFileSync('dashboard/static/dashboard.css', 'utf8');
const html = fs.readFileSync('dashboard/static/index.html', 'utf8');
const script = fs.readFileSync('dashboard/static/dashboard.js', 'utf8').split('el("logo").addEventListener')[0];
assert.match(css, /width:1920px;height:1080px/);
assert.match(css, /\.viewport\{position:fixed;inset:0;overflow:hidden\}/);
assert.match(css, /html,body\{[^}]*overflow:hidden/);
assert.match(css, /grid-template-columns:repeat\(7,minmax\(0,1fr\)\)/);
assert.match(css, /flex-direction:column-reverse/);
assert.match(css, /data-level="10"/);
assert.doesNotMatch(css, /@media|overflow-x:auto/);
assert.match(css, /table-layout:fixed/);
assert.match(css, /grid-template-columns:repeat\(3,minmax\(0,1fr\)\)/);
assert.ok(html.indexOf('ΦΟΡΤΟΣ ΠΑΡΑΓΩΓΗΣ') < html.indexOf('class="gauge"'));
// Resolve the fixed canvas's vertical budget from the actual CSS. This also runs
// without Playwright; the browser branch below verifies rendered geometry.
const rule = selector => {
  assert.ok(css.includes(selector + '{'), `Missing CSS rule: ${selector}`);
  const body = css.slice(css.indexOf(selector + '{') + selector.length + 1).split('}')[0];
  return Object.fromEntries(body.split(';').filter(Boolean).map(item => item.split(':')));
};
const px = value => { assert.match(value, /^\d+px$/); return parseInt(value, 10); };
const main = rule('main'), station = rule('.station'), stations = rule('.stations');
const rows = /^(\d+)px minmax\(0,1fr\) (\d+)px$/.exec(main['grid-template-rows']);
assert.ok(rows);
const heading = rule('.section-heading');
const stationArea = px(main.height) - 2 * px(main.padding) - 2 * px(main.gap) - Number(rows[1]) - Number(rows[2]);
const cardHeight = stationArea - px(heading['line-height']) - px(heading['margin-bottom']) - px(stations['margin-bottom']);
const cardRows = /^(\d+)px (\d+)px minmax\((\d+)px,1fr\) (\d+)px$/.exec(station['grid-template-rows']);
assert.ok(cardRows);
const minimumCardHeight = cardRows.slice(1).reduce((sum, n) => sum + Number(n), 0) +
  3 * px(station['row-gap']) + 2 * px(station.padding.split(' ')[0]) + 2 * parseInt(station.border, 10);
assert.ok(minimumCardHeight <= cardHeight, 'Card tracks and padding must fit without expanding into bottom panels');
assert.ok(px(rule('.bar').height) <= Number(cardRows[3]) + Number(cardRows[4]) + px(station['row-gap']));
assert.ok(px(rule('.station-icon').height) <= Number(cardRows[2]));
assert.ok(cardHeight <= 310, 'Cards remain compact');
assert.ok(px(main.gap) + px(stations['margin-bottom']) >= 32, 'Reserve at least 32 canvas pixels below cards');
assert.equal(rule('th:nth-child(1)').width, '18%');
assert.equal(rule('th:nth-child(2)').width, '36%');
console.log('PASS fixed-canvas card height, content budget and bottom-panel separation (1 case)');
// REV4 presentation budgets: protect the enlarged dial/type and rightward
// header allocation, as well as the compact icons and raised segmented bars.
const gauge = rule('.gauge'), header = rule('header');
assert.ok(px(gauge.width) >= 400 && px(gauge.height) >= 220);
assert.equal(header['grid-template-columns'], '440px 320px minmax(0,1fr) 380px');
assert.equal(rule('.logo')['justify-items'], 'end');
assert.equal(px(rule('.logo img')['max-width']), 350);
assert.ok(px(rule('#clock')['font-size']) >= 90);
assert.ok(px(rule('.panel h2')['font-size']) >= 28);
assert.ok(px(rule('.station-icon').width) <= 56);
assert.ok(px(rule('.station-icon').height) <= 56);
assert.equal(rule('.bar')['align-self'], 'start');
assert.equal(rule('.bar')['grid-row'], '3/5');
const overall = rule('.overall'), title = rule('.overall>span');
const headerContentHeight = Number(rows[1]) - px(header['padding-bottom']) - parseInt(header['border-bottom'], 10);
assert.ok(px(title['font-size']) * Number(title['line-height']) + px(overall.gap) + px(gauge.height) <= headerContentHeight,
  'Enlarged gauge and title must fit above the header divider');
assert.match(html, /<span id="load-title">ΦΟΡΤΟΣ ΠΑΡΑΓΩΓΗΣ<\/span>/);
for (const [group, count] of [['dial-ticks', 11], ['dial-minor-ticks', 10]]) {
  const markup = html.match(new RegExp(`<g class="${group}">([\\s\\S]*?)</g>`));
  assert.ok(markup, `Missing ${group}`);
  assert.equal((markup[1].match(/<path\b/g) || []).length, count);
  assert.notEqual(rule('.' + group).stroke, 'none');
  assert.ok(Number(rule('.' + group)['stroke-width']) >= 1);
}
assert.match(html, /M 34 142 A 126 126 0 0 1 286 142/);
assert.match(html, /id="progress-needle"[^>]*>[\s\S]*?L48 142/);
console.log('PASS REV4 gauge, ticks, header spacing, typography and raised bars (1 case)');
const canvas = {style:{}};
const listeners = {};
const context = vm.createContext({window:{innerWidth:1920, innerHeight:1080,
  addEventListener:(name, fn) => {listeners[name] = fn;}},
  document:{getElementById:() => canvas, addEventListener:(name, fn) => {listeners[name] = fn;}}});
vm.runInContext(script, context);
const viewports = [[1920,1080],[1440,900],[900,1080],[3840,2160],[2560,1080],[375,812]];
for (const [width,height] of viewports) {
  context.window.innerWidth = width; context.window.innerHeight = height;
  listeners.resize();
  const scale = Math.min(width/1920,height/1080);
  assert.equal(canvas.style.transform, `translate(-50%, -50%) scale(${scale})`);
  assert.ok(1920*scale <= width && 1080*scale <= height);
  canvas.style.transform = '';
  listeners.fullscreenchange();
  assert.ok(canvas.style.transform.endsWith(`scale(${scale})`));
}
console.log('PASS factory canvas, SVG header, seven-column structure and resize/fullscreen contract (1 case)');
let chromium;
try { ({chromium} = require('playwright')); }
catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND') throw error;
  console.log('SKIP rendered-browser layout (1 case): Playwright unavailable');
  process.exit(0);
}
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'https://dashboard.test') return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType:'text/html',
        body:html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'')});
      if (url.pathname === '/static/saridis-logo-smooth.png') return route.fulfill({contentType:'image/png',
        body:fs.readFileSync('dashboard/static/saridis-logo-smooth.png')});
      return route.abort();
    });
    await page.goto('https://dashboard.test/');
    await page.addStyleTag({content:css});
    await page.addScriptTag({content:script});
    await page.evaluate(() => {
      render({canonical_orders:{canonical_version:2,complete:true}, today:{active_work:123,overdue_work:42},
        urgent_orders:Array.from({length:5}, (_,i) => ({id:String(i),code:'ORDER-'+i,
          customer:'ΜΕΓΑΛΗ ΕΠΩΝΥΜΙΑ '.repeat(5),native_progress:65,deadline:'2026-01-01'})),
        workstations:['LASER','ΚΟΠΗ ΨΑΛΙΔΙ','ΣΤΡΑΝΤΖΑ','ΜΟΝΤΑΖ 1','ΜΟΝΤΑΖ 2','ΜΟΝΤΑΖ ΤΖΑΜΙΑ','ΨΥΚΤΙΚΑ']
          .map(name => ({name,pending_steps:999,load_percent:100}))});
      tick();
    });
    for (const [width,height] of viewports) {
      await page.setViewportSize({width,height});
      await page.evaluate(() => {document.dispatchEvent(new Event('fullscreenchange'));});
      const result = await page.evaluate(() => {
        const canvas = document.getElementById('factory-canvas');
        const rect = canvas.getBoundingClientRect();
        return {left:rect.left,top:rect.top,right:rect.right,bottom:rect.bottom,
          width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,
          overflows:[canvas,...document.querySelectorAll('header,.station,.panel,.stations')]
            .filter(item => item.scrollWidth > item.clientWidth+1 || item.scrollHeight > item.clientHeight+1)
            .map(item => item.className || item.id),
          separation:Math.min(...[...document.querySelectorAll('.station')].map(card =>
            (document.querySelector('.bottom').getBoundingClientRect().top - card.getBoundingClientRect().bottom) / (rect.width / 1920))),
          cardContentsContained:[...document.querySelectorAll('.station')].every(card =>
            [...card.querySelectorAll('.station-icon,.bar,.pending,.load>strong')].every(child =>
              child.getBoundingClientRect().bottom <= card.getBoundingClientRect().bottom)),
          stars:document.querySelectorAll('.priority svg').length,
          icons:document.querySelectorAll('.station-icon path').length,
          urgentRows:document.querySelectorAll('#orders tr').length,
          sectionIcons:document.querySelectorAll('.section-icon').length,
          clock:document.getElementById('clock').textContent,
          kpi:document.getElementById('daily-kpi').textContent};
      });
      assert.ok(result.left >= -.1 && result.top >= -.1 && result.right <= width+.1 && result.bottom <= height+.1);
      assert.ok(result.width <= width && result.height <= height);
      assert.deepEqual(result.overflows, []);
      assert.ok(result.separation >= 31.9, 'Cards must stay at least 32 canvas pixels above bottom panels');
      assert.ok(result.cardContentsContained, 'Card content must fit inside each card');
      assert.equal(result.urgentRows,5); assert.equal(result.sectionIcons,2);
      assert.match(result.clock, /^\d{2}:\d{2}$/);
      assert.equal(result.stars,7); assert.equal(result.icons,7); assert.equal(result.kpi,'—');
    }
    assert.equal(await page.locator('#logo').evaluate(img => img.naturalWidth),1047);
    assert.equal(await page.locator('.motto').evaluate(item => getComputedStyle(item).fontStyle),'italic');
    console.log('PASS rendered-browser layout, logo and seven cards across six viewports (1 case)');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
