const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');

test.beforeEach(async ({page}) => {
  const response = await page.request.get('/__props_fixture__/health');
  expect((await response.json()).fixture).toBe('tdeck-props-ui');
  expect((await page.request.post('/__props_fixture__/reset')).ok()).toBe(true);
  await page.request.get('/__props_fixture__/login');
  await installCommonReadMocks(page);
});

async function seedLibrary(page) {
  const before = await (await page.request.get('/api/props')).json();
  const id = '4cddf2c5-7355-4347-980c-b13f12cbfa85';
  const headers = {'Origin':new URL(process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token':'props-csrf'};
  expect((await page.request.put('/api/props/library', {headers,data:{revision:before.revision,
    library:[{id,name:'Welcome',prop_uuid:id,macro_uuid:null}],
    organization:{folders:[{id:'service',name:'Service'}],items:[{id,folderId:'service'}]}}})).ok()).toBe(true);
  return id;
}

async function addPreset(page, id) {
  await page.getByRole('button', {name:'Add Preset',exact:true}).click();
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.getByLabel('Preset',{exact:true}).selectOption(id);
  await expect(page.getByRole('dialog',{name:'Add Preset',exact:true})).toBeHidden();
}

test('Add Preset is a Timers-style button opening a blank cancellable picker without writes', async ({page}) => {
  await seedLibrary(page);
  const before = await (await page.request.get('/api/props')).json();
  const writes = [];
  page.on('request', req => { if (req.url().includes('/api/props') && req.method() !== 'GET') writes.push(req.url()); });
  await page.goto('/props');
  const add = page.getByRole('button',{name:'Add Preset',exact:true});
  await expect(add).toHaveClass('btn btn-primary btn-sm timer-add-preset-btn');
  await expect(page.locator('input[type=search]')).toHaveCount(0);
  await expect(page.getByLabel('Preset',{exact:true})).toBeHidden();
  await add.click();
  const picker = page.getByRole('dialog',{name:'Add Preset',exact:true});
  await expect(picker).toBeVisible();
  // A modal must be opaque; the shared soft panel background is translucent.
  await expect(picker).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(page.getByLabel('Preset',{exact:true})).toBeFocused();
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await expect(page.locator('#props-selector optgroup')).toHaveAttribute('label','Service');
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(picker).toBeHidden();
  await expect(add).toBeFocused();
  await add.click();
  await expect(page.locator('input[type=search]')).toHaveCount(0);
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.keyboard.press('Escape');
  await expect(picker).toBeHidden();
  expect((await (await page.request.get('/api/props')).json()).revision).toBe(before.revision);
  expect(writes).toEqual([]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});

test('running-order rows match Timers controls and persist explicit repeated selections and moves', async ({page}) => {
  const id = await seedLibrary(page);
  await page.goto('/props');
  await addPreset(page, id); await addPreset(page, id);
  await expect(page.locator('.props-order-panel')).toHaveClass('tdeck-soft rounded-3 p-3 props-order-panel');
  await expect(page.locator('#props-order')).toHaveJSProperty('tagName','TBODY');
  const up = page.getByRole('button',{name:'Move preset 1 up',exact:true});
  const down = page.getByRole('button',{name:'Move preset 1 down',exact:true});
  await expect(up).toHaveClass('btn btn-sm btn-outline-secondary me-1 btn-icon-sm');
  await expect(up).toHaveText('▲'); await expect(up).toBeDisabled();
  await expect(down).toHaveText('▼'); await expect(down).toBeEnabled();
  await expect(up.locator('..')).toHaveClass('d-flex flex-column align-items-center gap-1');
  await expect(page.getByRole('button',{name:'Move preset 2 down',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Delete preset 1',exact:true})).toHaveClass('btn btn-sm btn-outline-danger');
  await expect(page.getByRole('button',{name:'Delete preset 1',exact:true})).toHaveText('Delete');
  // A distinct friendly name proves positional movement, not merely same-ID swapping.
  const before = await (await page.request.get('/api/props')).json();
  const second = 'edfad26c-6b88-4932-a996-497136f2cedf';
  const headers = {'Origin':new URL(process.env.TDECK_BASE_URL).origin,'X-CSRF-Token':'props-csrf'};
  const response = await page.request.put('/api/props/library',{headers,data:{revision:before.revision,
    library:before.library.map(({id,name,prop_uuid,macro_uuid}) => ({id,name,prop_uuid,macro_uuid})).concat([{id:second,name:'Closing',prop_uuid:second,macro_uuid:null}]),
    organization:{folders:before.organization.folders,items:before.organization.items.concat([{id:second,folderId:null}])}}});
  expect(response.ok(), await response.text()).toBe(true);
  await page.reload(); await addPreset(page, second);
  await page.getByRole('button',{name:'Move preset 3 up',exact:true}).click();
  await expect(page.locator('[data-props-slot]').nth(1)).toContainText('Closing');
  await page.getByRole('button',{name:'Move preset 1 down',exact:true}).click();
  await expect(page.locator('[data-props-slot]').first()).toContainText('Closing');
  await page.getByRole('button',{name:'Delete preset 2',exact:true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(2);
  await page.reload();
  await expect(page.locator('[data-props-slot]').first()).toContainText('1. Closing');
  await expect(page.locator('[data-props-slot]').nth(1)).toContainText('2. Welcome');
  expect((await (await page.request.get('/api/props')).json()).order).toEqual([second,id]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:test.info().outputPath('props-order.png'),fullPage:true});
});

test('empty library disables Add Preset with permission-aware setup guidance', async ({page}) => {
  await page.goto('/props');
  await expect(page.getByRole('button',{name:'Add Preset',exact:true})).toBeDisabled();
  await expect(page.locator('#props-empty-library')).toContainText('No library mappings yet');
  await expect(page.locator('#props-empty-library').getByRole('link',{name:'Configure Props',exact:true})).toBeVisible();
  await page.request.get('/__props_fixture__/login?role=operator');
  await page.reload();
  await expect(page.getByRole('button',{name:'Add Preset',exact:true})).toBeDisabled();
  await expect(page.locator('#props-empty-library')).toContainText('Ask someone with Configure Props access');
  await expect(page.getByRole('link',{name:'Configure Props',exact:true})).toHaveCount(0);
});

test('conflicting Add selection keeps a readable picker and never retries or overwrites newer order', async ({page}) => {
  const id = await seedLibrary(page);
  await page.goto('/props');
  await page.getByRole('button',{name:'Add Preset',exact:true}).click();
  const before = await (await page.request.get('/api/props')).json();
  const headers = {'Origin':new URL(process.env.TDECK_BASE_URL).origin,'X-CSRF-Token':'props-csrf'};
  expect((await page.request.put('/api/props/order',{headers,data:{revision:before.revision,order:[id,id]}})).ok()).toBe(true);
  let writes = 0;
  page.on('request',req => { if (req.url().endsWith('/api/props/order') && req.method() === 'PUT') writes++; });
  await page.getByLabel('Preset',{exact:true}).selectOption(id);
  const picker = page.getByRole('dialog',{name:'Add Preset',exact:true});
  await expect(picker).toBeVisible();
  await expect(picker.getByRole('alert')).toContainText('Props changed');
  await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
  await expect(page.locator('[data-props-slot]')).toHaveCount(2);
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.screenshot({path:test.info().outputPath('props-picker-conflict.png'),fullPage:true});
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await page.reload(); await expect(page.locator('[data-props-slot]')).toHaveCount(2);
  expect(writes).toBe(1);
  expect((await (await page.request.get('/api/props')).json()).order).toEqual([id,id]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});

test('picker traps keyboard focus and unavailable presets cannot trigger', async ({page}) => {
  const errors = collectPageErrors(page);
  const id = await seedLibrary(page);
  const before = await (await page.request.get('/api/props')).json();
  const headers = {'Origin':new URL(process.env.TDECK_BASE_URL).origin,'X-CSRF-Token':'props-csrf'};
  expect((await page.request.put('/api/props/settings',{headers,data:{revision:before.revision,previous_use_macros:false,use_macros:true}})).ok()).toBe(true);
  await page.goto('/props');
  await page.getByRole('button',{name:'Add Preset',exact:true}).click();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button',{name:'Cancel',exact:true})).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Preset',{exact:true})).toBeFocused();
  await expect(page.locator('#props-selector optgroup')).toHaveCount(1);
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.getByLabel('Preset',{exact:true}).selectOption(id);
  await expect(page.getByRole('dialog',{name:'Add Preset',exact:true})).toBeHidden();
  await expect(page.getByRole('button',{name:'Trigger preset 1',exact:true})).toBeDisabled();
  await expect(page.locator('[data-props-slot]')).toContainText('Selected target missing');
  await page.getByRole('button',{name:'Delete preset 1',exact:true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(0);
  await page.reload(); await expect(page.locator('#props-order')).toContainText('No presets');
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(errors).toEqual([]);
});

test('pending Add save contains keyboard focus until delayed success returns focus to Add', async ({page}) => {
  const errors = collectPageErrors(page);
  const id = await seedLibrary(page);
  let release, started, writes = 0;
  const blocked = new Promise(resolve => { started = resolve; });
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/props/order', async route => {
    if (route.request().method() === 'PUT') { writes++; started(); await pending; }
    await route.continue();
  });
  await page.goto('/props');
  const add = page.getByRole('button',{name:'Add Preset',exact:true});
  const picker = page.getByRole('dialog',{name:'Add Preset',exact:true});
  await add.click();
  await page.getByLabel('Preset',{exact:true}).focus();
  await page.getByLabel('Preset',{exact:true}).selectOption(id);
  await blocked;
  try {
    await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','true');
    await expect(picker).toHaveAttribute('aria-modal','true');
    expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
    for (const key of ['Tab','Shift+Tab','Escape','Tab','Shift+Tab']) {
      await page.keyboard.press(key);
      await expect(picker).toBeVisible();
      expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
    }
    await expect(page.getByRole('button',{name:'Cancel',exact:true})).toBeDisabled();
    expect((await (await page.request.get('/api/props')).json()).order).toEqual([]);
    expect(writes).toBe(1);
  } finally { release(); }
  await expect(picker).toBeHidden();
  await expect(add).toBeFocused();
  expect((await (await page.request.get('/api/props')).json()).order).toEqual([id]);
  expect(writes).toBe(1);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(errors).toEqual([]);
});

for (const fallback of [false, true]) {
  test('pending Add save blocks background navigation through delayed failure' + (fallback ? ' without native inert' : ''), async ({page}) => {
    const errors = collectPageErrors(page);
    if (fallback) await page.addInitScript(() => { delete HTMLElement.prototype.inert; });
    const id = await seedLibrary(page);
    let release, started, writes = 0;
    const blocked = new Promise(resolve => { started = resolve; });
    const pending = new Promise(resolve => { release = resolve; });
    await page.route('**/api/props/order', async route => {
      if (route.request().method() === 'PUT') {
        writes++; started(); await pending;
        await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'Save unavailable'})});
      } else await route.continue();
    });
    await page.goto('/props');
    if (fallback) expect(await page.evaluate(() => 'inert' in HTMLElement.prototype)).toBe(false);
    const url = page.url();
    const add = page.getByRole('button',{name:'Add Preset',exact:true});
    const picker = page.getByRole('dialog',{name:'Add Preset',exact:true});
    const outside = page.locator('a[href="/props/configure"]').first();
    await add.click();
    await page.getByLabel('Preset',{exact:true}).focus();
    await page.getByLabel('Preset',{exact:true}).selectOption(id);
    await blocked;
    try {
      await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','true');
      expect(await outside.evaluate(el => !!el.closest('[aria-hidden="true"]'))).toBe(true);
      expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
      // Force the fallback path to handle escaped/programmatic focus and clicks too.
      await outside.evaluate(el => el.focus());
      expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
      expect(await outside.evaluate(el => !el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})))).toBe(true);
      await page.mouse.click(5, 5);
      for (const key of ['Tab','Shift+Tab','Escape','Tab','Shift+Tab']) {
        await page.keyboard.press(key);
        await expect(picker).toBeVisible();
        expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
      }
      await expect(page).toHaveURL(url);
      expect((await (await page.request.get('/api/props')).json()).order).toEqual([]);
      expect(writes).toBe(1);
    } finally { release(); }
    await expect(picker.getByRole('alert')).toContainText('Save unavailable');
    await expect(picker.getByRole('alert')).toContainText('Nothing was retried');
    await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
    await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
    await expect(page.getByLabel('Preset',{exact:true})).toBeEnabled();
    await expect(page.getByLabel('Preset',{exact:true})).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button',{name:'Cancel',exact:true})).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByLabel('Preset',{exact:true})).toBeFocused();
    expect(await outside.evaluate(el => !el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})))).toBe(true);
    await page.keyboard.press('Escape');
    await expect(picker).toBeHidden();
    await expect(add).toBeFocused();
    expect(await outside.evaluate(el => !!el.closest('[aria-hidden="true"]'))).toBe(false);
    await outside.focus(); await expect(outside).toBeFocused();
    expect(writes).toBe(1);
    expect((await (await page.request.get('/api/props')).json()).order).toEqual([]);
    expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
    expect(errors).toEqual([]);
  });
}

