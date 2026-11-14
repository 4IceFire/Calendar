const {test, expect} = require('@playwright/test');
const {installCommonReadMocks, collectPageErrors} = require('./helpers');
const prefix = '/__dlive_fixture__/';
test.setTimeout(60000);
test.beforeEach(async ({page}) => {
  const health = await (await page.request.get(prefix + 'health')).json();
  expect(health.fixture).toBe('tdeck-dlive'); expect(health.isolated).toBe(true);
  expect(health.checkout_db_unchanged).toBe(true);
  expect((await page.request.post(prefix + 'reset')).ok()).toBe(true);
  expect((await page.request.get(prefix + 'login?admin=1')).ok()).toBe(true);
  await installCommonReadMocks(page);
});
async function prompt(page, action, answer) {
  page.once('dialog', d => d.accept(answer)); await action();
}
async function save(page) {
  const response = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method() === 'POST');
  await page.locator('#digico-setup-save').click(); expect((await response).ok()).toBe(true);
  await expect(page.locator('#digico-setup-message')).toContainText('saved');
}
async function seedDigico(page) {
  const health = await page.request.get(prefix+'health');
  expect((await page.request.post('/api/digico/setup', {
    headers:{'X-CSRF-Token':'dlive-csrf', Origin:new URL(health.url()).origin},
    data:{personal_mix_provider:'digico', digico_enabled:false,
      digico_auxes:[{channel:1,label:'SD IEM',icon:'headset',colour:'#aa4422'},{channel:2,label:'Spare',enabled:false}],
      digico_channels:[1,2,3,4].map(n=>({channel:n,label:'SD '+n,icon:'drums',group:n===1?'Vocals':n===3?'Band':''}))},
  })).ok()).toBe(true);
}
async function showChannels(page, root) {
  await expect(root.locator('.catalog-handle')).toHaveCount(4);
  await page.evaluate(() => document.fonts.ready);
  await page.locator('[data-bs-target="#digico-tab-channels"]').click();
  await expect(page.locator('#digico-tab-channels')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#digico-tab-channels')).toHaveCSS('opacity','1');
  await expect(root).toBeVisible();
  await expect(root).toHaveAttribute('aria-busy','false');
}
async function openChannels(page, provider) {
  await page.goto('/config/digico');
  const root = page.locator(provider==='dlive'?'#dlive-input-mappings':'#digico-channel-table');
  await showChannels(page, root);
  return root;
}
async function mouseMove(page, handle, destination, cancel=false) {
  await handle.evaluate(el=>el.scrollIntoView({block:'center',behavior:'instant'}));
  const a=await handle.boundingBox();
  await page.mouse.move(a.x+a.width/2,a.y+a.height/2); await page.mouse.down();
  await page.mouse.move(a.x+a.width/2+10,a.y+a.height/2);
  await destination.evaluate(el=>el.scrollIntoView({block:'center',behavior:'instant'}));
  const b=await destination.boundingBox();
  await page.mouse.move(b.x+b.width/2,b.y+b.height/2,{steps:10});
  await expect(page.locator('.catalog-drag-ghost')).toBeVisible();
  await expect(page.locator('.catalog-drop-target')).toHaveCount(1);
  if(cancel) await page.keyboard.press('Escape');
  await page.mouse.up();
  await expect(page.locator('.catalog-drag-ghost')).toHaveCount(0);
}
test('saving the active provider preserves inactive provider configuration exactly', async ({page}) => {
  const before=(await (await page.request.get(prefix+'health')).json()).config;
  await openChannels(page,'dlive'); await save(page);
  const after=(await (await page.request.get(prefix+'health')).json()).config;
  for(const key of Object.keys(before).filter(k=>k.startsWith('digico_'))) expect(after[key],key).toEqual(before[key]);
  expect(after.digico_channel_organization).toBeUndefined();
});
for(const provider of ['dlive','digico']) {
  test(provider+' true groups are drafts until save, ordered stable tabs after reload', async ({page},info)=>{
    const errors=collectPageErrors(page);
    if(provider==='digico') await seedDigico(page);
    const root=await openChannels(page,provider);
    await expect(root.getByText('Section heading',{exact:true})).toHaveCount(0);
    const groups=root.locator('[data-catalog-folder]:not([data-catalog-folder=""])');
    await expect(groups).toHaveCount(2);
    await prompt(page,()=>groups.nth(1).getByRole('button',{name:'Rename group',exact:true}).click(),'Instruments');
    await expect(root).toHaveAttribute('aria-busy','false');
    await groups.nth(1).getByRole('button',{name:'Move group up',exact:true}).click();
    await expect(root).toHaveAttribute('aria-busy','false');
    await expect(root.getByRole('status')).toContainText('Unsaved');
    expect((await (await page.request.get(prefix+'health')).json()).config[provider+'_channel_organization']).toBeUndefined();
    await save(page); await page.reload();
    await showChannels(page, root);
    await expect(groups.nth(0)).toContainText('Instruments');
    const health=await (await page.request.get(prefix+'health')).json();
    expect(health.config[provider+'_channel_organization'].folders.map(f=>f.name)).toEqual(['Instruments','Vocals']);
    if(provider==='dlive') expect(health.config.dlive_channels.map(c=>c.channel)).toEqual([3,4,1,2]);
    else expect(health.config.digico_channels.map(c=>c.label)).toEqual(['SD 1','SD 2','SD 3','SD 4']);
    await page.screenshot({path:info.outputPath(provider+'-channel-groups.png'),fullPage:true});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    await page.goto('/personal-mixes'); await page.getByRole('button',{name:provider==='dlive'?'IEM':'SD IEM',exact:true}).click();
    await expect(page.getByRole('tab')).toHaveText(['Instruments2 inputs','Vocals2 inputs']);
    if(provider==='digico') {
      await expect(page.locator('.digico-send-toggle')).toHaveCount(4);
      await expect(page.locator('.digico-input-meter')).toHaveCount(4);
    }
    expect(errors).toEqual([]);
  });
  test(provider+' native drag cancellation, stable row edits, empty/delete and failed save recovery',async({page},info)=>{
    const errors=collectPageErrors(page);
    await page.emulateMedia({reducedMotion:'reduce'});
    if(info.project.name.startsWith('mobile')) await page.setViewportSize({width:320,height:900});
    if(provider==='digico') await seedDigico(page);
    const root=await openChannels(page,provider);
    const item=root.locator('[data-catalog-item="1"]');
    const label=item.locator(provider==='dlive'?'[data-key="label"]':'.digico-item-label');
    await label.fill('Keep this edited label');
    await item.getByRole('button',{name:/Choose channel icon/}).click();
    await page.locator('.digico-icon-option[data-icon="electric"]').click();
    await label.evaluate(el=>{window.mixEditedNode=el;});
    const destination=root.locator('[data-catalog-folder="legacy-3"] .catalog-header');
    await mouseMove(page,item.locator('.catalog-handle'),destination,true);
    await expect(root.locator('[data-catalog-folder="legacy-1"] [data-catalog-item="1"]')).toHaveCount(1);
    await mouseMove(page,item.locator('.catalog-handle'),destination);
    await expect(root).toHaveAttribute('aria-busy','false');
    await expect(root.locator('[data-catalog-folder="legacy-3"] [data-catalog-item="1"]')).toHaveCount(1);
    expect(await label.evaluate(el=>el===window.mixEditedNode)).toBe(true);
    await expect(label).toHaveValue('Keep this edited label');
    await expect(item.locator('.digico-item-icon')).toHaveValue('electric');
    // Create an empty group, then a duplicate name: IDs, not labels, define tabs.
    await prompt(page,()=>root.getByRole('button',{name:'New group',exact:true}).click(),'Empty group');
    await expect(root).toHaveAttribute('aria-busy','false');
    const band=root.locator('[data-catalog-folder="legacy-3"]');
    await prompt(page,()=>band.getByRole('button',{name:'Rename group',exact:true}).click(),'Vocals');
    await expect(root).toHaveAttribute('aria-busy','false');
    const before=(await (await page.request.get(prefix+'health')).json()).config;
    await page.route('**/api/digico/setup',async route=>{
      if(route.request().method()==='POST') await route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({ok:false,error:'Fixture storage unavailable'})});
      else await route.continue();
    });
    await page.locator('#digico-setup-save').click();
    await expect(page.locator('#digico-setup-message')).toContainText('Fixture storage unavailable');
    await expect(label).toHaveValue('Keep this edited label');
    expect((await (await page.request.get(prefix+'health')).json()).config).toEqual(before);
    await page.unroute('**/api/digico/setup'); await save(page); await page.reload();
    await showChannels(page, root);
    await expect(root.locator('[data-catalog-item="1"] .digico-item-icon')).toHaveValue('electric');
    await page.screenshot({path:info.outputPath(provider+'-drag-mobile-reduced.png'),fullPage:true});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    await page.goto('/personal-mixes'); await page.getByRole('button',{name:provider==='dlive'?'IEM':'SD IEM',exact:true}).click();
    await expect(page.getByRole('tab')).toHaveText(['Vocals1 input','Vocals3 inputs']);
    await page.getByRole('tab').nth(1).click();
    await expect(page.locator('.digico-channel:visible').first()).toContainText(provider==='dlive'?'Input 3':'SD 3');
    await page.goto('/config/digico'); await expect(root.locator('.catalog-handle')).toHaveCount(4);
    await page.locator('[data-bs-target="#digico-tab-channels"]').click();
    await prompt(page,()=>root.locator('[data-catalog-folder="legacy-3"]').getByRole('button',{name:'Delete group',exact:true}).click());
    await expect(root).toHaveAttribute('aria-busy','false');
    await expect(root.locator('[data-catalog-folder=""] [data-catalog-item]')).toHaveCount(3);
    await save(page); await page.reload();
    await expect(root.locator('[data-catalog-folder=""] [data-catalog-item]')).toHaveCount(3);
    const health=await (await page.request.get(prefix+'health')).json();
    expect(health.config[provider+'_channel_organization'].folders.map(f=>f.name)).toEqual(['Vocals','Empty group']);
    expect(health.messages.filter(m=>m.startsWith('b5')||m.startsWith('95')||m.startsWith('f000001a50100100030d'))).toEqual([]);
    // A deliberately failed HTTP response is expected; window errors are not.
    expect(errors).toEqual([]);
  });
}
for (const [provider, kind, gesture] of [
  ['digico','channel','pointer'], ['digico','aux','keyboard'],
  ['dlive','channel','keyboard'], ['dlive','aux','pointer'],
]) for (const finish of ['cancel','drop']) {
  test(`${provider} ${kind} delayed discovery during native ${gesture}: ${finish}`, async ({page}, info) => {
    const errors = collectPageErrors(page);
    await page.clock.install();
    await page.emulateMedia({reducedMotion:'reduce'});
    if (provider === 'digico') await seedDigico(page);
    const channels = await openChannels(page, provider);
    const root = kind === 'channel' ? channels : page.locator(provider === 'dlive' ? '#dlive-aux-mappings' : '#digico-aux-table');
    if (kind === 'aux') {
      await page.locator('[data-bs-target="#digico-tab-auxes"]').click();
      await expect(page.locator('#digico-tab-auxes')).toHaveCSS('opacity','1');
    }
    await expect(root).toBeVisible();
    await expect(root).toHaveAttribute('aria-busy','false');
    const before = (await (await page.request.get(prefix+'health')).json()).config;
    const key = provider + (kind === 'channel' ? '_channels' : '_auxes');
    const original = before[key];
    const added = kind === 'channel' ? {channel:5,label:'Discovered later'} :
      (provider === 'dlive' ? {channel:1003,bus:3,stereo:false,label:'Discovered later'} : {channel:3,label:'Discovered later'});
    // Another administrator's dedicated save changes actual backend counts.
    const response = await page.request.post('/api/digico/setup', {
      headers:{'X-CSRF-Token':'dlive-csrf',Origin:new URL(page.url()).origin},
      data:{[key]:original.concat([added])},
    });
    expect(response.ok()).toBe(true);
    expect((await (await page.request.get(prefix+'health')).json()).config[key]).toHaveLength(original.length+1);
    let release, received;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { received = resolve; });
    let armed = true;
    await page.route('**/api/digico/setup', async route => {
      if (route.request().method() !== 'GET' || !armed) return route.continue();
      armed = false;
      const actual = await route.fetch();
      received(); await held;
      await route.fulfill({response:actual});
    });
    const handle = root.locator('.catalog-handle').first();
    const count = original.length;
    await handle.evaluate(el => {
      el.scrollIntoView({block:'center',behavior:'instant'});
      window.pollDragHandle = el;
      window.pollDragRow = el.parentElement.querySelector('.digico-config-row, .dlive-mapping-row');
    });
    try {
      await page.clock.runFor(3000); await started;
      if (gesture === 'keyboard') {
        await handle.focus(); await page.keyboard.press('Space'); await page.keyboard.press('ArrowDown');
      } else {
        const box = await handle.boundingBox();
        await page.mouse.move(box.x+box.width/2,box.y+box.height/2); await page.mouse.down();
        await page.mouse.move(box.x+box.width/2+12,box.y+box.height/2,{steps:3});
        const destination = kind === 'channel' ? root.locator('[data-catalog-folder="legacy-3"] .catalog-header') : root.locator('[data-catalog-item]').nth(1);
        await destination.evaluate(el => el.scrollIntoView({block:'center',behavior:'instant'}));
        const dest = await destination.boundingBox();
        await page.mouse.move(dest.x+dest.width/2,dest.y+dest.height-(kind === 'channel' ? dest.height/2 : 3),{steps:10});
        await expect(page.locator('.catalog-drop-target')).toHaveCount(1);
      }
      await expect.poll(() => root.evaluate(el => !!el._catalogMoving && el._catalogMoving())).toBe(true);
      if (gesture === 'pointer') {
        await expect(page.locator('.catalog-drag-ghost')).toBeVisible();
        await page.locator('.catalog-drag-ghost').evaluate(el => { window.pollDragGhost = el; });
      }
      const delivered = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method()==='GET');
      release(); await delivered;
      // Wait for the real async JSON/render continuation, not just HTTP receipt.
      await expect(page.locator(kind==='channel'?'#digico-channel-count':'#digico-aux-count')).toHaveText(String(count+1));
      expect(await handle.evaluate(el => el === window.pollDragHandle)).toBe(true);
      expect(await root.evaluate(el => el._catalogMoving())).toBe(true);
      if (gesture === 'pointer') expect(await page.locator('.catalog-drag-ghost').evaluate(el => el === window.pollDragGhost)).toBe(true);
      await page.screenshot({path:info.outputPath('delayed-poll-active-drag.png')});
      if (finish === 'cancel') await page.keyboard.press('Escape');
      else if (gesture === 'keyboard') await page.keyboard.press('Enter');
      if (gesture === 'pointer') await page.mouse.up();
      await expect.poll(() => root.evaluate(el => el._catalogMoving())).toBe(false);
      await expect(root).toHaveAttribute('aria-busy','false');
      const refreshed = page.waitForResponse(r => r.url().endsWith('/api/digico/setup') && r.request().method()==='GET');
      await page.clock.runFor(3000);
      await (await refreshed).finished();
      if (finish === 'cancel') {
        await expect(root.locator('.catalog-handle')).toHaveCount(count+1);
        expect(await handle.evaluate(el => el === window.pollDragHandle)).toBe(false);
      } else {
        await expect(page.locator('#digico-setup-message')).toContainText('Unsaved');
        await expect(root.locator('.catalog-handle')).toHaveCount(count);
        expect(await root.evaluate(el => el.contains(window.pollDragRow))).toBe(true);
        const draftIds = await root.locator('[data-catalog-item]').evaluateAll(rows => rows.map(row => Number(row.dataset.catalogItem)));
        expect(draftIds).not.toEqual(original.map((item,index) => item.channel || index+1));
        await save(page);
        const saved = (await (await page.request.get(prefix+'health')).json()).config[key];
        expect(saved).toHaveLength(count);
        if (provider === 'dlive') expect(saved.map(row=>row.channel)).toEqual(draftIds);
        else expect(saved.map((row,index)=>({id:index+1,order:row.order})).sort((a,b)=>a.order-b.order).map(row=>row.id)).toEqual(draftIds);
      }
      expect((await (await page.request.get(prefix+'health')).json()).checkout_db_unchanged).toBe(true);
      expect(errors).toEqual([]);
    } finally { release(); await page.mouse.up(); }
  });
}

