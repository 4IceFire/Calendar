const {test, expect} = require('@playwright/test');
const {installCommonReadMocks, collectPageErrors} = require('./helpers');
const marker = '/__videohub_ignore_fixture__';
const headers = () => ({Origin: new URL(process.env.TDECK_BASE_URL).origin, 'X-CSRF-Token': 'ignore-csrf'});
test.setTimeout(60000);
async function catalog(page){return (await (await page.request.get('/api/videohub/presets')).json()).presets;}
async function health(page){return (await (await page.request.get(marker+'/health')).json());}
async function seed(page,name='Fixture preset'){const res=await page.request.post('/api/videohub/presets',{headers:headers(),data:{name,routes:Array.from({length:40},(_,i)=>({output:i+1,input:null}))}});expect(res.ok()).toBe(true);return (await res.json()).preset;}
async function create(page,name,mode){await page.locator('#vh-new').click();await page.locator('#vh-new-name').fill(name);await page.locator('#vh-new-mode').selectOption(mode);await page.locator('#vh-new-create').click();await expect(page.locator('#vh-name')).toHaveValue(name);}
test.beforeEach(async({page})=>{expect((await health(page)).fixture).toBe('tdeck-videohub-ignore-ui');expect((await page.request.post(marker+'/reset')).ok()).toBe(true);expect((await page.request.get(marker+'/login')).ok()).toBe(true);await installCommonReadMocks(page);});

test('new popup cancel, three immediate saved modes and duplicate beside name',async({page})=>{
 const errors=collectPageErrors(page); await page.goto('/videohub');
 await expect(page.getByRole('heading',{name:'VideoHub Presets',exact:true})).toBeVisible();
 await page.locator('#vh-new').click();await page.locator('#vh-new-name').fill('Cancelled');await page.locator('#vh-new-cancel').click();expect(await catalog(page)).toEqual([]);
 for(const mode of ['identity','device','keep']){await create(page,mode,mode);const p=(await catalog(page)).find(p=>p.name===mode);expect(p.routes).toHaveLength(40);expect(p.routes[0].input).toBe(mode==='keep'?null:1);}
 await page.locator('#vh-duplicate').click();await expect(page.locator('#vh-name')).toHaveValue('keep (Copy)');expect((await catalog(page)).map(p=>p.name)).toContain('keep');expect((await health(page)).writes).toEqual([]);expect(errors).toEqual([]);
});

test('serialized coalesced rename, flush before switching, empty/failed draft retained',async({page})=>{
 const first=await seed(page);const second=await seed(page,'Other');await page.goto('/videohub?preset='+first.id);
 await expect(page.locator('#vh-name')).toHaveValue('Fixture preset');
 let release, started;const waiting=new Promise(r=>started=r);const gate=new Promise(r=>release=r);let writes=0;
 await page.route('**/api/videohub/presets/'+first.id,async route=>{if(route.request().method()!=='PUT')return route.continue();writes++;if(writes===1){started();await gate;}return route.continue();});
 await page.locator('#vh-name').fill('First edit');await waiting;
 await page.locator('#vh-name').fill('Latest edit');
 const switchClick=page.locator(`[data-act="select"][data-id="${second.id}"]`).click();release();await switchClick;
 await expect(page.locator('#vh-name')).toHaveValue('Other');expect((await catalog(page)).find(p=>p.id===first.id).name).toBe('Latest edit');expect(writes).toBe(2);
 await page.locator('#vh-name').fill('');await expect(page.locator('#vh-save-status')).toContainText('required');await page.locator(`[data-act="select"][data-id="${first.id}"]`).click();await expect(page.locator('#vh-name')).toHaveValue('');
 await page.route('**/api/videohub/presets/'+second.id,route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({ok:false,error:'Changed by another operator'})}));
 await page.locator('#vh-name').fill('Recoverable draft');await expect(page.locator('#vh-save-status')).toContainText('Changed by another operator');await expect(page.locator('#vh-name')).toHaveValue('Recoverable draft');expect((await catalog(page)).find(p=>p.id===second.id).name).toBe('Other');
});