test('rejected Add save keeps selection deliberate and cancels without replay', async ({page}) => {
  const id = await seedLibrary(page);
  let writes = 0;
  await page.route('**/api/props/order',async route => {
    if (route.request().method() === 'PUT') {
      writes++; await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'Save unavailable'})});
    } else await route.continue();
  });
  await page.goto('/props');
  await page.getByRole('button',{name:'Add Preset',exact:true}).click();
  await page.getByLabel('Preset',{exact:true}).selectOption(id);
  const picker = page.getByRole('dialog',{name:'Add Preset',exact:true});
  await expect(picker.getByRole('alert')).toContainText('Save unavailable');
  await expect(page.getByLabel('Preset',{exact:true})).toHaveValue('');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(picker).toBeHidden();
  expect(writes).toBe(1);
  expect((await (await page.request.get('/api/props')).json()).order).toEqual([]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});

test('delayed API client still authenticates initial catalog refresh and renders both pages', async ({page}) => {
  const errors = collectPageErrors(page);
  const refreshes = [];
  page.on('request', request => {
    if (request.url().includes('/api/props/catalog/refresh')) refreshes.push(request.headers()['x-csrf-token']);
  });
  await page.route('**/static/api_client.js*', async route => {
    await new Promise(resolve => setTimeout(resolve, 700));
    await route.continue();
  });
  await page.goto('/props/configure');
  await expect(page.locator('#props-status')).toContainText('Catalog ready');
  await expect(page.getByRole('button', {name:'New folder', exact:true})).toBeVisible();
  expect(refreshes).toEqual(['props-csrf']);
  await expect(page.locator('#props-error')).toBeHidden();
  await page.goto('/props');
  await expect(page.locator('#props-order')).toContainText('No presets');
  expect(errors).toEqual([]);
});

