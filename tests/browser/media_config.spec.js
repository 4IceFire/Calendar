const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks, openProtectedPage} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  const probe = await request.get('/__media_fixture__/health');
  const body = await probe.json().catch(() => ({}));
  test.skip(!probe.ok() || body.fixture !== 'tdeck-media-ui', 'Run tests/media_ui_harness.py on port 5063 for Media write tests.');
  await installCommonReadMocks(page);
});

async function openConfig(page) {
  const access = await openProtectedPage(page, '/config/atem-media');
  expect(access.ok, access.reason).toBe(true);
  await expect(page.locator('#media-config-page')).toBeVisible();
  await expect(page.locator('#media-connection-status')).toHaveText('ATEM media connected');
}

async function openLibrary(page) {
  const access = await openProtectedPage(page, '/media-library');
  expect(access.ok, access.reason).toBe(true);
  await expect(page.locator('#media-library-page')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Select Welcome', exact: true})).toBeVisible();
}

test('Media setup is separate from library management and player tests', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await openConfig(page);
  await expect(page.locator('#media-setup')).toHaveAttribute('open', '');
  await expect(page.locator('#media-grid')).toHaveCount(0);
  await expect(page.locator('#media-retention-days')).toHaveValue('7');
  await page.getByRole('link', {name: 'Open Media Library', exact: true}).click();
  await expect(page.locator('#media-library-page')).toBeVisible();
  await expect(page.locator('#media-test-panel')).not.toHaveAttribute('open', '');
  await page.getByRole('button', {name: "Select Mother's Day", exact: true}).click();
  await expect(page.locator('#media-edit-form')).toBeVisible();
  await expect(page.locator('#media-create-preset')).toHaveAttribute('href', /\/config\/routing-presets\?image=/);
  await expect(page.locator('#media-delete-image')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({path: testInfo.outputPath('media-config.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('Media Config uploads, renames and deletes an image', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  const name = '<img src=x onerror="window.mediaNameExecuted=true"> ' + testInfo.project.name + ' ' + Date.now();
  const renamed = name + ' updated';
  await openLibrary(page);
  const image = await request.get('/__media_fixture__/upload.png');
  await page.locator('#media-upload-panel > summary').click();
  await page.locator('#media-upload-file').setInputFiles({name: 'phone-photo.png', mimeType: 'image/png', buffer: await image.body()});
  await page.locator('#media-upload-name').fill(name);
  await page.locator('#media-upload-button').click();
  await expect(page.locator('#media-message')).toContainText('Image added to the library.');
  await expect(page.locator('#media-selected-name')).toHaveText(name);
  await expect(page.locator('#media-grid [onerror]')).toHaveCount(0);
  expect(await page.evaluate(() => window.mediaNameExecuted)).toBeUndefined();
  await page.locator('#media-edit-name').fill(renamed);
  await page.locator('#media-save-image').click();
  await expect(page.locator('#media-message')).toHaveText('Image details saved.');
  await page.reload();
  await page.locator('#media-search').fill(renamed);
  await page.getByRole('button', {name: 'Select ' + renamed + '', exact: true}).click();
  await expect(page.locator('#media-create-preset')).toHaveAttribute('href', /\/config\/routing-presets\?image=/);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#media-delete-image').click();
  await expect(page.locator('#media-message')).toHaveText('Image deleted from the library.');
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('Config explains an upload rate limit without adding an image', async ({page, request}) => {
  await openLibrary(page);
  const initialCount = await page.locator('#media-grid .media-tile').count();
  const image = await request.get('/__media_fixture__/upload.png');
  await page.locator('#media-upload-panel > summary').click();
  await page.locator('#media-upload-file').setInputFiles({name: 'photo.png', mimeType: 'image/png', buffer: await image.body()});
  await page.route('**/api/media/upload', route => route.fulfill({
    status: 429, contentType: 'application/json',
    body: JSON.stringify({ok: false, error: 'rate_limited', message: 'Too many image uploads. Wait a minute and try again.'}),
  }));
  await page.locator('#media-upload-button').click();
  await expect(page.locator('#media-message')).toContainText('Too many image uploads. Wait a minute and try again.');
  await expect(page.locator('#media-upload-button')).toBeEnabled();
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(initialCount);
});

test('Media Config saves only unique VideoHub inputs and preserves unmapped players', async ({page}) => {
  const errors = collectPageErrors(page);
  const baseConfig = {
    atem_media_enabled: true, atem_media_node_path: '', atem_media_destinations: [
      // Previously saved AUX settings must disappear from the editor and save.
      {player: 2, label: 'Foyer', slots: [41, 42], aux: 1, videohub_input: 7},
      {player: 4, label: 'Kids', slots: [43, 44]},
    ],
  };
  let saved = null;
  await page.route('**/api/config/atem-media', async route => {
    if (route.request().method() === 'PUT') saved = route.request().postDataJSON();
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, config: saved || baseConfig})});
  });
  await openConfig(page);
  const first = page.locator('#media-destinations .media-destination').nth(0);
  await expect(page.locator('[data-field="aux"]')).toHaveCount(0);
  await expect(page.getByLabel('ATEM AUX output', {exact: true})).toHaveCount(0);
  await expect(first.locator('[data-field="videohub_input"]')).toHaveValue('7');
  await page.locator('#media-add-destination').click();
  const added = page.locator('#media-destinations .media-destination').nth(2);
  await added.locator('[data-field="player"]').fill('3');
  await added.locator('[data-field="label"]').fill('Gallery');
  await added.locator('[data-field="slots"]').fill('45, 46');
  await added.locator('[data-field="videohub_input"]').fill('7');
  await page.locator('#media-save-setup').click();
  await expect(page.locator('#media-setup-status')).toContainText('VideoHub input 7 is listed more than once');
  expect(saved).toBeNull();
  await added.locator('[data-field="videohub_input"]').fill('8');
  await page.locator('#media-retention-days').fill('14');
  await page.locator('#media-save-setup').click();
  await expect(page.locator('#media-setup-status')).toContainText('Setup saved.');
  expect(saved.media_temporary_retention_days).toBe(14);
  expect(saved.atem_media_destinations).toEqual([
    {player: 2, label: 'Foyer', slots: [41, 42], videohub_input: 7},
    {player: 4, label: 'Kids', slots: [43, 44]},
    {player: 3, label: 'Gallery', slots: [45, 46], videohub_input: 8},
  ]);
  expect(errors).toEqual([]);
});

test('Media Config keeps explicit player test control and verified completion', async ({page}) => {
  const errors = collectPageErrors(page);
  await openLibrary(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await page.locator('#media-test-panel > summary').click();
  await page.locator('#media-player').selectOption('2');
  await expect(page.locator('#media-test-panel')).toContainText('All TVs or outputs already using this media player will change together');
  const loadRequest = page.waitForRequest(request => new URL(request.url()).pathname === '/api/atem/media/load' && request.method() === 'POST');
  await page.locator('#media-load-button').click();
  const body = (await loadRequest).postDataJSON();
  expect(Object.keys(body).sort()).toEqual(['media_id', 'player']);
  expect(body.player).toBe(2);
  await expect(page.locator('#media-load-button')).toBeDisabled();
  await expect(page.locator('#media-job')).toContainText('Welcome loaded into Media Player 2');
  await expect(page.locator('#media-load-button')).toBeEnabled();
  expect(errors).toEqual([]);
});
