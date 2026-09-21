const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  const probe = await request.get('/__permissions_fixture__/health');
  const body = await probe.json().catch(() => ({}));
  test.skip(!probe.ok() || body.fixture !== 'tdeck-permissions-ui', 'Run tests/permissions_ui_harness.py on port 5064 with workers=1.');
  const reset = await request.post('/__permissions_fixture__/reset');
  expect(reset.ok()).toBe(true);
  await installCommonReadMocks(page);
});

async function dragRow(page, handle, destination, atTop = false) {
  await expect(handle).toBeEnabled();
  await handle.evaluate(el => el.scrollIntoView({block: 'center', behavior: 'instant'}));
  await handle.scrollIntoViewIfNeeded();
  const source = await handle.boundingBox();
  await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2);
  await page.mouse.down();
  await page.mouse.move(source.x + source.width / 2 + 8, source.y + source.height / 2);
  // Native drag edge scrolling deliberately keeps running while the pointer is
  // held; don't use an actionability wait that requires the target to stop.
  await destination.evaluate(el => el.scrollIntoView({block: 'center', behavior: 'instant'}));
  const target = await destination.boundingBox();
  await page.mouse.move(target.x + target.width / 2, target.y + (atTop ? 3 : target.height / 2), {steps: 10});
  await expect(page.locator('.catalog-drop-target')).toHaveCount(1);
  await page.mouse.up();
}

test('Group drag movement and header rename persist without changing grants', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await openGroup(page);
  const list = page.locator('#access-levels-role-list');
  await expect(list.getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  page.once('dialog', dialog => dialog.accept('Production teams'));
  let saved = page.waitForResponse(response => response.url().endsWith('/api/admin/groups/organization') && response.request().method() === 'POST');
  await list.getByRole('button', {name: 'New folder', exact: true}).click();
  expect((await saved).ok()).toBe(true);
  let group = list.locator('[data-role-item][data-role-id="68"]');
  saved = page.waitForResponse(response => response.url().endsWith('/api/admin/groups/organization') && response.request().method() === 'POST');
  await dragRow(page, list.getByRole('button', {name: 'Move Media Testing', exact: true}), list.getByRole('button', {name: 'Expand or collapse Production teams'}));
  expect((await saved).ok()).toBe(true);
  await page.reload();
  group = list.locator('[data-catalog-folder]:not([data-catalog-folder=""]) [data-role-item][data-role-id="68"]');
  await expect(group).toBeVisible();
  await expect(list.locator('[data-role-item][data-role-id="1"] [data-group-rename]')).toHaveCount(0);
  await expect(list.locator('[data-group-rename]')).toHaveCount(0);
  await list.locator('[data-role-select][data-role-id="68"]').click();
  await expect(page.locator('#group-editor-header [data-group-rename="68"]')).toBeVisible();
  page.once('dialog', dialog => dialog.accept('Production operators'));
  await page.getByRole('button', {name: 'Rename group', exact: true}).click();
  await expect(list.locator('[data-role-item][data-role-id="68"]')).toContainText('Production operators');
  const form = page.locator('[data-role-form][data-role-id="68"]');
  await list.locator('[data-role-select][data-role-id="68"]').click();
  await expect(pageGrant(form, 'routing')).toBeChecked();
  await expect(pageGrant(form, 'media')).toBeChecked();
  const state = await request.get('/api/admin/groups/organization');
  expect((await state.json()).organization.items.find(item => item.id === '68').folderId).toBeTruthy();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('group-drag-editor.png'), fullPage: true});
  await list.locator('[data-role-select][data-role-id="1"]').click();
  await expect(page.getByRole('button', {name: 'Rename group', exact: true})).toBeHidden();
  expect(errors).toEqual([]);
});

