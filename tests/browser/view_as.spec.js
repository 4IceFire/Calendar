const {test, expect} = require('@playwright/test');
const {collectPageErrors} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  const probe = await request.get('/__permissions_fixture__/health');
  const body = await probe.json().catch(() => ({}));
  test.skip(!probe.ok() || body.fixture !== 'tdeck-view-as-ui', 'Run tests/permissions_ui_harness.py --view-as on port 5065 with workers=1.');
  expect((await request.post('/__permissions_fixture__/reset')).ok()).toBe(true);
  await page.goto('/login?next=/admin/users/2');
  await page.getByLabel('Username', {exact: true}).fill('fixture-admin');
  await page.getByLabel('Password', {exact: true}).fill('fixture-password');
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await expect(page).toHaveURL(/\/admin\/users\/2$/);
});

async function beginViewAs(page) {
  await page.getByRole('button', {name: 'View as user', exact: true}).click();
  await expect(page).toHaveURL(/\/routing$/);
  const banner = page.locator('#view-as-banner');
  await expect(banner).toContainText('Viewing as media-operator');
  await expect(banner).toContainText('Actions are live');
  return banner;
}

test('View as applies target access, keeps the banner visible, and returns to Admin', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  const banner = await beginViewAs(page);
  await expect(page.locator('#routing-outputs button')).toHaveCount(1);
  await expect(page.locator('#routing-outputs')).toContainText('Foyer');
  await expect(page.locator('a[href="/config"]')).toHaveCount(0);

  const denied = await page.goto('/config/atem-media');
  expect(denied.status()).toBe(403);
  await expect(banner).toBeVisible();
  await page.goto('/media?output=1');
  await expect(page.getByRole('button', {name: 'Display Welcome', exact: true})).toBeVisible();
  await page.reload();
  await expect(banner).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const bounds = await banner.boundingBox();
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize().height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('view-as-media.png'), fullPage: true});

  await page.getByRole('button', {name: 'Return to admin', exact: true}).click();
  await expect(page).toHaveURL(/\/admin\/users\/2$/);
  await expect(banner).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'View as user', exact: true})).toBeVisible();
  const restored = await page.goto('/config/atem-media');
  expect(restored.status()).toBe(200);
  expect(errors).toEqual([]);
});

test('A test user can upload and display media using their granted permissions', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await beginViewAs(page);
  const listing = await page.request.get('/api/media');
  expect(listing.ok()).toBe(true);
  const image = (await listing.json()).items[0];
  const preview = await page.request.get(image.thumbnail_url);
  expect(preview.ok()).toBe(true);

  await page.goto('/media/upload?output=1');
  await page.locator('#media-upload-file').setInputFiles({name: 'phone-photo.png', mimeType: 'image/png', buffer: await preview.body()});
  await page.locator('#media-upload-name').fill('View as test image');
  await expect(page.locator('#media-upload-preview')).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('view-as-upload.png'), fullPage: true});
  const uploadFinished = page.waitForResponse(response => response.url().endsWith('/api/media/upload') && response.request().method() === 'POST');
  await page.getByRole('button', {name: 'Upload and display', exact: true}).click();
  expect((await uploadFinished).status()).toBe(201);
  await expect(page).toHaveURL(/\/routing\?media_job=/);
  await expect(page.locator('#view-as-banner')).toBeVisible();
  const saved = await page.request.get('/api/media');
  expect((await saved.json()).items.some(item => item.name === 'View as test image')).toBe(true);
  await page.getByRole('button', {name: 'Return to admin', exact: true}).click();
  await expect(page).toHaveURL(/\/admin\/users\/2$/);
  expect(errors).toEqual([]);
});
