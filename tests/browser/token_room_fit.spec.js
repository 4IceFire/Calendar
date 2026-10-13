const {test,expect}=require('@playwright/test');const {installCommonReadMocks}=require('./helpers');
const marker='/__videohub_ignore_fixture__';const headers=()=>({Origin:new URL(process.env.TDECK_BASE_URL).origin,'X-CSRF-Token':'ignore-csrf'});
test.setTimeout(60000);
test.beforeEach(async({page})=>{expect((await(await page.request.get(marker+'/health')).json()).isolated).toBe(true);expect((await page.request.post(marker+'/reset')).ok()).toBe(true);expect((await page.request.get(marker+'/login')).ok()).toBe(true);await installCommonReadMocks(page);});
test('edit token permissions preserves identity, cancels cleanly, confirms changes and keeps failed draft',async({page})=>{
 const r=await page.request.post('/api/config/service-tokens',{headers:headers(),data:{name:'Companion fixture',scopes:['read'],constraints:{videohub_outputs:[1]},expires_in_days:30}});expect(r.status()).toBe(201);const original=(await r.json()).token;
 await page.goto('/config/api-tokens');await page.getByRole('button',{name:'Edit permissions',exact:true}).click();await expect(page.locator('#api-token-edit-vh-outputs')).toHaveValue('1');await expect(page.locator('#api-token-edit-modal')).toHaveCSS('opacity','1');await expect(page.locator('#api-token-edit-save')).toBeVisible();await expect.poll(()=>page.locator('#api-token-edit-save').evaluate(el=>{const b=el.getBoundingClientRect();return b.top>=0&&b.height>0&&b.bottom<=innerHeight;})).toBe(true);await page.screenshot({path:test.info().outputPath('token-edit-modal.png')});await page.locator('#api-token-edit-scope-timers').check();await page.locator('#api-token-edit-modal').getByRole('button',{name:'Cancel',exact:true}).click();
 let stored=(await(await page.request.get('/api/config/service-tokens')).json()).tokens[0];expect(stored.scopes).toEqual(['read']);
 await page.getByRole('button',{name:'Edit permissions',exact:true}).click();await page.locator('#api-token-edit-scope-timers').check();await page.locator('#api-token-edit-vh-outputs').fill('1, 2');await page.locator('#api-token-edit-confirmed').check();await page.locator('#api-token-edit-save').click();await expect(page.locator('#api-token-edit-modal')).not.toBeVisible();
 stored=(await(await page.request.get('/api/config/service-tokens')).json()).tokens[0];expect(stored.id).toBe(original.id);expect(stored.expires_at).toBe(original.expires_at);expect(stored.scopes).toEqual(['read','timers']);expect(stored.constraints.videohub_outputs).toEqual([1,2]);expect(stored.token).toBeUndefined();expect(stored.token_hash).toBeUndefined();expect(await page.locator('#api-token-secret').inputValue()).toBe('');
 await page.getByRole('button',{name:'Edit permissions',exact:true}).click();await page.locator('#api-token-edit-vh-outputs').fill('3');await page.route('**/api/config/service-tokens/'+original.id,route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({ok:false,message:'Permissions changed. Refresh.'})}));await page.locator('#api-token-edit-confirmed').check();await page.locator('#api-token-edit-save').click();await expect(page.locator('#api-token-edit-error')).toContainText('Refresh');await expect(page.locator('#api-token-edit-vh-outputs')).toHaveValue('3');
});
test('full room image and markers share image bounds on desktop/mobile and both aspect ratios',async({page},info)=>{
 const created=await page.request.post('/api/videohub/presets',{headers:headers(),data:{name:'Full room',routes:[{output:1,input:null},{output:2,input:7}]}});expect(created.ok()).toBe(true);
 for(const shape of ['wide','portrait']){
  expect((await page.request.post(marker+'/set-image?shape='+shape)).ok()).toBe(true);
  for(const pathname of ['/videohub?room=fixture','/videohub/rooms','/videohub/monitor']){
   await page.goto(pathname);const kind=pathname.includes('/rooms')?'editor':pathname.includes('/monitor')?'monitor':'presets';const stage=page.locator(kind==='editor'?'#vhr-stage':kind==='monitor'?'.vhm-stage[data-room-background]':'#vh-room-stage');
   for(const size of [{width:1920,height:1080},{width:1280,height:650},{width:320,height:640},{width:390,height:844}]){
    await page.setViewportSize(size);await expect(stage).toHaveAttribute('data-image-ready','true');
    await expect.poll(()=>stage.evaluate((el,shape)=>{const b=el.getBoundingClientRect(),cs=getComputedStyle(el),bl=parseFloat(cs.borderLeftWidth)||0,br=parseFloat(cs.borderRightWidth)||0,bt=parseFloat(cs.borderTopWidth)||0,bb=parseFloat(cs.borderBottomWidth)||0;const markers=[...el.querySelectorAll('.vh-marker,.vhr-marker,.vhm-marker')];const ratio=shape==='wide'?3:.5;return Math.abs((b.width-bl-br)/(b.height-bt-bb)-ratio)<.01&&getComputedStyle(el).backgroundSize==='100% 100%'&&markers.length===2&&markers.every((m,i)=>{const r=m.getBoundingClientRect();return r.left>=b.left&&r.right<=b.right&&Math.abs((r.left+r.width/2)-(b.left+bl+(b.width-bl-br)*(i===0?.25:.75)))<3;});},shape)).toBe(true);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if(size.width===1920&&pathname==='/videohub?room=fixture')expect(await page.locator('#main-content').evaluate(el=>el.getBoundingClientRect().width)).toBeGreaterThan(1700);
    await page.screenshot({path:info.outputPath(shape+'-'+size.width+'-'+kind+'.png'),fullPage:true});
   }
  }
 }
 expect((await(await page.request.get(marker+'/health')).json()).writes).toEqual([]);
});
