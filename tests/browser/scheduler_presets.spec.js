const {test, expect} = require('@playwright/test');
const {collectPageErrors} = require('./helpers');
let fixture;
test.beforeEach(async ({request}) => {
  fixture = await (await request.get('/__scheduler_fixture__/health')).json();
  test.skip(fixture.fixture !== 'tdeck-scheduler-ui', 'Run scheduler_ui_harness.py on port 5067.');
  expect((await request.post('/__scheduler_fixture__/reset')).ok()).toBe(true);
});

test('Calendar editor displays ordered preset types and persists destination on edit', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await page.goto('/calendar/new');
  await page.locator('#add-trigger-btn').click();
  const row = page.locator('.trigger-row').last();
  await expect(row.locator('[name="actionType"] option')).toHaveText(['Companion Button', 'Timer Preset', 'VideoHub Preset', 'Routing Presets', 'API Call']);
  await row.locator('[name="actionType"]').selectOption('routing_preset');
  await row.getByLabel('Saved preset').selectOption(fixture.optional);
  await row.getByLabel('Preset destination').selectOption('3');
  await expect(row.locator('.companion-fields')).toBeHidden();
  await expect(row.locator('.preset-action-note')).toContainText('screens sharing it');
  await page.screenshot({path: testInfo.outputPath('calendar-preset.png'), fullPage: true});
  expect(await row.locator('.preset-action-fields').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  // Create through the real API, then verify editor restoration and browser saving.
  const saved = await (await request.post('/api/ui/events', {data: {name: 'Fixture service', day: 'Sunday', date: '2026-09-20', time: '10:00', repeating: true,
    times: [{uid: 'fixture-cue', minutes: 60, typeOfTrigger: 'BEFORE', actionType: 'routing_preset', preset_action: {preset: fixture.optional, output: 3}}]}})).json();
  expect(saved.ok).toBe(true);
  await page.goto('/calendar/edit/' + saved.id);
  await expect(page.getByLabel('Saved preset')).toHaveValue(fixture.optional);
  await expect(page.getByLabel('Preset destination')).toHaveValue('3');
  await page.locator('.trigger-row [name="actionType"]').selectOption('videohub_preset');
  await page.getByLabel('Saved preset').selectOption('9');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#event-form button[type="submit"]').click();
  await expect(page).toHaveURL(/\/calendar$/);
  const edited = await (await request.get('/api/events/' + saved.id)).json();
  expect(edited.times[0].uid).toBe('fixture-cue');
  expect(edited.times[0].preset_action).toEqual({preset: 9});
  expect(errors).toEqual([]);
});

test('Trigger templates save both preset types and retain fixed output and cue IDs', async ({page, request}, testInfo) => {
  const errors = collectPageErrors(page);
  await page.goto('/templates#triggers');
  await page.locator('#trigger-template-new').click();
  await page.locator('#trigger-template-label').fill('Preset automation');
  await page.locator('#trigger-template-add-trigger').click();
  const row = page.locator('#trigger-editor-triggers-container .trigger-row').last();
  await row.locator('[name="typeOfTrigger"]').selectOption('AT');
  await row.locator('[name="actionType"]').selectOption('routing_preset');
  let warning = '';
  page.once('dialog', async dialog => { warning = dialog.message(); await dialog.accept(); });
  await page.locator('#trigger-template-save').click();
  expect(warning).toContain('Choose a saved preset');
  await row.getByLabel('Saved preset').selectOption(fixture.fixed);
  await expect(row.getByLabel('Preset destination')).toHaveValue('2');
  await expect(row.getByLabel('Preset destination')).toBeDisabled();
  await page.screenshot({path: testInfo.outputPath('template-preset.png'), fullPage: true});
  await page.locator('#trigger-template-save').click();
  await expect.poll(async () => (await (await request.get('/api/templates')).json()).triggers.length).toBe(1);
  const initial = (await (await request.get('/api/templates')).json()).triggers[0];
  expect(initial.times[0].preset_action).toEqual({preset: fixture.fixed, output: 2});
  await expect.poll(() => page.evaluate(() => window.__selectedTriggerIndex)).toBe(0);
  await row.locator('[name="actionType"]').selectOption('videohub_preset');
  await row.getByLabel('Saved preset').selectOption('9');
  await page.locator('#trigger-template-save').click();
  await expect.poll(async () => (await (await request.get('/api/templates')).json()).triggers[0].times[0].actionType).toBe('videohub_preset');
  const updated = (await (await request.get('/api/templates')).json()).triggers[0];
  expect(updated.id).toBe(initial.id);
  expect(updated.times[0].uid).toBe(initial.times[0].uid);
  expect(updated.times[0].preset_action).toEqual({preset: 9});
  expect(errors).toEqual([]);
});
