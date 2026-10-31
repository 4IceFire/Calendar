const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');
const prefix = '/__personal_mix_tabs_fixture__/';

test.beforeEach(async ({page}) => {
  const health = await page.request.get(prefix + 'health');
  expect(health.ok()).toBe(true);
  const probe = await health.json();
  expect(probe.fixture).toBe('tdeck-personal-mix-tabs');
  expect(probe.isolated).toBe(true);
  expect(probe.checkout_db_unchanged).toBe(true);
  const reset = await page.request.post(prefix + 'reset');
  expect(reset.ok()).toBe(true);
  expect((await reset.json()).ok).toBe(true);
  expect((await page.request.get(prefix + 'login')).ok()).toBe(true);
  await installCommonReadMocks(page);
});

async function mix(page) {
  await page.goto('/personal-mixes');
  await page.getByRole('button', {name: 'Drums', exact: true}).click();
  await expect(page.locator('#digico-mixer-connection')).toContainText('Desk online');
}

test('group tabs select only one ordered section without device writes', async ({page}) => {
  const errors = collectPageErrors(page);
  await mix(page);
  const tabs = page.getByRole('tab');
  await expect(tabs).toHaveCount(10);
  expect(await tabs.allTextContents()).toEqual(['Vocals24 inputs', 'Band2 inputs', 'Keys1 input', 'Tracks1 input', 'Acoustic1 input', 'Electric1 input', 'Brass1 input', 'Strings1 input', 'FX1 input', 'Vocals1 input']);
  await expect(page.getByRole('tabpanel')).toHaveCount(1);
  await expect(page.locator('.digico-channel:visible')).toHaveCount(24);
  await tabs.nth(1).click();
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.digico-channel:visible')).toHaveCount(2);
  await expect(page.locator('.digico-channel:visible').first()).toContainText('Input 25');
  expect((await (await page.request.get(prefix + 'health')).json()).writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('case-sensitive and unusual headings retain unique accessible panels', async ({page}) => {
  expect((await page.request.post(prefix + 'reset?mode=names')).ok()).toBe(true);
  await mix(page);
  const tabs = page.getByRole('tab');
  await expect(tabs).toHaveCount(4);
  expect(await tabs.allTextContents()).toEqual(['Vocals1 input', 'vocals1 input', '<FX> / 🎸1 input', 'Vocals1 input']);
  for (let n = 0; n < 4; n++) {
    await tabs.nth(n).click();
    await expect(page.getByRole('tabpanel')).toHaveCount(1);
    await expect(page.getByRole('tabpanel')).toContainText(`Input ${n + 1}`);
    const target = await tabs.nth(n).getAttribute('aria-controls');
    await expect(page.locator(`[id="${target}"]`)).toHaveCount(1);
    expect(/\s/.test(target)).toBe(false);
  }
});

test('no enabled inputs shows an honest empty state without empty tabs', async ({page}) => {
  expect((await page.request.post(prefix + 'reset?mode=empty')).ok()).toBe(true);
  await mix(page);
  await expect(page.getByRole('tablist')).toHaveCount(0);
  await expect(page.getByRole('tab')).toHaveCount(0);
  await expect(page.locator('#digico-channel-groups')).toContainText('No enabled inputs are available for this mix.');
  expect((await (await page.request.get(prefix + 'health')).json()).writes).toEqual([]);
});

test('unnamed inputs use one Inputs tab and do not resurrect saved collapse state', async ({page}) => {
  expect((await page.request.post(prefix + 'reset?mode=ungrouped')).ok()).toBe(true);
  await page.addInitScript(() => localStorage.setItem('tdeck.digico.collapsed-sections', '["inputs-1"]'));
  await mix(page);
  await expect(page.getByRole('tab')).toHaveCount(1);
  await expect(page.getByRole('tab')).toHaveText('Inputs2 inputs');
  await expect(page.locator('.digico-channel:visible')).toHaveCount(2);
  await expect(page.locator('.digico-channel details')).toHaveCount(0);
});

test('state and meter polling update hidden groups without rebuilding selection or focus', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.clock.install();
  await mix(page);
  const first = page.getByRole('tab').first();
  await first.focus();
  await first.evaluate(el => { el.dataset.pollSentinel = 'original'; });
  expect((await page.request.post(prefix + 'feedback')).ok()).toBe(true);
  // Band stays hidden, but its existing fader and meter must still receive readbacks.
  await expect(page.locator('#digico-level-25')).toHaveValue(/0\.\d+/);
  await expect.poll(() => page.locator('#digico-pan-25').inputValue()).toBe('0.75');
  await expect(page.locator('[role="meter"]').nth(24)).toHaveAttribute('aria-valuenow', '84.0');
  await expect(first).toBeFocused();
  await expect(first).toHaveAttribute('data-poll-sentinel', 'original');
  await expect(first).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab').nth(1).click();
  await expect(page.locator('.digico-channel:visible').first().locator('.digico-value').first()).toHaveText('-6.0 dB');
  await expect(page.locator('.digico-channel:visible').first().getByRole('button')).toHaveText('Muted');
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.reads.state).toBeGreaterThan(1);
  expect(health.reads.meters).toBeGreaterThan(1);
  expect(health.writes).toEqual([]);
  // Exercise the existing 30-second config refresh without sleeping 30 seconds.
  const refreshed = page.waitForResponse(r => r.url().endsWith('/api/digico/mixer/config'));
  await page.clock.fastForward(31000);
  await refreshed;
  await expect(page.getByRole('tab').nth(1)).toHaveAttribute('aria-selected', 'true');
  expect(errors).toEqual([]);
});

test('AUX selection and authorized level pan mute controls keep their original targets', async ({page}) => {
  await mix(page);
  await expect(page.locator('#digico-aux-grid button')).toHaveCount(2);
  await page.getByRole('tab').nth(1).click();
  await page.getByRole('button', {name: 'Choose another AUX'}).click();
  await page.getByRole('button', {name: 'Vocals', exact: true}).click();
  await expect(page.locator('#digico-aux-current-label')).toHaveText('Vocals');
  await expect(page.getByRole('tab').nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.digico-pan-control:visible')).toHaveCount(0);
  expect((await (await page.request.get(prefix + 'health')).json()).writes).toEqual([]);
  await page.getByRole('button', {name: 'Choose another AUX'}).click();
  await page.getByRole('button', {name: 'Drums', exact: true}).click();
  await expect(page.locator('.digico-pan-control:visible')).toHaveCount(2);
  const levelWrite = page.waitForResponse(r => r.url().endsWith('/aux/1/channel/25/level') && r.request().method() === 'POST');
  await page.locator('#digico-level-25').press('ArrowRight');
  expect((await levelWrite).ok()).toBe(true);
  const panWrite = page.waitForResponse(r => r.url().endsWith('/aux/1/channel/25/pan') && r.request().method() === 'POST');
  await page.locator('#digico-pan-25').press('ArrowRight');
  expect((await panWrite).ok()).toBe(true);
  const muteWrite = page.waitForResponse(r => r.url().endsWith('/aux/1/channel/25/on') && r.request().method() === 'POST');
  await page.locator('.digico-channel:visible').first().getByRole('button').click();
  expect((await muteWrite).ok()).toBe(true);
  await expect(page.locator('.digico-channel:visible').first().getByRole('button')).toHaveText('Muted');
  const writes = (await (await page.request.get(prefix + 'health')).json()).writes;
  expect(writes.length).toBeGreaterThanOrEqual(3);
  expect(new Set(writes.map(w => `${w.aux}:${w.channel}`))).toEqual(new Set(['1:25']));
  expect(new Set(writes.map(w => w.field))).toEqual(new Set(['level', 'pan', 'sendOn']));
});

test('AUX and page permissions still reject direct unauthorized requests', async ({page, baseURL}) => {
  const response = await page.request.get('/api/digico/mixer/config');
  expect(response.ok()).toBe(true);
  expect((await response.json()).auxes.map(aux => aux.channel)).toEqual([1, 2]);
  expect((await page.request.get('/api/digico/aux/3/state')).status()).toBe(403);
  expect((await page.request.post('/api/digico/aux/3/channel/1/level', {
    headers: {'X-CSRF-Token': 'mix-tabs-csrf', Origin: new URL(baseURL).origin},
    data: {value: -12, final: true}
  })).status()).toBe(403);
  expect((await page.request.get(prefix + 'login?role=denied')).ok()).toBe(true);
  expect((await page.request.get('/personal-mixes')).status()).toBe(403);
  expect((await page.request.get('/api/digico/mixer/config')).status()).toBe(403);
  expect((await (await page.request.get(prefix + 'health')).json()).writes).toEqual([]);
});

test('keyboard tabs wrap, reveal overflow and expose the selected panel', async ({page}) => {
  await mix(page);
  const tabs = page.getByRole('tab');
  await tabs.first().focus();
  await tabs.first().press('ArrowRight');
  await expect(tabs.nth(1)).toBeFocused();
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('[role="tab"][tabindex="0"]')).toHaveCount(1);
  await tabs.nth(1).press('End');
  await expect(tabs.last()).toBeFocused();
  await expect(tabs.last()).toHaveAttribute('aria-selected', 'true');
  const overflowTab = await tabs.last().boundingBox();
  const list = await page.getByRole('tablist').boundingBox();
  expect(overflowTab.x).toBeGreaterThanOrEqual(list.x);
  expect(overflowTab.x + overflowTab.width).toBeLessThanOrEqual(list.x + list.width + 1);
  await tabs.last().press('ArrowRight');
  await expect(tabs.first()).toBeFocused();
  await tabs.first().press('ArrowLeft');
  await expect(tabs.last()).toBeFocused();
  await tabs.last().press('Home');
  await expect(tabs.first()).toBeFocused();
  const panelId = await tabs.first().getAttribute('aria-controls');
  await tabs.first().press('Tab');
  await expect(page.locator(`[id="${panelId}"]`)).toBeFocused();
  await expect(page.locator(`[id="${panelId}"]`)).toHaveAttribute('aria-labelledby', await tabs.first().getAttribute('id'));
  expect((await (await page.request.get(prefix + 'health')).json()).writes).toEqual([]);
});

test('wide and short desktop plus narrow mobile keep the strip and controls within the viewport', async ({page}, testInfo) => {
  const errors = collectPageErrors(page);
  await mix(page);
  for (const [width, height] of [[1920, 1080], [2540, 900], [1280, 600], [390, 844]]) {
    await page.setViewportSize({width, height});
    await page.evaluate(() => window.scrollTo(0, 1600));
    await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(1500);
    const navbar = await page.locator('.tdeck-navbar').boundingBox();
    const toolbar = await page.locator('.digico-mixer-topbar').boundingBox();
    const tabs = await page.getByRole('tablist').boundingBox();
    expect(Math.abs(toolbar.y - navbar.height)).toBeLessThan(2);
    expect(Math.abs(tabs.y - toolbar.y - toolbar.height)).toBeLessThan(2);
    expect(tabs.y + tabs.height).toBeLessThan(height / 2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path: testInfo.outputPath(`viewport-${width}x${height}.png`)});
  }
  expect(errors).toEqual([]);
});

