const {test, expect} = require('@playwright/test');
const {
  collectPageErrors,
  currentPath,
  installCommonReadMocks,
  openProtectedPage,
} = require('./helpers');

const DEFAULT_SMOKE_PATHS = [
  '/',
  '/timers',
  '/calendar',
  '/videohub',
  '/foyer-audio',
  '/routing',
  '/pixie',
  '/personal-mixes',
  '/surface-controls',
  '/config',
  '/api-reference',
  '/admin/permissions',
];

const smokePaths = String(process.env.TDECK_SMOKE_PATHS || '')
  .split(',')
  .map(value => value.trim())
  .filter(Boolean);

for (const path of smokePaths.length ? smokePaths : DEFAULT_SMOKE_PATHS) {
  test(`page smoke: ${path}`, async ({page}) => {
    await installCommonReadMocks(page);
    const pageErrors = collectPageErrors(page);
    const failedAssets = [];
    page.on('response', response => {
      const request = response.request();
      if (['script', 'stylesheet', 'image', 'font'].includes(request.resourceType()) && response.status() >= 400) {
        failedAssets.push(`${response.status()} ${response.url()}`);
      }
    });

    const access = await openProtectedPage(page, path);
    test.skip(!access.ok, access.reason);
    await expect(page.locator('#main-content')).toBeVisible();
    expect(pageErrors).toEqual([]);
    expect(failedAssets).toEqual([]);
  });
}

test('an unauthenticated protected page redirects or auth is explicitly disabled', async ({page}) => {
  await installCommonReadMocks(page);
  await page.goto('/routing', {waitUntil: 'domcontentloaded'});
  if (currentPath(page) === '/login') {
    await expect(page.locator('form[action="/login"]')).toBeVisible();
  } else {
    await expect(page.locator('#routing-page')).toBeVisible();
    await expect(page.locator('.dropdown-item-text')).toContainText('Authentication is disabled');
  }
});
