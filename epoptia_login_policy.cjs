'use strict';
const origin = 'https://app.epoptia.com';
const authenticatedPaths = Object.freeze(['/dashboard']);
// Candidate contract only: finalize checks visibility and link target at action time.
const shellSelector = 'nav a[href="/logout"]';
function sameOrigin(raw) {
  try {
    const u = new URL(raw);
    return u.origin === origin && !u.username && !u.password &&
      raw.startsWith(origin + '/') && !raw.slice(origin.length).startsWith('//');
  } catch { return false; }
}
function redirectAllowed(location, base) {
  if (!sameOrigin(base)) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(location)) return sameOrigin(location);
  if (location.startsWith('//')) return sameOrigin('https:' + location);
  try { return sameOrigin(new URL(location, base).href); } catch { return false; }
}
async function authenticated(surface) {
  const page = surface.page;
  const valid = () => sameOrigin(page.url()) && authenticatedPaths.includes(new URL(page.url()).pathname);
  if (surface.denied || !valid()) return false;
  if (await page.locator('input[type="password"]').isVisible()) return false;
  const shell = page.locator(shellSelector);
  return await shell.count() === 1 && await shell.isVisible() && valid() && !surface.denied;
}
module.exports = {origin, authenticatedPaths, shellSelector, sameOrigin, redirectAllowed, authenticated};