test('initial refresh failure retains readable state and recovers without replay', async ({page}) => {
  const errors = collectPageErrors(page);
  let calls = 0;
  await page.route('**/api/props/catalog/refresh', async route => {
    calls++;
    if (calls === 1) await route.fulfill({status:502, contentType:'application/json', body:JSON.stringify({ok:false,error:'Catalog refresh rejected'})});
    else await route.continue();
  });
  await page.goto('/props/configure');
  await expect(page.locator('#props-error')).toContainText('Catalog refresh rejected');
  await expect(page.getByRole('button', {name:'New folder',exact:true})).toBeVisible();
  await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
  // Safe GET polling continues even when the startup refresh failed.
  const poll = page.waitForRequest(request => request.url().endsWith('/api/props') && request.method() === 'GET');
  await page.locator('#page-title').click();
  await poll;
  expect(calls).toBe(1);
  await page.getByRole('button', {name:'Refresh catalog',exact:true}).click();
  await expect(page.locator('#props-error')).toBeHidden();
  await expect(page.locator('#props-status')).toContainText('Catalog ready');
  expect(calls).toBe(2);
  expect(errors).toEqual([]);
});

test('conflicting folder save rolls back optimistic tree then redraws current server state', async ({page}) => {
  const errors = collectPageErrors(page);
  const headers = {'Origin':new URL(test.info().project.use.baseURL || process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token':'props-csrf'};
  const before = await (await page.request.get('/api/props')).json();
  expect((await page.request.put('/api/props/library',{headers,data:{revision:before.revision,library:[],organization:{folders:[{id:'service',name:'Service'}],items:[]}}})).ok()).toBe(true);
  const other = await page.context().newPage();
  await installCommonReadMocks(other);
  await page.goto('/props/configure'); await other.goto('/props/configure');
  await expect(page.getByRole('button',{name:'Expand or collapse Service',exact:true})).toBeVisible();
  await expect(other.getByRole('button',{name:'Expand or collapse Service',exact:true})).toBeVisible();
  let release, started;
  const blocked = new Promise(resolve => {started = resolve;});
  const pending = new Promise(resolve => {release = resolve;});
  await page.route('**/api/props/library', async route => { started(); await pending; await route.continue(); });
  page.once('dialog', dialog => dialog.accept('Uncommitted folder'));
  await page.getByRole('button',{name:'New folder',exact:true}).click();
  await blocked;
  await expect(page.getByRole('button',{name:'Expand or collapse Uncommitted folder',exact:true})).toBeVisible();
  other.once('dialog', dialog => dialog.accept('Current server folder'));
  await other.getByRole('button',{name:'Rename folder',exact:true}).click();
  await expect(other.locator('#props-library')).toHaveAttribute('aria-busy','false');
  expect((await (await page.request.get('/api/props')).json()).organization.folders[0].name).toBe('Current server folder');
  release();
  await expect(page.locator('#props-error')).toContainText('Props changed');
  await expect(page.locator('#props-library')).toHaveAttribute('aria-busy','false');
  await expect(page.getByRole('button',{name:'Expand or collapse Uncommitted folder',exact:true})).toHaveCount(0);
  // No reload/blur is needed: the deferred redraw must win over widget rollback.
  await expect(page.getByRole('button',{name:'Expand or collapse Current server folder',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Expand or collapse Service',exact:true})).toHaveCount(0);
  await page.screenshot({path:test.info().outputPath('props-conflict-recovered.png'),fullPage:true});
  expect(errors).toEqual([]);
  await other.close();
});

test('configure mappings then autosave repeated presets, reorder, trigger and retain success on failure', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/props');
  await expect(page.getByRole('link', {name: 'Configure Props', exact: true}).first()).toBeVisible();
  await page.getByRole('link', {name: 'Configure Props', exact: true}).first().click();
  await page.getByRole('button',{name:'Add Prop',exact:true}).click();
  await page.getByLabel('Friendly name', {exact: true}).fill('Welcome');
  await expect(page.getByLabel('Friendly name', {exact: true})).toHaveValue('Welcome');
  await page.getByLabel('Existing ProPresenter prop', {exact: true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
  await expect(page.getByLabel('Friendly name', {exact: true})).toHaveValue('Welcome');
  await page.getByRole('button', {name: 'Add mapping', exact: true}).click();
  await expect(page.locator('#props-library')).toContainText('Welcome');
  const saved = (await (await page.request.get('/api/props')).json()).library[0];
  expect(saved.id).not.toBe(saved.prop_uuid);
  expect(saved.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  await expect(page.getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  await page.getByRole('link', {name: 'Back to Props', exact: true}).click();
  await addPreset(page, saved.id);
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await addPreset(page, saved.id);
  await expect(page.locator('[data-props-slot]')).toHaveCount(2);
  await expect(page.locator('[data-props-slot]').nth(1)).toContainText('2. Welcome');
  await page.locator('[data-props-slot]').first().getByRole('button', {name: 'Delete preset 1', exact: true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await expect(page.locator('[data-props-slot]').first()).toContainText('1. Welcome');
  await page.reload();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  await page.getByRole('button', {name: 'Trigger preset 1', exact: true}).click();
  await expect(page.locator('#props-last-triggered')).toContainText('Welcome');
  await page.request.post('/__props_fixture__/failure');
  await page.getByRole('button', {name: 'Trigger preset 1', exact: true}).click();
  await expect(page.locator('#props-error')).toContainText('Do not automatically retry');
  await expect(page.locator('#props-last-triggered')).toContainText('Welcome');
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('props.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('native dropdown is grouped and never blanks saved slots; folders and friendly-name updates persist', async ({page}) => {
  const id = '4cddf2c5-7355-4347-980c-b13f12cbfa85';
  const state = await (await page.request.get('/api/props')).json();
  const headers = {'Origin': new URL(test.info().project.use.baseURL || process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token':'props-csrf'};
  const library = [{id, name:'Welcome', prop_uuid:id, macro_uuid:null}];
  const save = await page.request.put('/api/props/library', {headers, data:{revision:state.revision, library, organization:{folders:[{id:'folder-test',name:'Service'}],items:[{id,folderId:'folder-test'}]}}});
  expect(save.ok()).toBe(true);
  await page.goto('/props');
  await addPreset(page, id);
  await page.getByRole('button', {name:'Add Preset',exact:true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await expect(page.locator('input[type=search]')).toHaveCount(0);
  await expect(page.getByLabel('Preset', {exact:true})).toHaveValue('');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  // Changing a name on another browser must not turn the saved slot selector blank.
  const current = await (await page.request.get('/api/props')).json();
  library[0].name = 'Welcome again';
  expect((await page.request.put('/api/props/library', {headers,data:{revision:current.revision,library,organization:current.organization}})).ok()).toBe(true);
  await page.getByRole('button', {name:'Move preset 1 up',exact:true}).focus().catch(() => {});
  await page.locator('#page-title').click();
  await expect(page.locator('[data-props-slot]').first()).toContainText('Welcome again');
  await expect(page.getByLabel('Change preset 1', {exact:true})).toHaveValue(id);
  await page.getByRole('button',{name:'Add Preset',exact:true}).click();
  await expect(page.locator('#props-selector optgroup')).toHaveAttribute('label','Service');
  await expect(page.getByLabel('Preset', {exact:true})).toHaveValue('');
  await expect(page.locator('#props-selector optgroup')).toHaveAttribute('label','Service');
  await page.screenshot({path:test.info().outputPath('props-grouped.png'),fullPage:true});
});

test('shared folders use keyboard moves and collapse; macro mode saves without triggering', async ({page}) => {
  await page.goto('/props/configure');
  await page.getByRole('button',{name:'Add Prop',exact:true}).click();
  await page.getByLabel('Friendly name', {exact:true}).fill('Welcome');
  await page.getByLabel('Existing ProPresenter prop', {exact:true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
  await page.getByRole('button',{name:'Add mapping',exact:true}).click();
  await expect(page.getByRole('button',{name:'Move Welcome',exact:true})).toBeVisible();
  page.once('dialog', dialog => dialog.accept('Service'));
  await page.getByRole('button',{name:'New folder',exact:true}).click();
  await expect(page.getByRole('button',{name:'Expand or collapse Service',exact:true})).toBeVisible();
  const handle = page.getByRole('button',{name:'Move Welcome',exact:true});
  await expect(page.locator('#props-library')).toHaveAttribute('aria-busy','false');
  await expect(handle).toBeEnabled();
  await handle.press('Space'); await expect(handle).toHaveAttribute('aria-pressed','true');
  await handle.press('ArrowDown'); await handle.press('Enter');
  await expect.poll(async () => (await (await page.request.get('/api/props')).json()).organization.items[0].folderId).toBeTruthy();
  await page.getByRole('button',{name:'Expand or collapse Service',exact:true}).click();
  await expect(page.getByLabel('Friendly name for Welcome',{exact:true})).toBeHidden();
  await page.getByRole('button',{name:'Expand or collapse Service',exact:true}).click();
  await page.getByLabel('Use Macros for Props',{exact:true}).check();
  await expect.poll(async () => (await (await page.request.get('/api/props')).json()).use_macros).toBe(true);
  await page.reload();
  await expect(page.getByLabel('Use Macros for Props',{exact:true})).toBeChecked();
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  await page.screenshot({path:test.info().outputPath('props-configure.png'),fullPage:true});
});

test('external UUID spelling is retained without showing an available target as missing', async ({page}) => {
  const id = '4cddf2c5-7355-4347-980c-b13f12cbfa85';
  const state = await (await page.request.get('/api/props')).json();
  const headers = {'Origin':new URL(test.info().project.use.baseURL || process.env.TDECK_BASE_URL).origin,'X-CSRF-Token':'props-csrf'};
  const response = await page.request.put('/api/props/library',{headers,data:{revision:state.revision,library:[{id,name:'Welcome',prop_uuid:id.toUpperCase(),macro_uuid:null}],organization:{folders:[],items:[{id}]}}});
  expect(response.ok()).toBe(true);
  await page.goto('/props/configure');
  await expect(page.getByLabel('ProPresenter prop for Welcome',{exact:true}).locator('option:checked')).toHaveText('Original');
  expect((await (await page.request.get('/api/props')).json()).library[0].prop_uuid).toBe(id.toUpperCase());
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});

test('operator may edit order but cannot manage mappings, denied user has no Props', async ({page}) => {
  await page.request.get('/__props_fixture__/login?role=operator');
  await page.goto('/props');
  await expect(page.getByRole('link', {name: 'Configure Props', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Add Preset', exact: true})).toBeVisible();
  const denied = await page.request.put('/api/v1/props/library', {data: {}, headers: {'Origin': new URL(page.url()).origin, 'X-CSRF-Token':'props-csrf'}});
  expect(denied.status()).toBe(403);
  await page.request.get('/__props_fixture__/login?role=denied');
  expect((await page.request.get('/props')).status()).toBe(403);
});


test('Configure Props uses compact Timers panel and blank cancellable Add Prop dialog with native dropdowns', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/props/configure');
  await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
  const before = await (await page.request.get('/api/props')).json();
  const writes = [];
  page.on('request', req => { if (req.url().includes('/api/props') && req.method() !== 'GET') writes.push(req.url()); });
  await expect(page.locator('input[type=search],datalist')).toHaveCount(0);
  await expect(page.locator('.props-library-panel')).toHaveClass('tdeck-soft rounded-3 p-3 props-library-panel');
  const add = page.getByRole('button',{name:'Add Prop',exact:true});
  await expect(add).toHaveClass('btn btn-primary btn-sm timer-add-preset-btn');
  await expect(page.getByLabel('Friendly name',{exact:true})).toBeHidden();
  const picker = page.getByRole('dialog',{name:'Add Prop',exact:true});
  for (const cancel of ['Cancel','Escape']) {
    await add.click();
    await expect(picker).toBeVisible();
    await expect(picker).toHaveCSS('background-color','rgb(255, 255, 255)');
    await expect(page.getByLabel('Friendly name',{exact:true})).toBeFocused();
    await expect(page.getByLabel('Friendly name',{exact:true})).toHaveValue('');
    await expect(page.getByLabel('Existing ProPresenter prop',{exact:true})).toHaveValue('');
    await expect(page.getByLabel('Optional macro',{exact:true})).toHaveValue('');
    if (cancel === 'Cancel') await page.screenshot({path:test.info().outputPath('props-add-blank.png'),fullPage:true});
    await page.getByLabel('Friendly name',{exact:true}).fill('Unsubmitted');
    await page.getByLabel('Existing ProPresenter prop',{exact:true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
    if (cancel === 'Cancel') await page.getByRole('button',{name:'Cancel',exact:true}).click();
    else await page.keyboard.press('Escape');
    await expect(picker).toBeHidden(); await expect(add).toBeFocused();
  }
  expect(writes).toEqual([]);
  expect((await (await page.request.get('/api/props')).json()).revision).toBe(before.revision);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(errors).toEqual([]);
});


test('compact library rows label editable mappings and outline blocked used Delete', async ({page}) => {
  const id = await seedLibrary(page);
  await page.goto('/props'); await addPreset(page,id);
  await page.goto('/props/configure');
  const row = page.locator('.props-row').first();
  await expect(row.getByText('Friendly name',{exact:true})).toBeVisible();
  await expect(row.getByText('ProPresenter prop',{exact:true})).toBeVisible();
  await expect(row.getByText('Optional macro',{exact:true})).toBeVisible();
  const remove = page.getByRole('button',{name:'Delete mapping Welcome',exact:true});
  await expect(remove).toHaveText('Delete');
  await expect(remove).toHaveClass('btn btn-sm btn-outline-danger');
  await expect(remove).toBeDisabled();
  await expect(row).toContainText('Used in running order');
  await page.getByLabel('Friendly name for Welcome',{exact:true}).fill('Welcome edited');
  await page.getByLabel('Friendly name for Welcome',{exact:true}).press('Tab');
  await expect(page.getByLabel('Friendly name for Welcome edited',{exact:true})).toHaveValue('Welcome edited');
  await page.getByLabel('ProPresenter prop for Welcome edited',{exact:true}).selectOption('edfad26c-6b88-4932-a996-497136f2cedf');
  await expect.poll(async () => (await (await page.request.get('/api/props')).json()).library[0].prop_uuid).toBe('edfad26c-6b88-4932-a996-497136f2cedf');
  await page.reload();
  await expect(page.getByLabel('ProPresenter prop for Welcome edited',{exact:true})).toHaveValue('edfad26c-6b88-4932-a996-497136f2cedf');
  expect((await (await page.request.get('/api/props')).json()).order).toEqual([id]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:test.info().outputPath('props-compact-library.png'),fullPage:true});
});


for (const outcome of ['success','failure','failure-fallback']) {
  test('Add Prop pending save isolates focus and recovers without replay: ' + outcome, async ({page}) => {
    const errors = collectPageErrors(page);
    if (outcome === 'failure-fallback') await page.addInitScript(() => { delete HTMLElement.prototype.inert; });
    await page.goto('/props/configure');
    await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
    let started, release, writes = 0;
    const blocked = new Promise(resolve => {started=resolve;});
    const pending = new Promise(resolve => {release=resolve;});
    await page.route('**/api/props/library', async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      writes++; started(); await pending;
      if (outcome === 'success') await route.continue();
      else await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'Save unavailable'})});
    });
    const add = page.getByRole('button',{name:'Add Prop',exact:true});
    const picker = page.getByRole('dialog',{name:'Add Prop',exact:true});
    const outside = page.locator('a[href="/props"]').first();
    await add.click();
    await page.getByLabel('Friendly name',{exact:true}).fill('Welcome');
    await page.getByLabel('Existing ProPresenter prop',{exact:true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
    await page.getByRole('button',{name:'Add mapping',exact:true}).click();
    await blocked;
    try {
      await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','true');
      await outside.evaluate(el => el.focus());
      expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
      expect(await outside.evaluate(el => !el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})))).toBe(true);
      for (const key of ['Tab','Shift+Tab','Escape']) {
        await page.keyboard.press(key); await expect(picker).toBeVisible();
        expect(await picker.evaluate(el => el.contains(document.activeElement))).toBe(true);
      }
      await expect(page.getByRole('button',{name:'Cancel',exact:true})).toBeDisabled();
      expect((await (await page.request.get('/api/props')).json()).library).toEqual([]);
    } finally {release();}
    if (outcome === 'success') {
      await expect(picker).toBeHidden(); await expect(add).toBeFocused();
      expect((await (await page.request.get('/api/props')).json()).library[0].name).toBe('Welcome');
    } else {
      await expect(picker.getByRole('alert')).toContainText('Nothing was retried');
      await expect(page.getByLabel('Existing ProPresenter prop',{exact:true})).toHaveValue('');
      await expect(page.getByLabel('Optional macro',{exact:true})).toHaveValue('');
      await expect(page.getByLabel('Existing ProPresenter prop',{exact:true})).toBeFocused();
      await expect(page.getByRole('button',{name:'Add mapping',exact:true})).toBeEnabled();
      await page.keyboard.press('Escape'); await expect(picker).toBeHidden(); await expect(add).toBeFocused();
      expect((await (await page.request.get('/api/props')).json()).library).toEqual([]);
    }
    expect(await outside.evaluate(el => !!el.closest('[aria-hidden="true"],[inert]'))).toBe(false);
    expect(writes).toBe(1);
    expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
    expect(errors).toEqual([]);
  });
}

test('duplicate prop submission is rejected without rewriting existing mapping or retrying', async ({page}) => {
  const id = await seedLibrary(page);
  await page.goto('/props/configure');
  await page.getByRole('button',{name:'Add Prop',exact:true}).click();
  await page.getByLabel('Friendly name',{exact:true}).fill('Duplicate');
  await page.getByLabel('Existing ProPresenter prop',{exact:true}).selectOption(id);
  let writes = 0;
  page.on('request', req => {if(req.url().endsWith('/api/props/library') && req.method()==='PUT') writes++;});
  await page.getByRole('button',{name:'Add mapping',exact:true}).click();
  const picker = page.getByRole('dialog',{name:'Add Prop',exact:true});
  await expect(picker.getByRole('alert')).toContainText('Nothing was retried');
  await expect(page.getByLabel('Existing ProPresenter prop',{exact:true})).toHaveValue('');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  const current = (await (await page.request.get('/api/props')).json()).library;
  expect(current).toHaveLength(1); expect(current[0].id).toBe(id); expect(current[0].name).toBe('Welcome');
  expect(writes).toBe(1);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});


test('explicit Add Prop saves macro reference and supports native macro editing and unused deletion', async ({page}) => {
  const errors = collectPageErrors(page);
  await page.goto('/props/configure');
  await page.getByRole('button',{name:'Add Prop',exact:true}).click();
  await page.getByLabel('Friendly name',{exact:true}).fill('Welcome');
  await page.getByLabel('Existing ProPresenter prop',{exact:true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
  await page.getByLabel('Optional macro',{exact:true}).selectOption('9a87ac44-52d3-483f-a26c-f0b97b8e6281');
  expect((await (await page.request.get('/api/props')).json()).library).toEqual([]);
  await page.getByRole('button',{name:'Add mapping',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'Add Prop',exact:true})).toBeHidden();
  await expect(page.getByLabel('Macro for Welcome',{exact:true})).toHaveValue('9a87ac44-52d3-483f-a26c-f0b97b8e6281');
  const saved = (await (await page.request.get('/api/props')).json()).library[0];
  expect(saved.macro_uuid).toBe('9a87ac44-52d3-483f-a26c-f0b97b8e6281');
  await page.getByLabel('Macro for Welcome',{exact:true}).selectOption('20bc2bfa-df38-448e-83bb-f9eaa1b0a66f');
  await expect.poll(async () => (await (await page.request.get('/api/props')).json()).library[0].macro_uuid).toBe('20bc2bfa-df38-448e-83bb-f9eaa1b0a66f');
  await page.getByLabel('Macro for Welcome',{exact:true}).selectOption('');
  await expect.poll(async () => (await (await page.request.get('/api/props')).json()).library[0].macro_uuid).toBe(null);
  await page.getByRole('button',{name:'Delete mapping Welcome',exact:true}).click();
  await expect(page.locator('.props-row')).toHaveCount(0);
  expect((await (await page.request.get('/api/props')).json()).library).toEqual([]);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  expect(errors).toEqual([]);
});

test('failed mapping edit restores server values without mutation replay', async ({page}) => {
  await seedLibrary(page); await page.goto('/props/configure');
  await expect(page.locator('#props-page')).toHaveAttribute('aria-busy','false');
  let writes=0;
  await page.route('**/api/props/library',async route => {
    if(route.request().method() === 'PUT') {
      writes++; await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'Edit unavailable'})});
    } else await route.continue();
  });
  await page.getByLabel('Friendly name for Welcome',{exact:true}).fill('Not saved');
  await expect(page.getByLabel('Friendly name for Welcome',{exact:true})).toHaveValue('Not saved');
  await page.locator('#page-title').click();
  await expect(page.locator('#props-error')).toContainText('Edit unavailable');
  await expect(page.getByLabel('Friendly name for Welcome',{exact:true})).toHaveValue('Welcome');
  expect((await (await page.request.get('/api/props')).json()).library[0].name).toBe('Welcome');
  expect(writes).toBe(1);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
});
