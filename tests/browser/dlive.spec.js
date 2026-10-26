const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');
const prefix = '/__dlive_fixture__/';
test.setTimeout(60000);

test.beforeEach(async ({page}) => {
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.fixture).toBe('tdeck-dlive');
  expect(health.isolated).toBe(true);
  expect(health.checkout_db_unchanged).toBe(true);
  expect((await page.request.post(prefix + 'reset')).ok()).toBe(true);
  expect((await page.request.get(prefix + 'login')).ok()).toBe(true);
  await installCommonReadMocks(page);
});

function commands(messages) {
  return messages.filter(m => m.startsWith('b5') || m.startsWith('95') || m.startsWith('f000001a50100100030d'));
}

test('actual dLive socket readbacks drive existing grouped performer controls', async ({page}, info) => {
  const errors = collectPageErrors(page);
  await page.goto('/personal-mixes');
  await page.getByRole('button', {name: 'IEM', exact: true}).click();
  await expect(page.locator('#digico-mixer-connection')).toContainText('online');
  await expect(page.getByRole('tab')).toHaveCount(2);
  await expect(page.locator('#digico-level-1')).toBeEnabled({timeout: 15000});
  await expect(page.locator('#digico-pan-1')).toBeDisabled();
  await expect(page.locator('.digico-send-toggle').first()).toHaveText('Unavailable');
  await expect(page.locator('#digico-mixer-notice')).toContainText('Per-send mute');
  expect(commands((await (await page.request.get(prefix + 'health')).json()).messages)).toEqual([]);
  await page.getByRole('tab').nth(1).click();
  await expect(page.locator('.digico-channel:visible')).toHaveCount(2);
  const write = page.waitForResponse(r => r.url().includes('/aux/2002/channel/3/level') &&
    r.request().method() === 'POST' && r.request().postDataJSON().final === true);
  await page.locator('#digico-level-3').press('ArrowRight');
  await page.locator('#digico-level-3').press('Tab');
  expect((await write).ok()).toBe(true);
  await expect.poll(async () => commands((await (await page.request.get(prefix + 'health')).json()).messages).length).toBeGreaterThan(0);
  expect((await page.request.post(prefix + 'feedback')).ok()).toBe(true);
  await expect(page.locator('.digico-channel:visible').first().locator('.digico-value').first()).toContainText('-10.2 dB');
  expect((await page.request.get('/api/digico/aux/1001/state')).status()).toBe(403);
  await page.screenshot({path: info.outputPath('dlive-performer.png'), fullPage: true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expect(errors).toEqual([]);
});

test('setup selects provider and saves explicit mappings without audio writes', async ({page}, info) => {
  expect((await page.request.get(prefix + 'login?admin=1')).ok()).toBe(true);
  await page.goto('/config/digico');
  await expect(page.locator('#personal-mix-provider')).toHaveValue('dlive');
  await expect(page.locator('#dlive-aux-mappings [data-mapping]')).toHaveCount(2);
  await page.locator('#dlive-aux-mappings [data-key="label"]').first().fill('Lead IEM');
  const save = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method() === 'POST');
  await page.locator('#digico-setup-save').click();
  expect((await save).ok()).toBe(true);
  await expect(page.locator('#digico-setup-message')).toContainText('saved');
  await page.reload();
  await expect(page.locator('#dlive-aux-mappings [data-key="label"]').first()).toHaveValue('Lead IEM');
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.config.dlive_auxes[0].channel).toBe(2002);
  expect(commands(health.messages)).toEqual([]);
  await page.screenshot({path: info.outputPath('dlive-setup.png'), fullPage: true});
  await page.locator('#personal-mix-provider').selectOption('digico');
  await expect(page.locator('#dlive-setup')).toBeHidden();
  await expect(page.locator('#digico-legacy-connection')).toBeVisible();
  expect((await (await page.request.get(prefix + 'health')).json()).config.personal_mix_provider).toBe('dlive');
});

test('overcapacity read demand has actionable feedback without fabricated levels', async ({page}, info) => {
  expect((await page.request.get(prefix + 'login?admin=1')).ok()).toBe(true);
  const fixtureURL = (await page.request.get(prefix + 'health')).url();
  const setup = await page.request.post('/api/digico/setup', {
    headers: {'X-CSRF-Token': 'dlive-csrf', Origin: new URL(fixtureURL).origin},
    data: {dlive_request_interval: .2, dlive_stale_after: 2,
      dlive_channels: Array.from({length: 128}, (_, i) => ({channel: i + 1, label: `Input ${i + 1}`}))},
  });
  expect(setup.ok()).toBe(true);
  expect((await page.request.get(prefix + 'login')).ok()).toBe(true);
  await page.goto('/personal-mixes');
  await page.getByRole('button', {name: 'IEM', exact: true}).click();
  await expect(page.locator('#digico-mixer-notice')).toContainText('refresh budget', {timeout: 15000});
  await expect(page.locator('#digico-mixer-notice')).toContainText('Reduce active AUXes/inputs');
  const state = await (await page.request.get('/api/digico/aux/2002/state')).json();
  expect(state.status.polling.activeAuxes).toBe(1);
  expect(state.status.polling.fields).toBe(130);
  expect(commands((await (await page.request.get(prefix + 'health')).json()).messages)).toEqual([]);
  await page.screenshot({path: info.outputPath('dlive-capacity-warning.png'), fullPage: false});
});
