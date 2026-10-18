const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks, openProtectedPage} = require('./helpers');

const marker = 'tdeck-media-replace-ui';
test.beforeEach(async ({page, request}) => {
  const probe = await request.get('/__media_fixture__/replace/health');
  const body = await probe.json().catch(() => ({}));
  expect(['127.0.0.1', 'localhost', '[::1]']).toContain(new URL(probe.url()).hostname);
  expect(probe.ok() && body.fixture === marker && body.isolated === true,
    'Replacement writes require tests/media_replace_ui_harness.py on loopback.').toBe(true);
  await installCommonReadMocks(page);
});

test('fixture admits more than ten sequential real replacement requests without a rate-limit response', async ({request}) => {
  const items = (await (await request.get('/api/media')).json()).items;
  const id = items.find(item => item.name === 'Welcome').id;
  for (let attempt = 0; attempt < 12; attempt++) {
    const response = await request.post('/api/media/' + id + '/replace', {
      multipart: {file: {name: 'invalid.png', mimeType: 'image/png', buffer: Buffer.from('not an image')}},
    });
    expect(response.status(), 'replacement attempt ' + (attempt + 1)).toBe(400);
  }
});

async function openLibrary(page) {
  const access = await openProtectedPage(page, '/media-library');
  expect(access.ok, access.reason).toBe(true);
  await expect(page.locator('#media-grid .media-tile').first()).toBeVisible();
}
async function imageFile(request) {
  const response = await request.get('/__media_fixture__/upload.png');
  expect(response.ok()).toBe(true);
  return {name: 'replacement.png', mimeType: 'image/png', buffer: await response.body()};
}
function watchWrites(page) {
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    // Expected image-error telemetry is not a media or device mutation.
    if (request.method() !== 'GET' && path.startsWith('/api/') && path !== '/api/client-errors') {
      writes.push(path);
    }
  });
  return writes;
}

