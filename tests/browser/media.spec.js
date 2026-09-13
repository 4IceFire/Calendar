const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks, openProtectedPage} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  const probe = await request.get('/__media_fixture__/health');
  const body = await probe.json().catch(() => ({}));
  test.skip(!probe.ok() || body.fixture !== 'tdeck-media-ui', 'Run tests/media_ui_harness.py on port 5063 for Media write tests.');
  await installCommonReadMocks(page);
});

async function openMedia(page, output = 1) {
  const access = await openProtectedPage(page, '/media?output=' + output);
  expect(access.ok, access.reason).toBe(true);
  await expect(page.getByRole('button', {name: 'Display Welcome', exact: true})).toBeVisible();
}

async function chooseUpload(page, request, name) {
  const image = await request.get('/__media_fixture__/upload.png');
  await page.locator('#media-upload-file').setInputFiles({name: 'phone-photo.png', mimeType: 'image/png', buffer: await image.body()});
  await page.locator('#media-upload-name').fill(name);
  await expect(page.locator('#media-upload-preview')).toBeVisible();
}

function mockDisplay(page, options = {}) {
  let calls = 0;
  let job = null;
  const ready = page.route('**/api/media/display', route => {
    calls += 1;
    const body = route.request().postDataJSON();
    job = {id: 'display-fixture-job', mediaId: body.media_id, output: body.output, status: options.failed ? 'failed' : 'queued', message: options.failed ? 'No screens were changed. Please try again.' : 'Displaying your image…', error: null};
    return route.fulfill({status: 202, contentType: 'application/json', body: JSON.stringify({ok: true, job})});
  });
  return {ready, calls: () => calls, job: () => job};
}

