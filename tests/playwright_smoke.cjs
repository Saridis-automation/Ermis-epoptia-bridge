// Local data URL only: no server, external browsing, or page writes.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

process.env.PLAYWRIGHT_BROWSERS_PATH = path.resolve(__dirname, '../.cache/ms-playwright');

async function main() {
  const { chromium } = require('playwright');
  const temporaryDirectory = path.resolve(__dirname, '../.cache/tmp');
  fs.mkdirSync(temporaryDirectory, { recursive: true });
  process.env.TMPDIR = temporaryDirectory;
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ offline: true, serviceWorkers: 'block' });
    await context.route('**/*', route => route.abort());
    const page = await context.newPage();
    await page.goto('data:text/html,<title>Local Chromium smoke test</title>');
    const title = await page.title();
    assert.equal(title, 'Local Chromium smoke test');
    console.log(`PASS: ${title}`);
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  // Print only library names for missing OS dependencies, never launch logs.
  const libraries = [...new Set(String(error.message).match(/\blib[\w.+-]+\.so(?:\.\d+)*/g) || [])];
  console.error(libraries.length ? libraries.join('\n') : 'FAIL: local Chromium smoke test could not complete.');
  process.exitCode = 1;
});