test('explicit replacement preserves existing identity and metadata after reload without device commands', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  const seed = await request.post('/__media_fixture__/replace/seed', {data: {fixture: marker, missing: 'none'}});
  expect(seed.ok()).toBe(true);
  const id = (await seed.json()).id;
  const writes = watchWrites(page);
  await openLibrary(page);
  const tile = page.locator('[data-media-id="' + id + '"]');
  const before = (await (await request.get('/api/media')).json()).items.find(item => item.id === id);
  const beforeBytes = await (await request.get(before.url)).body();
  await tile.click();
  await expect(page.locator('#media-replace-help')).toContainText('preset links');
  await page.locator('#media-replace-file').setInputFiles(await imageFile(request));
  expect(writes).toEqual([]);
  const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/api/media/' + id + '/replace');
  await page.locator('#media-replace-button').click();
  const replacementResponse = await responsePromise;
  expect(replacementResponse.ok()).toBe(true);
  const multipart = replacementResponse.request().postDataBuffer().toString('latin1');
  expect((multipart.match(/Content-Disposition: form-data; name="[^"]+"/g) || [])).toEqual(['Content-Disposition: form-data; name="file"']);
  await expect(page.locator('#media-replace-status')).toContainText('Image replaced');
  expect(await page.locator('#media-replace-file').evaluate(input => input.files.length)).toBe(0);
  await expect(page.locator('#media-preview-image')).toHaveAttribute('src', /[?&]media_refresh=/);
  await expect(page.locator('#media-preview-image')).toBeVisible();
  await expect.poll(() => page.locator('#media-preview-image').evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({path: testInfo.outputPath('media-replace-success.png'), fullPage: true});
  await page.reload();
  await page.locator('[data-media-id="' + id + '"]').click();
  await expect(page.locator('#media-selected-name')).toHaveText(before.name);
  const after = (await (await request.get('/api/media')).json()).items.find(item => item.id === id);
  const afterBytes = await (await request.get(after.url)).body();
  expect(afterBytes.equals(beforeBytes)).toBe(false);
  for (const key of ['id', 'name', 'preset', 'created_at', 'uploaded_by', 'uploaded_by_id', 'temporary', 'expires_at']) {
    expect(after[key], key).toEqual(before[key]);
  }
  expect(writes).toEqual(['/api/media/' + id + '/replace']);
  expect(errors).toEqual([]);
  await page.screenshot({path: testInfo.outputPath('media-replace.png'), fullPage: true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

for (const missing of ['both', 'file', 'thumbnail']) {
  test('repairs synthetic missing ' + missing + ' assets while retaining an editable selectable entry', async ({page, request}) => {
    const seed = await request.post('/__media_fixture__/replace/seed', {data: {fixture: marker, missing}});
    expect(seed.ok()).toBe(true);
    const id = (await seed.json()).id;
    const writes = watchWrites(page);
    await openLibrary(page);
    const tile = page.locator('[data-media-id="' + id + '"]');
    await expect(tile).toContainText(missing !== 'thumbnail' ? 'Image file missing' : 'Preview missing');
    await expect(tile.locator('img')).toHaveCount(0);
    await tile.click();
    await expect(page.locator('#media-edit-name')).toBeEnabled();
    if (missing !== 'thumbnail') {
      await expect(page.locator('#media-preview-placeholder')).toHaveText(/Image file missing/);
      await expect(page.locator('#media-preview-image')).not.toHaveAttribute('src', /.+/);
      await page.locator('#media-test-panel > summary').click();
      await expect(page.locator('#media-load-button')).toBeDisabled();
      await expect(page.locator('#media-load-help')).toContainText('missing');
    } else {
      await expect(page.locator('#media-preview-image')).toBeVisible();
    }
    await page.locator('#media-replace-file').setInputFiles(await imageFile(request));
    expect(writes).toEqual([]);
    await page.locator('#media-replace-button').click();
    await expect(page.locator('#media-replace-status')).toContainText('Image replaced');
    await expect(tile).not.toContainText('missing');
    await page.reload();
    await page.locator('[data-media-id="' + id + '"]').click();
    await expect(page.locator('#media-preview-placeholder')).not.toBeVisible();
    const repaired = (await (await request.get('/api/media')).json()).items.find(item => item.id === id);
    expect(repaired.file_missing).toBe(false);
    expect(repaired.thumbnail_missing).toBe(false);
    expect(writes).toEqual(['/api/media/' + id + '/replace']);
  });
}

for (const failure of ['invalid', 'busy']) {
  test(failure + ' replacement retains the chosen file and reports failure beside its form without retry', async ({page, request}) => {
    await openLibrary(page);
    const tile = page.getByRole('button', {name: 'Select Welcome', exact: true});
    const id = await tile.getAttribute('data-media-id');
    await tile.click();
    const writes = watchWrites(page);
    if (failure === 'busy') {
      // Deterministic contention response: the successful and invalid cases use
      // the actual endpoint; no global coordinator/shared fixture is changed.
      await page.route('**/api/media/' + id + '/replace', route => route.fulfill({
        status: 409, contentType: 'application/json', body: JSON.stringify({ok: false, message: 'Another media operation is busy. Try again when it finishes.'}),
      }));
    }
    const file = failure === 'invalid' ? {name: 'not-an-image.png', mimeType: 'image/png', buffer: Buffer.from('not an image')} : await imageFile(request);
    await page.locator('#media-replace-file').setInputFiles(file);
    await page.locator('#media-replace-button').click();
    await expect(page.locator('#media-replace-status')).toContainText('Could not confirm replacement');
    await expect(page.locator('#media-replace-status')).toContainText(failure === 'busy' ? 'busy' : /valid|image/i);
    await expect(page.locator('#media-replace-status')).toContainText('Nothing will be retried automatically');
    expect(await page.locator('#media-replace-file').evaluate(input => input.files[0].name)).toBe(file.name);
    await expect(page.locator('#media-replace-button')).toBeEnabled();
    await expect(page.locator('#media-message')).not.toBeVisible();
    await page.waitForTimeout(300);
    expect(writes).toEqual(['/api/media/' + id + '/replace']);
  });
}

test('a delayed real replacement freezes selection and conflicting controls until the original response returns', async ({page, request}) => {
  await openLibrary(page);
  const tile = page.getByRole('button', {name: 'Select Welcome', exact: true});
  const id = await tile.getAttribute('data-media-id');
  await tile.click();
  const writes = watchWrites(page);
  const held = await request.post('/__media_fixture__/replace/hold', {data: {fixture: marker, id}});
  expect(held.ok()).toBe(true);
  await page.locator('#media-replace-file').setInputFiles(await imageFile(request));
  await page.locator('#media-replace-button').click();
  try {
    await expect.poll(async () => {
      const response = await request.get('/__media_fixture__/replace/held');
      expect(response.ok()).toBe(true);
      const body = await response.json();
      expect(body.fixture).toBe(marker);
      return body.waiting;
    }).toBe(true);
    for (const selector of ['#media-saved-tab', '#media-temporary-tab', '#media-search', '#media-refresh-library', '#media-upload-file', '#media-upload-button', '#media-save-image', '#media-delete-image', '#media-edit-name', '#media-replace-file', '#media-replace-button', '#media-load-button']) {
      await expect(page.locator(selector)).toBeDisabled();
    }
    await expect(page.locator('#media-grid .media-tile:enabled')).toHaveCount(0);
    // Even synthetic event dispatch must not bypass the pending-selection guard.
    await page.getByRole('button', {name: "Select Mother's Day", exact: true}).dispatchEvent('click');
    await page.locator('#media-temporary-tab').dispatchEvent('click');
    await page.locator('#media-upload-form').dispatchEvent('submit');
    await page.locator('#media-edit-form').dispatchEvent('submit');
    await page.locator('#media-delete-image').dispatchEvent('click');
    await expect(page.locator('#media-selected-name')).toHaveText('Welcome');
    await expect(tile).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#media-saved-tab')).toHaveAttribute('aria-pressed', 'true');
    expect(writes).toEqual(['/api/media/' + id + '/replace']);
  } finally {
    const released = await request.post('/__media_fixture__/replace/release', {data: {fixture: marker}});
    expect(released.ok()).toBe(true);
  }
  await expect(page.locator('#media-replace-status')).toContainText('Image replaced');
  await expect(page.locator('#media-selected-name')).toHaveText('Welcome');
  await expect(page.locator('#media-edit-name')).toBeEnabled();
  expect(writes).toEqual(['/api/media/' + id + '/replace']);
});

test('a success response for another identity is not applied or retried', async ({page, request}) => {
  await openLibrary(page);
  const tile = page.getByRole('button', {name: 'Select Welcome', exact: true});
  const id = await tile.getAttribute('data-media-id');
  await tile.click();
  const writes = watchWrites(page);
  await page.route('**/api/media/' + id + '/replace', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, item: {id: 'other', name: 'Wrong entry'}}),
  }));
  await page.locator('#media-replace-file').setInputFiles(await imageFile(request));
  await page.locator('#media-replace-button').click();
  await expect(page.locator('#media-replace-status')).toContainText('did not confirm the original image entry');
  await expect(page.locator('#media-selected-name')).toHaveText('Welcome');
  expect(await page.locator('#media-replace-file').evaluate(input => input.files.length)).toBe(1);
  expect(writes).toEqual(['/api/media/' + id + '/replace']);
});

test('older lists without missing flags remain selectable and preview errors show an honest placeholder', async ({page}) => {
  await page.route('**/api/media?collection=saved', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.items = body.items.map(item => {
      const old = {...item};
      delete old.file_missing;
      delete old.thumbnail_missing;
      return old;
    });
    await route.fulfill({response, json: body});
  });
  const writes = watchWrites(page);
  await openLibrary(page);
  await page.getByRole('button', {name: 'Select Welcome', exact: true}).click();
  await page.locator('#media-test-panel > summary').click();
  await expect(page.locator('#media-load-button')).toBeEnabled();
  await page.locator('#media-preview-image').dispatchEvent('error');
  await expect(page.locator('#media-preview-placeholder')).toHaveText('Image preview unavailable. Refresh the library to check this entry.');
  await expect(page.locator('#media-preview-image')).not.toBeVisible();
  await expect(page.locator('#media-edit-name')).toBeEnabled();
  expect(writes).toEqual([]);
});