test('VideoHub drag folders and keyboard order survive reload and preserve locked presets', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await page.goto('/videohub');
  const list = page.locator('#vh-list');
  await expect(list.getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  const before = (await (await request.get('/api/videohub/presets')).json()).presets;
  page.once('dialog', dialog => dialog.accept('Services'));
  let saved = page.waitForResponse(response => response.url().endsWith('/api/videohub/presets/organization') && response.request().method() === 'POST');
  await list.getByRole('button', {name: 'New folder', exact: true}).click();
  expect((await saved).ok()).toBe(true);
  const preset = list.locator('[data-catalog-item="8"]');
  saved = page.waitForResponse(response => response.url().endsWith('/api/videohub/presets/organization') && response.request().method() === 'POST');
  const handle = preset.locator('.catalog-handle');
  await expect(handle).toBeEnabled();
  await handle.focus(); await page.keyboard.press('Space'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Enter');
  expect((await saved).ok()).toBe(true);
  await expect(list.locator('[data-act="select"]').first()).toHaveAttribute('data-id', '8');
  saved = page.waitForResponse(response => response.url().endsWith('/api/videohub/presets/organization') && response.request().method() === 'POST');
  await dragRow(page, handle, list.getByRole('button', {name: 'Expand or collapse Services'}));
  expect((await saved).ok()).toBe(true);
  await page.reload();
  const folder = list.locator('[data-catalog-folder]:not([data-catalog-folder=""])');
  await expect(folder.locator('[data-act="select"][data-id="8"]')).toBeVisible();
  await expect(folder).toContainText('Locked');
  await page.screenshot({path: testInfo.outputPath('videohub-drag-folders.png'), fullPage: true});
  await list.getByRole('button', {name: 'Expand or collapse Services'}).click();
  await expect(folder.locator('[data-act="select"][data-id="8"]')).toBeHidden();
  const after = (await (await request.get('/api/videohub/presets')).json()).presets;
  expect(after).toEqual(before);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  expect(errors).toEqual([]);
});

async function openGroup(page) {
  await page.goto('/admin/permissions?tab=groups#role-68');
  await expect(page.locator('#access-levels-role-list').getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  const form = page.locator('[data-role-form][data-role-id="68"]');
  await expect(form).toBeVisible();
  return form;
}

test('Drag cancellation, delayed saves and failed saves keep the selected group stable', async ({page, request}) => {
  const errors = collectPageErrors(page);
  const form = await openGroup(page);
  const list = page.locator('#access-levels-role-list');
  const state = (await (await request.get('/api/admin/groups/organization')).json()).organization;
  let writes = 0, release;
  await page.route('**/api/admin/groups/organization', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    writes += 1;
    await new Promise(resolve => { release = resolve; });
    await route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({ok: false, error: 'Fixture save failed'})});
  });
  const handle = list.getByRole('button', {name: 'Move Media Testing', exact: true});
  await handle.focus(); await page.keyboard.press('Space'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Escape');
  expect(writes).toBe(0);
  await expect(form).toBeVisible();
  await handle.focus(); await page.keyboard.press('Space'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Enter');
  await expect(list.locator('[data-catalog-item]').first()).toHaveAttribute('data-catalog-item', '68');
  await expect(list).toHaveAttribute('aria-busy', 'true');
  await expect(form).toBeVisible();
  await expect.poll(() => writes).toBe(1);
  release();
  await expect(page.getByText('Fixture save failed', {exact: true})).toBeVisible();
  await expect(list.locator('[data-catalog-item]').first()).toHaveAttribute('data-catalog-item', '1');
  await expect(handle).toBeFocused();
  expect((await (await request.get('/api/admin/groups/organization')).json()).organization).toEqual(state);
  expect(errors).toEqual([]);
});

test('Dropping into a collapsed folder opens it and moving back to No folder persists', async ({page, request}) => {
  await openGroup(page);
  const list = page.locator('#access-levels-role-list');
  page.once('dialog', dialog => dialog.accept('Team'));
  await list.getByRole('button', {name: 'New folder', exact: true}).click();
  const folderToggle = list.getByRole('button', {name: 'Expand or collapse Team'});
  await expect(folderToggle).toBeEnabled(); await folderToggle.click();
  await expect(folderToggle).toHaveAttribute('aria-expanded', 'false');
  const handle = list.getByRole('button', {name: 'Move Media Testing', exact: true});
  await dragRow(page, handle, folderToggle);
  await expect(folderToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(list).toHaveAttribute('aria-busy', 'false');
  await dragRow(page, handle, list.locator('[data-catalog-folder=""] .catalog-header'));
  await expect(list.locator('[data-catalog-folder=""] [data-catalog-item="68"]')).toBeVisible();
  await expect(list).toHaveAttribute('aria-busy', 'false');
  expect((await (await request.get('/api/admin/groups/organization')).json()).organization.items.find(item => item.id === '68').folderId).toBe(null);
  await page.reload();
  await expect(list.locator('[data-catalog-folder=""] [data-catalog-item="68"]')).toBeVisible();
});

test('Touch dragging uses the same folder allocation without page scrolling', async ({page, browserName}, testInfo) => {
  test.skip(browserName !== 'chromium' || !testInfo.project.use.isMobile, 'Native multi-point touch input uses the mobile Chromium protocol.');
  await openGroup(page);
  const list = page.locator('#access-levels-role-list');
  page.once('dialog', dialog => dialog.accept('Touch team'));
  await list.getByRole('button', {name: 'New folder', exact: true}).click();
  const target = list.getByRole('button', {name: 'Expand or collapse Touch team'});
  await expect(target).toBeEnabled();
  // Keep the gesture clear of intentional edge scrolling while checking that
  // touching the handle does not turn into a normal page-pan gesture.
  await target.evaluate(el => el.scrollIntoView({block: 'center', behavior: 'instant'}));
  const handle = list.getByRole('button', {name: 'Move Media Testing', exact: true});
  const a = await handle.boundingBox(), b = await target.boundingBox();
  const session = await page.context().newCDPSession(page);
  const start = {x: a.x + a.width / 2, y: a.y + a.height / 2};
  const end = {x: b.x + b.width / 2, y: b.y + b.height / 2};
  const before = await page.evaluate(() => window.scrollY);
  await session.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [start]});
  for (let n = 1; n <= 10; n++) await session.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: start.x + (end.x - start.x) * n / 10, y: start.y + (end.y - start.y) * n / 10}]});
  await expect(page.locator('.catalog-drag-ghost')).toBeVisible();
  await session.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
  await expect(list.locator('[data-catalog-folder]:not([data-catalog-folder=""]) [data-catalog-item="68"]')).toBeVisible();
  expect(Math.abs(await page.evaluate(() => window.scrollY) - before)).toBeLessThan(10);
  await session.detach();
});

