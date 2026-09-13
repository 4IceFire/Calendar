const {test, expect} = require('@playwright/test');
const {collectPageErrors} = require('./helpers');

test.beforeEach(async ({page, request}) => {
  const health = await request.get('/__permissions_fixture__/health');
  const data = await health.json().catch(() => ({}));
  test.skip(data.fixture !== 'tdeck-routing-presets-ui', 'Run permissions_ui_harness.py --presets on port 5066 with workers=1.');
  expect((await request.post('/__permissions_fixture__/reset')).ok()).toBe(true);
  await page.goto('/login?next=/admin/users/2');
  await page.getByLabel('Username', {exact: true}).fill('fixture-admin');
  await page.getByLabel('Password', {exact: true}).fill('fixture-password');
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await expect(page.getByRole('button', {name: 'View as user', exact: true})).toBeVisible();
});

async function asOperator(page) {
  await page.goto('/admin/users/2');
  await page.getByRole('button', {name: 'View as user', exact: true}).click();
  await expect(page).toHaveURL(/\/routing$/);
  await page.getByRole('link', {name: 'Presets', exact: true}).click();
  await expect(page.locator('#preset-grid .media-tile')).toHaveCount(2);
}

test('Only assigned presets appear; a fixed preset confirms before image and API actions', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await asOperator(page);
  await expect(page.getByRole('button', {name: 'Select Private preset', exact: true})).toHaveCount(0);
  expect((await page.request.get('/api/media')).status()).toBe(403);
  await page.getByRole('button', {name: "Select Mother's Day", exact: true}).click();
  const modal = page.locator('#preset-confirm-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toHaveCSS('opacity', '1');
  await expect(modal).toContainText('Foyer');
  await expect(modal).toContainText('Start the welcome timer');
  await expect(modal).toContainText('Other screens using the same media player will also show this image.');
  expect((await (await request.get('/__permissions_fixture__/state')).json()).actions).toEqual([]);
  await page.screenshot({path: testInfo.outputPath('preset-confirmation.png'), fullPage: true, animations: 'disabled'});
  await modal.getByRole('button', {name: 'Cancel', exact: true}).click();
  await expect(modal).toBeHidden();
  expect((await (await request.get('/__permissions_fixture__/state')).json()).actions).toEqual([]);
  await page.getByRole('button', {name: "Select Mother's Day", exact: true}).click();
  await modal.getByRole('button', {name: 'Confirm & apply', exact: true}).click();
  await expect(page).toHaveURL(/\/routing\?preset_job=/);
  await expect(page.locator('#routing-status')).toHaveText('Preset applied successfully.');
  expect((await (await request.get('/__permissions_fixture__/state')).json()).actions).toEqual([{preset: 1}]);
  expect(errors).toEqual([]);
});

test('A preset without a destination uses the restricted Routing output picker', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await asOperator(page);
  await page.getByRole('button', {name: 'Select Team Night', exact: true}).click();
  await expect(page).toHaveURL(/\/routing\?preset=/);
  await expect(page.locator('#routing-outputs button')).toHaveCount(1);
  await expect(page.locator('#routing-output-step')).toContainText('Choose an output for Team Night');
  await page.screenshot({path: testInfo.outputPath('preset-output-picker.png'), fullPage: true});
  await page.locator('#routing-outputs button').click();
  await expect(page.locator('#preset-confirm-modal')).toBeVisible();
  await expect(page.locator('#preset-confirm-copy')).toContainText('Team Night');
  await page.getByRole('button', {name: 'Confirm & apply', exact: true}).click();
  await expect(page).toHaveURL(/\/routing\?preset_job=/);
  expect(errors).toEqual([]);
});

test('Admin configures a preset and assigns it from the group Routing tab', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await page.goto('/config/routing-presets');
  await expect(page.locator('#preset-fields')).toBeEnabled();
  await page.getByLabel('Preset name', {exact: true}).fill('Team welcome');
  await page.getByLabel('Description (optional)', {exact: true}).fill('Show the welcome graphic and start the timer.');
  await page.locator('#preset-image').selectOption({label: 'Welcome'});
  await page.locator('#preset-output').selectOption('1');
  await page.locator('#preset-actions-panel summary').click();
  await page.getByRole('button', {name: 'Add action', exact: true}).click();
  await page.getByLabel('Action description', {exact: true}).fill('Start welcome timer');
  await page.getByLabel('TDeck API path', {exact: true}).fill('/api/timers/apply');
  await page.getByLabel('JSON body (optional)', {exact: true}).fill('{"preset": 1}');
  await page.getByRole('button', {name: 'Save preset', exact: true}).click();
  await expect(page.locator('#preset-config-message')).toContainText('Preset saved.');
  await expect(page.getByRole('button', {name: 'Team welcome', exact: true})).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('preset-config.png'), fullPage: true});
  await page.reload();
  await page.getByRole('button', {name: 'Team welcome', exact: true}).click();
  await expect(page.getByLabel('TDeck API path', {exact: true})).toHaveValue('/api/timers/apply');
  await page.goto('/admin/permissions?tab=groups#role-68');
  const form = page.locator('[data-role-form][data-role-id="68"]');
  await form.getByRole('tab', {name: 'Routing', exact: true}).click();
  const saved = page.waitForResponse(response => response.url().endsWith('/api/admin/groups/68') && response.request().method() === 'POST');
  await form.getByLabel('Team welcome', {exact: true}).check();
  expect((await saved).ok()).toBe(true);
  await page.goto('/admin/users/2');
  await page.getByRole('button', {name: 'View as user', exact: true}).click();
  await page.getByRole('link', {name: 'Presets', exact: true}).click();
  await expect(page.getByRole('button', {name: 'Select Team welcome', exact: true})).toBeVisible();
  expect(errors).toEqual([]);
});
