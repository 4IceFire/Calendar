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
  await page.locator('[data-bs-target="#digico-tab-auxes"]').click();
  await page.locator('#dlive-aux-mappings [data-key="label"]').first().fill('Lead IEM');
  const save = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method() === 'POST');
  await page.locator('#digico-setup-save').click();
  expect((await save).ok()).toBe(true);
  await expect(page.locator('#digico-setup-message')).toContainText('saved');
  await page.reload();
  await expect(page.locator('#dlive-aux-mappings [data-mapping]')).toHaveCount(2);
  await page.locator('[data-bs-target="#digico-tab-auxes"]').click();
  await expect(page.locator('#dlive-aux-mappings [data-key="label"]').first()).toHaveValue('Lead IEM');
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.config.dlive_auxes[0].channel).toBe(2002);
  expect(commands(health.messages)).toEqual([]);
  await expect(page.locator('#digico-tab-auxes')).toHaveCSS('opacity', '1');
  await page.evaluate(() => window.scrollTo({top: 0, behavior: 'instant'}));
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.screenshot({path: info.outputPath('dlive-setup.png'), fullPage: true});
  await page.locator('[data-bs-target="#digico-tab-connection"]').click();
  await page.locator('#personal-mix-provider').selectOption('digico');
  await expect(page.locator('#dlive-setup')).toBeHidden();
  await expect(page.locator('#digico-legacy-connection')).toBeVisible();
  expect((await (await page.request.get(prefix + 'health')).json()).config.personal_mix_provider).toBe('dlive');
});

test('dLive arrangement editor saves colours, icons and section headings by stable mapping', async ({page}, info) => {
  const errors = collectPageErrors(page);
  await page.setViewportSize({width: info.project.name.startsWith('mobile') ? 320 : 1920, height: 900});
  expect((await page.request.get(prefix + 'login?admin=1')).ok()).toBe(true);
  await page.goto('/config/digico');
  const auxTab = page.locator('[data-bs-target="#digico-tab-auxes"]');
  await expect(auxTab).toBeEnabled();
  const rows = page.locator('#dlive-aux-mappings [data-mapping]');
  await expect(rows).toHaveCount(2);
  await auxTab.click();
  await expect(page.locator('#digico-tab-auxes')).toHaveCSS('opacity', '1');
  await rows.first().getByLabel('Group').fill('Stage');
  await rows.first().getByLabel('Colour').fill('#cc4422');
  await rows.first().getByRole('button', {name: /Choose AUX icon/}).click();
  await page.locator('.digico-icon-option[data-icon="headset"]').click();
  await rows.first().getByRole('button', {name: /Move .* down/}).click();
  await expect(rows.first().locator('[data-key="bus"]')).toHaveValue('1');
  await page.locator('[data-bs-target="#digico-tab-channels"]').click();
  const channels = page.locator('#dlive-input-mappings [data-mapping]');
  await channels.nth(2).getByLabel('Section heading').fill('Instruments');
  await channels.nth(2).getByRole('button', {name: /Move .* up/}).click();
  const save = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method() === 'POST');
  await page.locator('#digico-setup-save').click();
  expect((await save).ok()).toBe(true);
  await expect(page.locator('#digico-setup-message')).toContainText('saved');
  await page.reload();
  await expect(page.locator('#dlive-aux-mappings [data-mapping]')).toHaveCount(2);
  await page.locator('[data-bs-target="#digico-tab-auxes"]').click();
  await expect(rows.nth(1).getByLabel('Group')).toHaveValue('Stage');
  await expect(rows.nth(1).getByLabel('Colour')).toHaveValue('#cc4422');
  await expect(rows.nth(1).locator('.digico-item-icon')).toHaveValue('headset');
  await expect(page.locator('#digico-tab-auxes')).toHaveCSS('opacity', '1');
  await page.evaluate(() => window.scrollTo({top: 0, behavior: 'instant'}));
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.screenshot({path: info.outputPath('dlive-arrangement.png'), fullPage: true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.config.dlive_auxes.map(a => a.channel)).toEqual([1001, 2002]);
  expect(health.config.dlive_auxes[1]).toMatchObject({bus: 2, stereo: true, group: 'Stage', colour: '#cc4422', icon: 'headset', order: 2});
  expect(health.config.dlive_channels.map(c => c.channel)).toEqual([1, 3, 2, 4]);
  expect(health.config.dlive_channels[1].group).toBe('Instruments');
  expect(commands(health.messages)).toEqual([]);
  await page.goto('/personal-mixes');
  await expect(page.getByRole('heading', {name: 'Stage', exact: true})).toBeVisible();
  const tiles = page.locator('.digico-aux-tile');
  await expect(tiles).toHaveText(['Mono IEM', 'IEM']);
  expect(await tiles.nth(1).evaluate(el => el.style.getPropertyValue('--tile-tint'))).toBe('#cc4422');
  await page.screenshot({path: info.outputPath('dlive-grouped-picker.png'), fullPage: true});
  await tiles.nth(1).click();
  await expect(page.getByRole('tab', {name: 'Instruments'})).toBeVisible();
  expect(errors).toEqual([]);
});

