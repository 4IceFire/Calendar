const {test, expect} = require('@playwright/test');
const {installCommonReadMocks, collectPageErrors} = require('./helpers');
const marker = '/__videohub_ignore_fixture__';
// Real-auth fixture work can overlap CPU-heavy regression runs.
test.setTimeout(60000);
test.use({actionTimeout: 20000});
const headers = () => ({Origin: new URL(process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token': 'ignore-csrf'});

async function health(page) {return (await (await page.request.get(marker + '/health')).json());}
async function seed(page, routes) {
  const response = await page.request.post('/api/videohub/presets', {headers: headers(), data: {name: 'Fixture preset', routes}});
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).preset;
}
async function open(page, id) {
  await page.goto(`/videohub?preset=${id}&room=fixture`);
  await expect(page.locator('#vh-name')).toHaveValue('Fixture preset');
  await expect(page.locator('.vh-marker')).toHaveCount(2);
}
async function choose(page, output, input) {
  await page.locator(`.vh-marker[data-output="${output}"]`).click();
  await expect(page.locator('#vhi-grid button')).not.toHaveCount(0);
  await page.locator(`#vhi-grid [data-input="${input}"]`).click();
  await page.waitForURL(url => url.pathname === '/videohub', {timeout: 30000});
  await expect(page.locator(`.vh-marker[data-output="${output}"]`)).toBeVisible({timeout: 20000});
}

test.beforeEach(async ({page}) => {
  expect((await health(page)).fixture).toBe('tdeck-videohub-ignore-ui');
  expect((await page.request.post(marker + '/reset')).ok()).toBe(true);
  expect((await page.request.get(marker + '/login')).ok()).toBe(true);
  await installCommonReadMocks(page);
  page.on('dialog', dialog => dialog.accept());
});

test('choose Ignore, cancel without mutation, save/reload/edit and apply only real routes', async ({page}) => {
  const errors = collectPageErrors(page);
  const preset = await seed(page, Array.from({length: 40}, (_, i) => ({output: i + 1, input: i + 1})));
  await open(page, preset.id);
  await page.locator('.vh-marker[data-output="1"]').click();
  await expect(page.locator('[data-input="ignore"]')).toContainText('Keep Current Routing');
  await page.locator('#vhi-cancel').click();
  await expect(page.locator('.vh-marker[data-output="1"]')).not.toHaveClass(/vh-marker-ignore/);
  await choose(page, 1, 'ignore');
  await choose(page, 2, '7');
  await expect(page.locator('.vh-marker[data-output="1"]')).toHaveClass(/vh-marker-ignore/);
  await expect(page.locator('.vh-marker[data-output="1"]')).toContainText('Keep Current Routing');
  expect((await health(page)).writes).toEqual([]);
  let saved = (await (await page.request.get('/api/videohub/presets')).json()).presets[0];
  expect(saved.routes.slice(0, 2)).toEqual([{output: 1, input: null}, {output: 2, input: 7}]);
  await open(page, preset.id);
  await page.locator('.vh-marker[data-output="1"]').click();
  await expect(page.locator('[data-input="ignore"]')).toHaveClass(/active/);
  await page.locator('#vhi-cancel').click();
  await expect(page.locator('.vh-marker[data-output="1"]')).toHaveClass(/vh-marker-ignore/);
  await expect(page.locator('#vh-name')).toHaveValue('Fixture preset');
  await page.request.post(marker + '/change-live');
  await page.locator('#vh-apply').click();
  await expect(page.locator('#vh-status')).toContainText('Applied preset');
  const actual = await health(page);
  expect(actual.routing[0]).toBe(12);
  expect(actual.routing[1]).toBe(7);
  expect(actual.writes).toHaveLength(1);
  expect(actual.writes[0].routes).toHaveLength(39);
  expect(actual.writes[0].routes.some(([output]) => output === 0)).toBe(false);
  expect(actual.reads).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.locator('.vh-marker-ignore').evaluate(el => {
    const marker = el.getBoundingClientRect();
    const stage = el.parentElement.getBoundingClientRect();
    return marker.left >= stage.left && marker.right <= stage.right;
  })).toBe(true);
  await page.screenshot({path: test.info().outputPath('videohub-ignore.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('all-ignore is a no-op and choosing a real input replaces Ignore', async ({page}) => {
  const preset = await seed(page, Array.from({length: 40}, (_, i) => ({output: i + 1, input: null})));
  await open(page, preset.id);
  await page.locator('#vh-apply').click();
  await expect(page.locator('#vh-status')).toContainText('Applied preset');
  expect((await health(page)).writes).toEqual([]);
  expect((await health(page)).reads).toBe(0);
  await choose(page, 1, '7');
  await expect(page.locator('.vh-marker[data-output="1"]')).not.toHaveClass(/vh-marker-ignore/);
  const saved = (await (await page.request.get('/api/videohub/presets')).json()).presets[0];
  expect(saved.routes[0]).toEqual({output: 1, input: 7});
  expect(saved.routes.slice(1).every(route => route.input === null)).toBe(true);
  expect((await health(page)).writes).toEqual([]);
});

test('Routing Preset picker saves image only, cancels without commands and applies to edited output', async ({page}) => {
  const errors = collectPageErrors(page);
  const preset = await seed(page, Array.from({length: 40}, (_, i) => ({output: i + 1, input: null})));
  await open(page, preset.id);
  await page.locator('.vh-marker[data-output="1"]').click();
  await page.getByRole('button', {name: 'Routing Preset', exact: true}).click();
  await page.locator('#vhi-preset-search').fill('Welcome');
  await page.locator('#vhi-cancel').click();
  await expect(page.locator('.vh-marker[data-output="1"]')).toContainText('Keep Current Routing');
  await page.locator('.vh-marker[data-output="1"]').click();
  await page.getByRole('button', {name: 'Routing Preset', exact: true}).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('videohub-image-picker.png'), fullPage: true});
  await page.getByRole('button', {name: 'Welcome image', exact: true}).click();
  await page.waitForURL(url => url.pathname === '/videohub', {timeout: 30000});
  await expect(page.locator('.vh-marker[data-output="1"]')).toContainText('Welcome image', {timeout: 20000});
  await choose(page, 2, '7');
  const state = await health(page);
  expect(state.writes).toEqual([]);
  expect(state.images).toEqual([]);
  let saved = (await (await page.request.get('/api/videohub/presets')).json()).presets[0];
  expect(saved.routes[0]).toEqual({output: 1, routing_preset: state.routing_preset});
  await open(page, preset.id);
  await expect(page.locator('.vh-marker[data-output="1"]')).toContainText('Welcome image');
  await page.request.post(marker + '/image-ready');
  await page.locator('#vh-apply').click();
  await expect(page.locator('#vh-status')).toContainText('Applied preset');
  const actual = await health(page);
  expect(actual.routing.slice(0, 3)).toEqual([5, 7, 3]);
  expect(actual.routing[8]).toBe(9);
  expect(actual.images).toEqual([{media_id: 'fixture-image', player: 2}]);
  expect(actual.actions).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('videohub-image.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('locked and view-only presets display Ignore, reject edits, and retain failure feedback', async ({page}) => {
  const preset = await seed(page, [{output: 1, input: null}, {output: 2, input: 7}]);
  const base = `/api/videohub/presets/${preset.id}`;
  expect((await page.request.post(base + '/lock', {headers: headers(), data: {locked: true}})).ok()).toBe(true);
  await open(page, preset.id);
  await expect(page.locator('.vh-marker[data-output="1"]')).toBeDisabled();
  await expect(page.locator('.vh-marker[data-output="1"]')).toContainText('Keep Current Routing');
  await expect(page.locator('#vh-name')).toBeDisabled();
  expect((await page.request.put(base, {headers: headers(), data: {name: 'Changed', routes: []}})).status()).toBe(400);
  await page.request.get(marker + '/login?role=operator');
  await open(page, preset.id);
  await expect(page.locator('#vh-name')).toBeDisabled();
  await expect(page.locator('.vh-marker[data-output="1"]')).toBeDisabled();
  expect((await page.request.put(base, {headers: headers(), data: {name: 'Changed', routes: []}})).status()).toBe(403);
  await page.request.post(marker + '/failure');
  await page.locator('#vh-apply').click();
  await expect(page.locator('#vh-status')).toHaveClass(/alert-danger/);
  await expect(page.locator('#vh-status')).toContainText('did not confirm');
  expect((await health(page)).writes[0].routes).toEqual([[1, 6]]);
  await page.request.get(marker + '/login?role=denied');
  expect((await page.request.post(base + '/apply', {headers: headers()})).status()).toBe(403);
});