test('Media picker stays simple and filters presets on desktop and mobile', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  const hardwareRequests = [];
  page.on('request', request => { if (/\/api\/(atem\/media|config\/atem-media|status\/summary)/.test(new URL(request.url()).pathname)) hardwareRequests.push(request.url()); });
  await openMedia(page);
  await page.locator('#media-search').fill('welcome');
  await expect(page.locator('#media-grid .media-tile')).toHaveCount(1);
  await page.locator('#media-search').fill('');
  await page.locator('#media-filter').selectOption('presets');
  await expect(page.getByRole('button', {name: "Display Mother's Day, preset", exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Display Welcome', exact: true})).toHaveCount(0);
  for (const id of ['media-connection-status', 'media-setup', 'media-player', 'media-load-button', 'media-edit-form', 'media-upload-form']) await expect(page.locator('#' + id)).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('media-picker.png'), fullPage: true});
  expect(hardwareRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test('A player with only a VideoHub input displays an existing image and returns to outputs', async ({page}) => {
  const errors = collectPageErrors(page);
  await openMedia(page, 2);
  const pending = page.waitForRequest(request => new URL(request.url()).pathname === '/api/media/display' && request.method() === 'POST');
  await page.getByRole('button', {name: 'Display Welcome', exact: true}).click();
  const body = (await pending).postDataJSON();
  expect(body.output).toBe(2);
  expect(Object.keys(body).sort()).toEqual(['media_id', 'output']);
  await expect(page).toHaveURL(/\/routing\?media_job=/);
  await expect(page.locator('#routing-output-step')).toBeVisible();
  await expect(page.locator('#routing-input-step')).toBeHidden();
  expect(errors).toEqual([]);
});

test('Uploading an image displays it and returns to outputs', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await openMedia(page, 1);
  await page.locator('#media-upload-link').click();
  await expect(page).toHaveURL(/\/media\/upload\?output=1$/);
  await chooseUpload(page, request, 'Upload flow ' + testInfo.project.name + ' ' + Date.now());
  await page.screenshot({path: testInfo.outputPath('media-upload.png'), fullPage: true});
  await page.getByRole('button', {name: 'Upload and display', exact: true}).click();
  await expect(page).toHaveURL(/\/routing\?media_job=/);
  await expect(page.locator('#routing-output-step')).toBeVisible();
  expect(errors).toEqual([]);
});

test('Media back buttons preserve the selected output through upload and inputs', async ({page}) => {
  await openMedia(page, 3);
  await expect(page.locator('#media-back')).toHaveAttribute('href', '/routing?output=3');
  await page.locator('#media-upload-link').click();
  await expect(page.locator('#media-back')).toHaveAttribute('href', '/media?output=3');
  await page.locator('#media-back').click();
  await page.locator('#media-back').click();
  await expect(page).toHaveURL(/\/routing\?output=3$/);
  await expect(page.locator('#routing-input-step')).toBeVisible();
  await expect(page.locator('#routing-current')).toContainText('Hall');
});

test('Failed display retains the selected image and requires an explicit retry', async ({page}) => {
  const display = mockDisplay(page, {failed: true});
  await display.ready;
  await openMedia(page);
  await page.getByRole('button', {name: 'Display Welcome', exact: true}).click();
  await expect(page.locator('#media-progress-name')).toHaveText('Welcome');
  await expect(page.locator('#media-progress-message')).toContainText('No screens were changed');
  await expect(page.locator('#media-display-retry')).toBeVisible();
  await expect(page).toHaveURL(/\/media\?output=1$/);
  expect(display.calls()).toBe(1);
  await page.locator('#media-display-retry').click();
  await expect.poll(display.calls).toBe(2);
});

test('Retry after a display failure reuses the saved upload without duplicating it', async ({page, request}, testInfo) => {
  let uploads = 0;
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/media/upload' && request.method() === 'POST') uploads += 1; });
  const display = mockDisplay(page, {failed: true});
  await display.ready;
  await page.goto('/media/upload?output=1');
  await chooseUpload(page, request, 'Retry upload ' + testInfo.project.name + ' ' + Date.now());
  await page.locator('#media-upload-button').click();
  await expect(page.locator('#media-display-retry')).toBeVisible();
  const savedId = display.job().mediaId;
  await expect(page.locator('#media-upload-button')).toBeDisabled();
  await page.reload();
  await expect(page.locator('#media-display-retry')).toBeVisible();
  expect(display.calls()).toBe(1);
  await page.locator('#media-display-retry').click();
  await expect.poll(display.calls).toBe(2);
  expect(display.job().mediaId).toBe(savedId);
  expect(uploads).toBe(1);
});

test('Read-only users preview images without write or setup controls', async ({page}) => {
  const errors = collectPageErrors(page);
  const writes = [];
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname.indexOf('/api/media') === 0) writes.push(request.url()); });
  await page.goto('/__media_fixture__/readonly');
  await page.getByRole('button', {name: 'Preview Welcome', exact: true}).click();
  await expect(page.locator('#media-preview-modal')).toBeVisible();
  await expect(page.locator('#media-preview-image')).toHaveAttribute('alt', 'Welcome');
  await page.locator('#media-preview-close').click();
  await expect(page.locator('#media-preview-modal')).toBeHidden();
  for (const id of ['media-upload-link', 'media-upload-form', 'media-edit-form', 'media-player', 'media-setup']) await expect(page.locator('#' + id)).toHaveCount(0);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('Refreshing a pending display resumes job checks without another display request', async ({page}) => {
  const display = mockDisplay(page);
  await display.ready;
  let finish = false;
  let reads = 0;
  await page.route('**/api/media/display/display-fixture-job', route => {
    reads += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, job: {...display.job(), status: finish ? 'succeeded' : 'queued'}})});
  });
  await openMedia(page);
  await page.getByRole('button', {name: 'Display Welcome', exact: true}).click();
  await expect(page.locator('#media-back')).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByRole('button', {name: 'Display Welcome', exact: true})).toBeDisabled();
  await expect.poll(() => reads).toBeGreaterThan(0);
  await page.reload();
  await expect.poll(() => reads).toBeGreaterThan(1);
  expect(display.calls()).toBe(1);
  finish = true;
  await expect(page).toHaveURL(/\/routing\?media_job=display-fixture-job$/);
});

test('Library without an output supports previews and points back to Routing', async ({page}) => {
  await page.goto('/media');
  await expect(page.locator('#media-back')).toHaveText('Choose an output');
  await expect(page.locator('#media-back')).toHaveAttribute('href', '/routing');
  await page.getByRole('button', {name: 'Preview Welcome', exact: true}).click();
  await expect(page.locator('#media-preview-modal')).toBeVisible();
});
