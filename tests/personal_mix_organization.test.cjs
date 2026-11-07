const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function fixture() {
  let rendered;
  const window = {renderCatalogOrganization(root, tree, rows, options) { rendered = {root, tree, rows, options}; }};
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../static/personal_mix_organization.js'), 'utf8'), {window});
  return {api:window.TDeckMixOrganization, rendered:()=>rendered};
}
const plain = value => JSON.parse(JSON.stringify(value));
test('legacy headings preserve section membership, repeated names and case-sensitive names',()=>{
  const {api}=fixture();
  const rows=['','Vocals','Vocals','','Vocals','vocals','<FX> / 🎸'].map((group,i)=>({id:i+1,group}));
  assert.deepEqual(plain(api.legacy(rows,true)),{
    folders:[{id:'legacy-2',name:'Vocals'},{id:'legacy-5',name:'Vocals'},{id:'legacy-6',name:'vocals'},{id:'legacy-7',name:'<FX> / 🎸'}],
    items:[{id:'1',folderId:null},{id:'2',folderId:'legacy-2'},{id:'3',folderId:'legacy-2'},{id:'4',folderId:'legacy-2'},
      {id:'5',folderId:'legacy-5'},{id:'6',folderId:'legacy-6'},{id:'7',folderId:'legacy-7'}],
  });
});
test('flat AUX mode does not turn legacy AUX labels into folders',()=>{
  const {api}=fixture();
  assert.deepEqual(plain(api.legacy([{id:2002,group:'Stage'},{id:1001,group:'Band'}],false)),
    {folders:[],items:[{id:'2002',folderId:null},{id:'1001',folderId:null}]});
});
test('shared renderer stages drafts and preserves stable nodes and group IDs',()=>{
  const f=fixture(); let dirty=0;
  const rows=[{id:'1',node:{value:'edited label'}},{id:'2',node:{icon:'drums'}}];
  const original={folders:[{id:'b',name:'Band'}],items:[{id:'1',folderId:'b'},{id:'2',folderId:null}]};
  const controller=f.api.attach({},original,rows,true,()=>dirty++);
  const r=f.rendered();
  assert.equal(r.options.draft,true); assert.equal(r.options.folders,true); assert.equal(r.rows[0].node,rows[0].node);
  r.tree.folders[0].name='Renamed'; r.tree.items.reverse(); r.options.save(r.tree);
  assert.equal(dirty,1); assert.deepEqual(plain(controller.organization()),plain(r.tree));
  assert.equal(original.folders[0].name,'Band');
});
test('manual input numbers serialize as physical identities, never row positions',()=>{
  const f=fixture(); let number=3;
  const controller=f.api.attach({},null,[{id:'draft',node:{},identity:()=>number}],true,()=>{});
  assert.equal(controller.organization().items[0].id,'3'); number=128;
  assert.equal(controller.organization().items[0].id,'128');
});
test('add and delete reconcile only membership, retaining empty named groups',()=>{
  const f=fixture(), root={}; let dirty=0;
  const node={};
  const controller=f.api.attach(root,{folders:[{id:'stable',name:'Keep'}],items:[{id:'1',folderId:'stable'}]},[{id:'1',node}],true,()=>dirty++);
  const added={id:'draft',node:{},identity:()=>7}; controller.add(added); controller.remove(node);
  assert.equal(dirty,2);
  assert.deepEqual(plain(controller.organization()),{folders:[{id:'stable',name:'Keep'}],items:[{id:'7',folderId:null}]});
  assert.equal(f.rendered().rows[0].node,added.node);
});
