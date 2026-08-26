const {expect} = require('@playwright/test');

async function installCommonReadMocks(page) {
  await page.route('**/api/status/summary**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ok: true,
      companion: {connected: false},
      propresenter: {connected: false},
      videohub: {connected: false},
      digico: {connected: false},
      atem: {connected: false},
      pixie: {connected: false},
    }),
  }));
  await page.route('**/api/activity-log/alerts**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ok: true, count: 0, failures: 0, warnings: 0}),
  }));
}

function currentPath(page) {
  try {
    return new URL(page.url()).pathname;
  } catch (error) {
    return '';
  }
}

async function openProtectedPage(page, path) {
  let response = await page.goto(path, {waitUntil: 'domcontentloaded'});
  if (currentPath(page) === '/login') {
    const username = process.env.TDECK_USERNAME;
    const password = process.env.TDECK_PASSWORD;
    if (!username || !password) {
      return {
        ok: false,
        reason: 'Authentication is enabled; set TDECK_USERNAME and TDECK_PASSWORD for browser tests.',
      };
    }
    await page.locator('input[name="username"]').fill(username);
    await page.locator('input[name="password"]').fill(password);
    await Promise.all([
      page.waitForNavigation({waitUntil: 'domcontentloaded'}),
      page.getByRole('button', {name: 'Sign in'}).click(),
    ]);
    if (currentPath(page) === '/login') {
      throw new Error('TDeck browser-test login failed; verify the credentials and account state.');
    }
    if (currentPath(page) !== path) {
      response = await page.goto(path, {waitUntil: 'domcontentloaded'});
    }
  }
  if (response && response.status() === 403) {
    return {ok: false, reason: `The browser-test account does not have access to ${path}.`};
  }
  await expect(page.locator('#main-content')).toBeVisible();
  return {ok: true};
}

function collectPageErrors(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(String(error && error.message || error)));
  return errors;
}

module.exports = {
  collectPageErrors,
  currentPath,
  installCommonReadMocks,
  openProtectedPage,
};