test('permission picker keeps hidden grants restricted and uses explicit All', async ({page, request}, info) => {
  const errors = collectPageErrors(page);
  expect((await page.request.get(prefix + 'login?admin=1')).ok()).toBe(true);
  expect((await page.request.post(prefix + 'grants', {data: ['1']})).ok()).toBe(true);
  await page.goto('/admin/permissions?tab=groups#role-2');
  await page.locator('[data-role-select][data-role-id="2"]').click();
  const form = page.locator('[data-role-form][data-role-id="2"]');
  await form.locator('[data-permission-tab="digico"]').click();
  const auxes = form.locator('[name="digico_allowed_auxes_role"]');
  expect(await auxes.evaluateAll(nodes => nodes.map(n => n.value).sort())).toEqual(['1001', '2002']);
  const all = form.getByLabel('All enabled AUXes on this desk');
  await expect(all).not.toBeChecked();
  // An unrelated change sends an empty visible AUX selection, never All.
  const saved = page.waitForResponse(r => r.url().endsWith('/api/admin/groups/2') && r.request().method() === 'POST');
  await form.locator('[data-permission-tab="general"]').click();
  await form.locator('input[name="page_keys"][value="page:home"]').check();
  expect((await saved).ok()).toBe(true);
  expect((await (await page.request.get(prefix + 'health')).json()).grants).toEqual(['1']);
  expect((await request.get(prefix + 'login')).ok()).toBe(true);
  expect((await (await request.get('/api/digico/mixer/config')).json()).auxes).toEqual([]);
  expect((await request.get('/api/digico/aux/2002/state')).status()).toBe(403);
  await form.locator('[data-permission-tab="digico"]').click();
  for (const selected of [true, false]) {
    const save = page.waitForResponse(r => r.url().endsWith('/api/admin/groups/2') && r.request().method() === 'POST');
    await form.locator('[name="digico_allowed_auxes_role"][value="2002"]').setChecked(selected);
    expect((await save).ok()).toBe(true);
  }
  const saveAll = page.waitForResponse(r => r.url().endsWith('/api/admin/groups/2') && r.request().method() === 'POST');
  await all.check();
  expect((await saveAll).ok()).toBe(true);
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.grants).toContain('1');
  await page.reload();
  await page.locator('[data-role-select][data-role-id="2"]').click();
  await form.locator('[data-permission-tab="digico"]').click();
  await expect(all).toBeChecked();
  await page.evaluate(() => window.scrollTo({top: 0, behavior: 'instant'}));
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.screenshot({path: info.outputPath('dlive-permissions.png'), fullPage: true});
  expect((await request.get(prefix + 'login')).ok()).toBe(true);
  expect((await (await request.get('/api/digico/mixer/config')).json()).auxes.map(a => a.channel).sort()).toEqual([1001, 2002]);
  const fixtureURL = (await page.request.get(prefix + 'health')).url();
  expect((await page.request.post('/api/digico/setup', {
    headers: {'X-CSRF-Token': 'dlive-csrf', Origin: new URL(fixtureURL).origin},
    data: {personal_mix_provider: 'digico'},
  })).ok()).toBe(true);
  const stale = page.waitForResponse(r => r.url().endsWith('/api/admin/groups/2') && r.request().method() === 'POST');
  await form.locator('[name="digico_allowed_auxes_role"][value="2002"]').check();
  expect((await stale).status()).toBe(409);
  expect((await (await page.request.get(prefix + 'health')).json()).grants).toEqual(health.grants);
  await page.reload();
  await page.locator('[data-role-select][data-role-id="2"]').click();
  await form.locator('[data-permission-tab="digico"]').click();
  expect(await auxes.evaluateAll(nodes => nodes.map(n => n.value))).toEqual(['1']);
  await expect(form.locator('[name="digico_allowed_auxes_role"][value="1"]')).toBeChecked();
  expect(errors).toEqual([]);
  expect(commands((await (await page.request.get(prefix + 'health')).json()).messages)).toEqual([]);
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
