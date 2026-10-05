const {test, expect} = require('@playwright/test');
const {collectPageErrors, installCommonReadMocks} = require('./helpers');

test.beforeEach(async ({page}) => {
  const response = await page.request.get('/__props_fixture__/health');
  expect((await response.json()).fixture).toBe('tdeck-props-ui');
  await page.request.post('/__props_fixture__/reset');
  await page.request.get('/__props_fixture__/login');
  await installCommonReadMocks(page);
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
  await expect(page.getByRole('link', {name: 'Configure Props', exact: true})).toBeVisible();
  await page.getByRole('link', {name: 'Configure Props', exact: true}).click();
  await page.getByLabel('Friendly name', {exact: true}).fill('Welcome');
  await expect(page.getByLabel('Friendly name', {exact: true})).toHaveValue('Welcome');
  await page.getByLabel('Search ProPresenter props', {exact: true}).fill('Original');
  await page.getByLabel('Existing ProPresenter prop', {exact: true}).selectOption('4cddf2c5-7355-4347-980c-b13f12cbfa85');
  await expect(page.getByLabel('Friendly name', {exact: true})).toHaveValue('Welcome');
  await page.getByRole('button', {name: 'Add mapping', exact: true}).click();
  await expect(page.locator('#props-library')).toContainText('Welcome');
  const saved = (await (await page.request.get('/api/props')).json()).library[0];
  expect(saved.id).not.toBe(saved.prop_uuid);
  expect(saved.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  await expect(page.getByRole('button', {name: 'New folder', exact: true})).toBeVisible();
  await page.getByRole('link', {name: 'Back to Props', exact: true}).click();
  await page.getByLabel('Preset', {exact: true}).selectOption({label: 'Welcome'});
  await page.getByRole('button', {name: 'Add preset', exact: true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await page.getByRole('button', {name: 'Add preset', exact: true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(2);
  await expect(page.locator('[data-props-slot]').nth(1)).toContainText('2. Welcome');
  await page.locator('[data-props-slot]').first().getByRole('button', {name: 'Remove', exact: true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await expect(page.locator('[data-props-slot]').first()).toContainText('1. Welcome');
  await page.reload();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(0);
  await page.getByRole('button', {name: 'Trigger', exact: true}).click();
  await expect(page.locator('#props-last-triggered')).toContainText('Welcome');
  await page.request.post('/__props_fixture__/failure');
  await page.getByRole('button', {name: 'Trigger', exact: true}).click();
  await expect(page.locator('#props-error')).toContainText('Do not automatically retry');
  await expect(page.locator('#props-last-triggered')).toContainText('Welcome');
  expect((await (await page.request.get('/__props_fixture__/health')).json()).calls).toBe(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('props.png'), fullPage: true});
  expect(errors).toEqual([]);
});

test('search is grouped and never blanks saved slots; folders and friendly-name updates persist', async ({page}) => {
  const id = '4cddf2c5-7355-4347-980c-b13f12cbfa85';
  const state = await (await page.request.get('/api/props')).json();
  const headers = {'Origin': new URL(test.info().project.use.baseURL || process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token':'props-csrf'};
  const library = [{id, name:'Welcome', prop_uuid:id, macro_uuid:null}];
  const save = await page.request.put('/api/props/library', {headers, data:{revision:state.revision, library, organization:{folders:[{id:'folder-test',name:'Service'}],items:[{id,folderId:'folder-test'}]}}});
  expect(save.ok()).toBe(true);
  await page.goto('/props');
  await page.getByLabel('Preset', {exact:true}).selectOption(id);
  await page.getByRole('button', {name:'Add preset',exact:true}).click();
  await expect(page.locator('[data-props-slot]')).toHaveCount(1);
  await page.getByLabel('Search presets', {exact:true}).fill('No results');
  // Changing a name on another browser must not turn the saved slot selector blank.
  const current = await (await page.request.get('/api/props')).json();
  library[0].name = 'Welcome again';
  expect((await page.request.put('/api/props/library', {headers,data:{revision:current.revision,library,organization:current.organization}})).ok()).toBe(true);
  await page.getByRole('button', {name:'Move preset 1 up',exact:true}).focus().catch(() => {});
  await page.locator('#page-title').click();
  await expect(page.locator('[data-props-slot]').first()).toContainText('Welcome again');
  await expect(page.getByLabel('Change preset 1', {exact:true})).toHaveValue(id);
  await page.getByLabel('Search presets', {exact:true}).fill('Service');
  await expect(page.locator('#props-selector optgroup')).toHaveAttribute('label','Service');
  await page.screenshot({path:test.info().outputPath('props-grouped.png'),fullPage:true});
});

test('shared folders use keyboard moves and collapse; macro mode saves without triggering', async ({page}) => {
  await page.goto('/props/configure');
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
  await expect(page.getByRole('button', {name: 'Add preset', exact: true})).toBeVisible();
  const denied = await page.request.put('/api/v1/props/library', {data: {}, headers: {'Origin': new URL(page.url()).origin, 'X-CSRF-Token':'props-csrf'}});
  expect(denied.status()).toBe(403);
  await page.request.get('/__props_fixture__/login?role=denied');
  expect((await page.request.get('/props')).status()).toBe(403);
});