test('explicit output selection persists numeric Keep and actual image ID, open/search/cancel no mutation',async({page})=>{
 const p=await seed(page);await page.goto('/videohub?preset='+p.id+'&room=fixture');await expect(page.locator('.vh-marker')).toHaveCount(2);
 await page.locator('[data-output="1"]').click();await page.getByRole('button',{name:'Routing Preset',exact:true}).click();await page.locator('#vhi-preset-search').fill('Welcome');await page.locator('#vhi-cancel').click();expect((await catalog(page))[0].routes[0].input).toBe(null);
 await page.locator('[data-output="1"]').click();await page.locator('[data-input="7"]').click();await expect(page.locator('[data-output="1"]')).toContainText('7');expect((await catalog(page))[0].routes[0].input).toBe(7);
 await page.locator('[data-output="1"]').click();await page.locator('[data-input="ignore"]').click();await expect(page.locator('[data-output="1"]')).toContainText('Keep');
 await page.locator('[data-output="1"]').click();await page.getByRole('button',{name:'Routing Preset',exact:true}).click();await page.getByRole('button',{name:'Welcome image',exact:true}).click();await expect(page.locator('[data-output="1"]')).toContainText('Welcome');await expect(page.locator('#vh-selection-note')).toContainText('output 1');expect((await catalog(page))[0].routes[0]).toEqual({output:1,routing_preset:(await health(page)).routing_preset});expect((await health(page)).images).toEqual([]);expect((await health(page)).writes).toEqual([]);
});

test('small real device overrides fallback40 labels and dense unassigned geometry stays usable',async({page},info)=>{
 await page.goto('/videohub');
 expect((await page.request.post(marker+'/small-device')).ok()).toBe(true);
 await create(page,'Small device','device');
 await expect(page.locator('.vh-marker')).toHaveCount(2); // Outputs 1/2 belong to the fixture room.
 await page.locator('#vh-name').fill('Small renamed');await expect(page.locator('#vh-save-status')).toHaveText('Saved');
 expect((await catalog(page))[0].routes).toEqual([8,7,6,5].map((input,i)=>({output:i+1,input})));
 expect((await page.request.post(marker+'/reset')).ok()).toBe(true);
 await seed(page,'Dense Keep');await page.goto('/videohub');await expect(page.locator('.vh-marker')).toHaveCount(38);
 for(const size of [{width:1280,height:650},{width:320,height:640}]){
  await page.setViewportSize(size);await page.waitForTimeout(100);
  expect(await page.locator('#vh-room-stage').evaluate(stage=>{
   const box=stage.getBoundingClientRect();const rects=[...stage.querySelectorAll('.vh-marker')].map(x=>x.getBoundingClientRect());
   return rects.every((r,i)=>r.width>=90&&r.height>=40&&r.left>=box.left&&r.right<=box.right+1&&rects.slice(i+1).every(s=>r.right<=s.left||s.right<=r.left||r.bottom<=s.top||s.bottom<=r.top));
  })).toBe(true);
  await page.screenshot({path:info.outputPath('dense-'+size.width+'.png'),fullPage:true});
 }
});

test('short and long preset rows always place actions below metadata and list fills usable card',async({page},info)=>{
 await seed(page,'Short');await seed(page,'A very long preset name that wraps onto multiple lines on narrow panels');await page.goto('/videohub');await expect(page.locator('#vh-list [data-act="select"]')).toHaveCount(2);
 for(const size of (info.project.name.includes('mobile')?[{width:390,height:844},{width:320,height:640}]:[{width:1920,height:1080},{width:1280,height:650}])){
 await page.setViewportSize(size);await page.waitForTimeout(100);
 expect(await page.locator('#vh-list').evaluate(el=>{const card=el.closest('.card-body').getBoundingClientRect();return Math.abs(el.getBoundingClientRect().bottom-(card.bottom-parseFloat(getComputedStyle(el.closest('.card-body')).paddingBottom)))<4;})).toBe(true);
 expect(await page.locator('.vh-preset-row').evaluateAll(rows=>rows.every(row=>row.querySelector('[data-act="select"]').getBoundingClientRect().top>=row.querySelector('.vh-preset-meta').getBoundingClientRect().bottom))).toBe(true);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);await page.screenshot({path:info.outputPath('vh-'+size.width+'x'+size.height+'.png'),fullPage:true});}
});
