const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');

test.beforeEach(async ({page}) => {
  const response = await page.request.get('/__default_views_fixture__/health');
  const probe = await response.json().catch(() => ({}));
  test.skip(probe.fixture !== 'tdeck-default-views-ui', 'Run tests/default_views_ui_harness.py on port 5068 with workers=1.');
  await page.request.post('/__default_views_fixture__/reset');
  await page.request.get('/__default_views_fixture__/login');
  await installCommonReadMocks(page);
});

async function saveDefaults(page, kind) {
  const response = page.waitForResponse(r => r.url().endsWith('/api/admin/default-views/' + kind) && r.request().method() === 'PUT');
  await page.getByRole('button', {name: 'Save defaults'}).click();
  expect((await response).ok()).toBe(true);
}

test('Routing defaults persist and Show all reveals choices without changing access', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/routing');
  await expect(page.locator('#routing-outputs button')).toHaveCount(2);
  await page.getByRole('button', {name: 'Configure defaults'}).click();
  await page.locator('[data-default-field="outputs"][value="1"]').check();
  await page.locator('[data-default-field="inputs"][value="2"]').check();
  await saveDefaults(page, 'routing');
  await expect(page.locator('#routing-outputs button')).toHaveCount(1);
  await page.reload();
  await expect(page.locator('#routing-outputs button')).toHaveCount(1);
  await page.getByLabel('Show all', {exact: true}).check();
  await expect(page.locator('#routing-outputs button')).toHaveCount(2);
  await page.getByLabel('Show all', {exact: true}).uncheck();
  await page.locator('#routing-outputs button').click();
  await expect(page.locator('#routing-inputs button')).toHaveCount(1);
  await page.getByLabel('Show all', {exact: true}).check();
  await expect(page.locator('#routing-inputs button')).toHaveCount(3);
  await page.request.get('/__default_views_fixture__/login?role=operator');
  await page.goto('/routing');
  await expect(page.locator('#routing-default-view')).toHaveCount(0);
  await expect(page.locator('#routing-outputs button')).toHaveCount(1);
  await page.locator('#routing-outputs button').click();
  await expect(page.locator('#routing-inputs button')).toHaveCount(2);
  expect(errors).toEqual([]);
});

test('Master spans the page above Monitor and retains volume control; defaults persist', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/foyer-audio');
  const master = page.locator('#foyer-audio-master [data-source-id="master"]');
  const monitor = page.locator('.foyer-audio-monitor').filter({has: page.locator('[data-foyer-monitor-volume]')});
  await expect(master).toBeVisible();
  const masterBounds = await master.boundingBox();
  const monitorBounds = await monitor.boundingBox();
  expect(masterBounds.y + masterBounds.height).toBeLessThanOrEqual(monitorBounds.y);
  expect(Math.abs(masterBounds.width - monitorBounds.width)).toBeLessThan(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.route('**/api/atem/audio/volume', route => route.fulfill({contentType: 'application/json', body: '{"ok":true}'}));
  const change = page.waitForRequest(r => r.url().includes('/api/atem/audio/volume'));
  await master.locator('input[type="range"]').focus();
  await master.locator('input[type="range"]').press('ArrowRight');
  expect((await change).postDataJSON().source_id).toBe('master');
  await page.getByRole('button', {name: 'Configure defaults'}).click();
  await page.locator('[data-default-field="sources"][value="1"]').check();
  await saveDefaults(page, 'audio');
  await expect(master).toHaveCount(0);
  await expect(page.locator('#foyer-audio-grid [data-source-id]')).toHaveCount(1);
  await expect(monitor).toBeVisible();
  await page.reload();
  await expect(page.locator('#foyer-audio-grid [data-source-id]')).toHaveCount(1);
  await page.getByLabel('Show all faders').check();
  await expect(master).toBeVisible();
  await expect(page.locator('#foyer-audio-grid [data-source-id]')).toHaveCount(2);
  await page.screenshot({path: test.info().outputPath('record-audio.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('Record Audio nonadmin cannot reveal Master or Monitor outside grants', async ({page}) => {
  await page.request.get('/__default_views_fixture__/login?role=operator');
  await page.goto('/foyer-audio');
  await expect(page.locator('#foyer-audio-grid [data-source-id="1"]')).toBeVisible();
  await expect(page.locator('[data-source-id="master"]')).toHaveCount(0);
  await expect(page.locator('[data-foyer-monitor-volume]')).toHaveCount(0);
  await expect(page.getByLabel('Show all faders')).toHaveCount(0);
});