test('native mobile Chromium touch reorders AUXes without changing ordinals',async({page,browserName},info)=>{
  test.skip(browserName!=='chromium'||!info.project.use.isMobile,'Native touch gesture uses Chromium CDP');
  await page.goto('/config/digico');
  const root=page.locator('#dlive-aux-mappings');
  await expect(root.locator('.catalog-handle')).toHaveCount(2);
  await page.locator('[data-bs-target="#digico-tab-auxes"]').click();
  await root.evaluate(el=>el.scrollIntoView({block:'center',behavior:'instant'}));
  const handle=root.locator('.catalog-handle').first();
  const destination=root.locator('[data-catalog-item]').nth(1);
  const a=await handle.boundingBox(),b=await destination.boundingBox();
  const start={x:a.x+a.width/2,y:a.y+a.height/2};
  const end={x:b.x+20,y:b.y+b.height-4};
  const session=await page.context().newCDPSession(page);
  await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[start]});
  for(let n=1;n<=10;n++) await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x+(end.x-start.x)*n/10,y:start.y+(end.y-start.y)*n/10}]});
  await expect(page.locator('.catalog-drag-ghost')).toBeVisible();
  await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await expect(root).toHaveAttribute('aria-busy','false');
  await expect(root.locator('[data-key="bus"]').first()).toHaveValue('1');
  await save(page); await page.reload();
  await expect(root.locator('.catalog-handle')).toHaveCount(2);
  const auxes=(await (await page.request.get(prefix+'health')).json()).config.dlive_auxes;
  expect(auxes.map(a=>[a.channel,a.bus,!!a.stereo])).toEqual([[1001,1,false],[2002,2,true]]);
});