test('Opening a VideoHub preset during a slow organisation save does not undo the move', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/videohub');
  const list = page.locator('#vh-list');
  await expect(list.getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  let release;
  await page.route('**/api/videohub/presets/organization', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    await new Promise(resolve => { release = resolve; });
    await route.continue();
  });
  await list.locator('[data-catalog-item="8"] .catalog-handle').focus();
  await page.keyboard.press('Space'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Enter');
  await expect(list).toHaveAttribute('aria-busy', 'true');
  await list.locator('[data-act="select"][data-id="8"]').click();
  await expect(page.locator('#vh-name')).toHaveValue('Midweek');
  await expect(list.locator('[data-catalog-item]').first()).toHaveAttribute('data-catalog-item', '8');
  await expect.poll(() => typeof release).toBe('function'); release();
  await expect(list).toHaveAttribute('aria-busy', 'false');
  await expect(list.locator('[data-catalog-item]').first()).toHaveAttribute('data-catalog-item', '8');
  await expect(list.locator('[data-catalog-item="8"]')).toContainText('Open');
  expect(errors).toEqual([]);
});

function pageGrant(form, key) {
  return form.locator('input[name="page_keys"][value="page:' + key + '"]');
}

async function saveChange(page, action) {
  const result = page.waitForResponse(response => response.url().endsWith('/api/admin/groups/68') && response.request().method() === 'POST');
  await action();
  const response = await result;
  expect(response.ok()).toBe(true);
  return response.request().postDataJSON();
}

test('Routing contains media selection and subordinate upload options', async ({page}) => {
  const errors = collectPageErrors(page);
  const form = await openGroup(page);
  await expect(form.locator('.group-page-access input[value^="page:media"]')).toHaveCount(1);
  await expect(pageGrant(form, 'media_library')).toBeVisible();
  await expect(form.locator('[data-permission-tab="media"]')).toHaveCount(0);
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  const media = pageGrant(form, 'media');
  const upload = pageGrant(form, 'media_upload');
  await expect(media).toBeVisible();
  await expect(upload).toBeVisible();
  const payload = await saveChange(page, () => upload.check());
  expect(payload.page_keys).toContain('page:media_upload');
  const permanent = pageGrant(form, 'media_save');
  await expect(permanent).toBeVisible();
  const savePayload = await saveChange(page, () => permanent.check());
  expect(savePayload.page_keys).toContain('page:media_save');
  await saveChange(page, () => media.uncheck());
  await expect(upload).toBeHidden();
  await expect(permanent).toBeHidden();
  await expect(upload).toBeChecked();
  await saveChange(page, () => media.check());
  await expect(upload).toBeVisible();
  await saveChange(page, () => pageGrant(form, 'routing').uncheck());
  await expect(form.getByRole('tab', {name: 'Routing', exact: true})).toBeHidden();
  await expect(media).toBeHidden();
  await expect(form.getByRole('tab', {name: 'General', exact: true})).toHaveAttribute('aria-selected', 'true');
  await page.reload();
  await saveChange(page, () => pageGrant(form, 'routing').check());
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  await expect(media).toBeChecked();
  await expect(upload).toBeChecked();
  await expect(permanent).toBeChecked();
  expect(errors).toEqual([]);
});

