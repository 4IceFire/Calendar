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

async function openGroup(page) {
  await page.goto('/admin/permissions?tab=groups#role-68');
  const form = page.locator('[data-role-form][data-role-id="68"]');
  await expect(form).toBeVisible();
  return form;
}

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
