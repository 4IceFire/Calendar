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
  await expect(page.getByRole('button', {name: 'View as user', exact: true})).toBeVisible();
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

for (const mode of ['view-as', 'sign-in']) {
  test(`Single input/output restrictions survive a separate Media group (${mode})`, async ({page}, testInfo) => {
    const errors = collectPageErrors(page);
    await page.goto('/admin/permissions?tab=groups#role-68');
    const form = page.locator('[data-role-form][data-role-id="68"]');
    await form.getByRole('tab', {name: 'Routing', exact: true}).click();
    for (const [field, value] of [['outputs', '26'], ['inputs', '13']]) {
      const saved = page.waitForResponse(response => response.url().endsWith('/api/admin/groups/68')
        && response.request().method() === 'POST'
        && response.request().postDataJSON()[`videohub_allowed_${field}_role`] === value);
      await form.locator(`input[name="videohub_allowed_${field}_role"]`).fill(value);
      expect((await saved).ok()).toBe(true);
    }
    await page.route('**/api/videohub/state', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ok: true, configured: true,
        inputs: Array.from({length: 40}, (_, i) => ({number: i + 1, label: `Input ${i + 1}`})),
        outputs: Array.from({length: 40}, (_, i) => ({number: i + 1, label: `Output ${i + 1}`})),
        routing: Array.from({length: 40}, () => 13),
      }),
    }));
    if (mode === 'view-as') {
      await page.goto('/admin/users/2');
      await beginViewAs(page);
    } else {
      await page.goto('/logout');
      await page.goto('/login');
      await page.getByLabel('Username', {exact: true}).fill('media-operator');
      await page.getByLabel('Password', {exact: true}).fill('fixture-password');
      await page.getByRole('button', {name: 'Sign in', exact: true}).click();
      await expect(page).toHaveURL(/\/routing$/);
    }
    await expect(page.locator('#routing-outputs button')).toHaveText(['26: Output 26']);
    await page.locator('#routing-outputs button').click();
    await expect(page.locator('#routing-inputs button')).toHaveText(['13: Input 13']);
    await expect(page.locator('#routing-media-choice')).toBeVisible();
    await page.screenshot({path: testInfo.outputPath('single-routing-port.png'), fullPage: true});
    await page.locator('#routing-media-choice').click();
    await expect(page).toHaveURL(/\/media\?output=26$/);
    await expect(page.getByRole('button', {name: 'Display Welcome', exact: true})).toBeVisible();
    await expect(page.locator('#media-upload-link')).toBeVisible();
    expect(errors).toEqual([]);
  });
}
