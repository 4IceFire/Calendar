const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks, openProtectedPage} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  // These tests intentionally exercise writes. Refuse every server except the
  // explicitly isolated fixture, including a normal localhost installation.
  const probe = await request.get('/__media_fixture__/health');
  const body = await probe.json().catch(() => ({}));
  test.skip(!probe.ok() || body.fixture !== 'tdeck-media-ui', 'Run tests/media_ui_harness.py on port 5063 for Media write tests.');
  await installCommonReadMocks(page);
});

async function openMedia(page) {
  const access = await openProtectedPage(page, '/media');
  expect(access.ok, access.reason).toBe(true);
  await expect(page.getByRole('button', {name: 'Select Welcome', exact: true})).toBeVisible();
  await expect(page.locator('#media-connection-status')).toHaveText('ATEM media connected');
}

async function uploadFixture(page, request, name) {
  const image = await request.get('/__media_fixture__/upload.png');
  await page.locator('#media-upload-panel > summary').click();
  await page.locator('#media-upload-file').setInputFiles({name: 'phone-photo.png', mimeType: 'image/png', buffer: await image.body()});
  await page.locator('#media-upload-name').fill(name);
  await page.locator('#media-upload-button').click();
  await expect(page.locator('#media-message')).toContainText('Image uploaded to the library.');
  await expect(page.locator('#media-selected-name')).toHaveText(name);
}