test('tabs stay directly below the mixer toolbar at deep scroll and overflow only horizontally', async ({page}, testInfo) => {
  await mix(page);
  const tabs = page.getByRole('tablist');
  const toolbar = page.locator('.digico-mixer-topbar');
  const initial = await tabs.boundingBox();
  const header = await toolbar.boundingBox();
  expect(Math.abs(initial.y - header.y - header.height)).toBeLessThan(2);
  await page.evaluate(() => window.scrollTo(0, 1600));
  await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(1500);
  await expect.poll(async () => (await tabs.boundingBox()).y).toBeGreaterThan(0);
  const pinned = await tabs.boundingBox();
  const pinnedHeader = await toolbar.boundingBox();
  expect(Math.abs(pinned.y - pinnedHeader.y - pinnedHeader.height)).toBeLessThan(2);
  expect(await page.evaluate(() => scrollY)).toBeGreaterThan(500);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('sticky-deep.png')});
  await page.setViewportSize({width: 320, height: 720});
  await expect.poll(() => tabs.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  await tabs.evaluate(el => { el.scrollLeft = el.scrollWidth; });
  expect(await tabs.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
  await page.getByRole('tab').last().click();
  await expect(page.locator('.digico-channel:visible')).toHaveCount(1);
  const card = await page.locator('.digico-channel:visible').boundingBox();
  const bar = await tabs.boundingBox();
  expect(card.y).toBeGreaterThanOrEqual(bar.y + bar.height);
  expect(card.y).toBeLessThan(720);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('overflow-320.png')});
});