test('Tabs expose enabled pages and support keyboard navigation and revocation', async ({page}) => {
  const errors = collectPageErrors(page);
  const form = await openGroup(page);
  await expect(form.getByRole('tab')).toHaveText(['General', 'Routing']);
  await expect(form.locator('[data-permission-panel="digico"]')).toBeHidden();
  const general = form.getByRole('tab', {name: 'General', exact: true});
  await general.focus();
  await page.keyboard.press('ArrowRight');
  await expect(form.getByRole('tab', {name: 'Routing', exact: true})).toBeFocused();
  await page.keyboard.press('End');
  const routing = form.getByRole('tab', {name: 'Routing', exact: true});
  await expect(routing).toBeFocused();
  await expect(form.locator('[data-permission-panel="routing"]')).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(general).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(routing).toBeFocused();
  await saveChange(page, () => pageGrant(form, 'routing').uncheck());
  await expect(routing).toBeHidden();
  await expect(general).toHaveAttribute('aria-selected', 'true');
  await expect(form.locator('[data-permission-panel="routing"]')).toBeHidden();
  await saveChange(page, () => pageGrant(form, 'digico_mixer').check());
  await expect(form.getByRole('tab', {name: 'Personal Mixes', exact: true})).toBeVisible();
  expect(errors).toEqual([]);
});

test('Hidden restrictions survive page revocation, group switching and reload', async ({page}) => {
  const form = await openGroup(page);
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  const outputs = form.locator('input[name="videohub_allowed_outputs_role"]');
  await expect(outputs).toHaveValue('[1, 2]');
  await saveChange(page, () => outputs.fill('2'));
  const payload = await saveChange(page, () => pageGrant(form, 'routing').uncheck());
  expect(payload.videohub_allowed_outputs_role).toBe('2');
  await page.locator('[data-role-select][data-role-id="69"]').click();
  await expect(form).toBeHidden();
  await page.locator('[data-role-select][data-role-id="68"]').click();
  await expect(form).toBeVisible();
  await page.reload();
  await saveChange(page, () => pageGrant(form, 'routing').check());
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  // Firefox can restore the equivalent typed form ('2') after reloading '[2]'.
  await expect.poll(async () => (await outputs.inputValue()).replace(/[\[\]\s]/g, '')).toBe('2');
  await expect(form.locator('input[name="videohub_allowed_inputs_role"]')).toHaveValue('[1, 7]');
});

test('Pixie hides unavailable device choices and retains selected resources', async ({page}) => {
  const errors = collectPageErrors(page);
  const form = await openGroup(page);
  await saveChange(page, () => pageGrant(form, 'pixie_controls').check());
  await form.getByRole('tab', {name: 'Pixie', exact: true}).click();
  const main = form.locator('[data-role="pixie-auditorium-block"][data-auditorium-id="main"]');
  const kids = form.locator('[data-role="pixie-auditorium-block"][data-auditorium-id="kids"]');
  const front = main.locator('[data-role="pixie-device"][value="main-front"]');
  await expect(front).toBeChecked();
  await expect(front).toBeVisible();
  await expect(kids.locator('[data-role="pixie-device-scope"]')).toBeHidden();
  const payload = await saveChange(page, () => main.locator('[data-role="pixie-all-devices"]').check());
  expect(payload.pixie_allowed_devices_role.main).toBe('*');
  await expect(front).toBeHidden();
  await expect(front).toBeDisabled();
  await expect(front).toBeChecked();
  await saveChange(page, () => main.locator('[data-role="pixie-all-devices"]').uncheck());
  await expect(front).toBeVisible();
  await expect(front).toBeChecked();
  await saveChange(page, () => pageGrant(form, 'pixie_controls').uncheck());
  await page.reload();
  await saveChange(page, () => pageGrant(form, 'pixie_controls').check());
  await form.getByRole('tab', {name: 'Pixie', exact: true}).click();
  await expect(front).toBeChecked();
  await expect(front).toBeVisible();
  expect(errors).toEqual([]);
});

test('All enabled detail tabs fit desktop and mobile without widening the page', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  const form = await openGroup(page);
  for (const key of ['digico_mixer', 'videohub', 'surface_controls', 'pixie_controls', 'atem_audio']) {
    await saveChange(page, () => pageGrant(form, key).check());
  }
  await expect(form.getByRole('tab')).toHaveText(['General', 'Personal Mixes', 'Routing', 'VideoHub', 'Surface Controls', 'Pixie', 'Record Audio']);
  await form.getByRole('tab', {name: 'Record Audio', exact: true}).click();
  await expect(form.locator('[data-permission-panel="audio"]')).toBeVisible();
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('group-permissions.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('An auto-save error is shown and the next edit can save successfully', async ({page}) => {
  const form = await openGroup(page);
  let fail = true;
  await page.route('**/api/admin/groups/68', async route => {
    if (fail) {
      fail = false;
      return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({ok: false, error: 'Could not save this change.'})});
    }
    return route.continue();
  });
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  await pageGrant(form, 'media').uncheck();
  const error = page.locator('[data-role-panel][data-role-id="68"] [data-role-error]');
  await expect(error).toHaveText('Could not save this change.');
  await expect(error).toBeVisible();
  await saveChange(page, () => pageGrant(form, 'media').check());
  await expect(error).toBeHidden();
  await page.reload();
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  await expect(pageGrant(form, 'media')).toBeChecked();
});