test('Media library searches and filters presets without layout overflow', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await openMedia(page);
  await page.locator('#media-search').fill('welcome');
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(1);
  await page.locator('#media-search').fill('');
  await page.locator('#media-filter').selectOption('presets');
  await expect(page.getByRole('button', {name: "Select Mother's Day, preset", exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Select Welcome', exact: true})).toHaveCount(0);
  await page.getByRole('button', {name: "Select Mother's Day, preset", exact: true}).click();
  await expect(page.locator('#media-preview-image')).toHaveAttribute('alt', "Mother's Day");
  await expect(page.locator('#media-preview-image')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('media-library.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('Media uploads, renames, saves a preset, and deletes the local image', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  const name = 'Team Night ' + testInfo.project.name + ' ' + Date.now();
  const renamed = name + ' revised';
  await openMedia(page);
  await uploadFixture(page, request, name);
  await page.locator('#media-edit-panel > summary').click();
  await page.locator('#media-edit-name').fill(renamed);
  await page.locator('#media-edit-preset').check();
  await page.locator('#media-save-image').click();
  await expect(page.locator('#media-message')).toHaveText('Image details saved.');
  await expect(page.locator('#media-selected-details')).toContainText('Preset');
  await page.locator('#media-search').fill(renamed);
  await page.locator('#media-filter').selectOption('presets');
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(1);
  await page.reload();
  await page.locator('#media-search').fill(renamed);
  await page.locator('#media-filter').selectOption('presets');
  await page.getByRole('button', {name: 'Select ' + renamed + ', preset', exact: true}).click();
  await expect(page.locator('#media-selected-name')).toHaveText(renamed);
  await page.locator('#media-edit-panel > summary').click();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#media-delete-image').click();
  await expect(page.locator('#media-message')).toHaveText('Image deleted from the library.');
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('Media loads a selected image and reports verified player completion', async ({page}) => {
  const errors = collectPageErrors(page);
  await openMedia(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await page.locator('#media-player').selectOption('2');
  await expect(page.locator('.media-load-section')).toContainText('All TVs or outputs already using this media player will change together');
  await page.locator('#media-load-button').click();
  await expect(page.locator('#media-load-button')).toBeDisabled();
  await expect(page.locator('#media-job')).toContainText('Welcome loaded into Media Player 2');
  await expect(page.locator('#media-load-button')).toBeEnabled();
  expect(errors).toEqual([]);
});

test('Media shows a rejected load without claiming success', async ({page}) => {
  await page.route('**/api/atem/media/load', route => route.fulfill({
    status: 409, contentType: 'application/json',
    body: JSON.stringify({ok: false, error: 'Simulated upload conflict: the still slots are busy.'}),
  }));
  await openMedia(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await page.locator('#media-load-button').click();
  await expect(page.locator('#media-message')).toContainText('Simulated upload conflict');
  await expect(page.locator('#media-job')).toBeHidden();
  await expect(page.locator('#media-load-button')).toBeEnabled();
});

test('Media reports an asynchronous transfer failure and allows another attempt', async ({page}) => {
  let job = null;
  await page.route('**/api/atem/media/state', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.job = job;
    await route.fulfill({response, json: body});
  });
  await page.route('**/api/atem/media/load', route => {
    job = {id: 'failed-fixture-job', mediaName: 'Welcome', player: 2, slot: 41, status: 'failed', error: 'Simulated ATEM transfer failure.'};
    return route.fulfill({status: 202, contentType: 'application/json', body: JSON.stringify({ok: true, job: {...job, status: 'queued'}})});
  });
  await openMedia(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await page.locator('#media-player').selectOption('2');
  await page.locator('#media-load-button').click();
  await expect(page.locator('#media-job')).toContainText('Simulated ATEM transfer failure.');
  await expect(page.locator('#media-job')).toHaveClass(/alert-danger/);
  await expect(page.locator('#media-load-button')).toBeEnabled();
});

test('Media setup accepts multiple players and rejects overlapping reservations', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  const baseConfig = {
    atem_media_enabled: true, atem_media_node_path: '', atem_media_destinations: [
      {player: 2, label: 'Foyer', slots: [41, 42]},
      {player: 4, label: 'Kids', slots: [43, 44]},
    ],
  };
  let saved = null;
  await page.route('**/api/config/atem-media', async route => {
    if (route.request().method() === 'PUT') saved = route.request().postDataJSON();
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, config: saved || baseConfig})});
  });
  await openMedia(page);
  await page.locator('#media-setup > summary').click();
  await expect(page.locator('#media-destinations .media-destination')).toHaveCount(2);
  await page.locator('#media-add-destination').click();
  const destination = page.locator('#media-destinations .media-destination').nth(2);
  await destination.locator('[data-field="player"]').fill('3');
  await destination.locator('[data-field="label"]').fill('Gallery');
  await destination.locator('[data-field="slots"]').fill('41, 46');
  await page.locator('#media-save-setup').click();
  await expect(page.locator('#media-setup-status')).toContainText('Still slot 41 is listed more than once.');
  expect(saved).toBeNull();
  await destination.locator('[data-field="slots"]').fill('45, 46');
  await page.locator('#media-save-setup').click();
  await expect(page.locator('#media-setup-status')).toContainText('Setup saved.');
  expect(saved.atem_media_destinations).toEqual([...baseConfig.atem_media_destinations, {player: 3, label: 'Gallery', slots: [45, 46]}]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('media-setup.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('Media browse-only users can select images without seeing write controls', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.route('**/api/media', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.permissions = {upload: false, manage: false, load: false};
    await route.fulfill({response, json: body});
  });
  await page.goto('/__media_fixture__/readonly');
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await expect(page.locator('#media-preview-image')).toBeVisible();
  for (const id of ['media-upload-form', 'media-edit-form', 'media-load-button', 'media-setup']) {
    await expect(page.locator('#' + id)).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test('Config-only Media setup initializes without requesting the image library', async ({page}) => {
  const errors = collectPageErrors(page);
  let libraryRequests = 0;
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/media') libraryRequests += 1; });
  const response = await page.goto('/config/atem-media');
  expect(response.status()).toBe(200);
  await expect(page.locator('#media-setup')).toHaveAttribute('open', '');
  await expect(page.locator('#media-destinations .media-destination')).toHaveCount(2);
  await expect(page.locator('#media-search')).toHaveCount(0);
  expect(libraryRequests).toBe(0);
  expect(errors).toEqual([]);
});

test('Media waits for complete device state and recovers without hiding the library', async ({page}) => {
  let ready = false;
  await page.route('**/api/atem/media/state', async route => {
    const response = await route.fetch();
    await route.fulfill({response, json: {...await response.json(), ready}});
  });
  await openMedia(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await expect(page.locator('#media-load-button')).toBeDisabled();
  await expect(page.locator('#media-load-help')).toContainText('Waiting for the ATEM video format');
  await expect(page.locator('#media-preview-image')).toBeVisible();
  ready = true;
  await page.locator('#media-refresh-state').click();
  await expect(page.locator('#media-load-button')).toBeEnabled();
});
