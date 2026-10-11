const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function templateContext(name) {
  const source = fs.readFileSync(path.join(__dirname, '..', 'templates', name), 'utf8');
  const script = source.match(/<script>([\s\S]*?)<\/script>/)[1].split('(async () => {')[0];
  const elements = {};
  const stored = {};
  const context = vm.createContext({URLSearchParams, Date, Math, JSON,
    window: {location: {search: '?draft=test', href: ''}},
    sessionStorage: {getItem: key => stored[key], setItem: (key, value) => {stored[key] = value;}},
    document: {getElementById: id => elements[id] || null,
      createElement: () => ({style: {}, dataset: {}})},
  });
  vm.runInContext(script, context);
  return {run: code => vm.runInContext(code, context), elements, stored};
}

test('homepage save, restore and marker preserve explicit Ignore', () => {
  const ctx = templateContext('videohub.html');
  ctx.run('_vhRouting = _vhRoutingFromPreset({routes:[{output:1,input:null},{output:2,input:7}]});');
  assert.equal(ctx.run('_vhRouting[0]'), null);
  const routes = JSON.parse(ctx.run('JSON.stringify(_vhRoutesFromRouting())'));
  assert.deepEqual(routes.slice(0, 2), [{output: 1, input: null}, {output: 2, input: 7}]);
  ctx.stored.vh_draft_test = JSON.stringify({routing: [null, 7], name: 'Mixed'});
  assert.equal(ctx.run('_vhTryLoadDraftFromQuery()'), true);
  assert.equal(ctx.run('_vhRouting[0]'), null);
  ctx.elements['vh-room-tabs'] = {};
  const markers = [];
  ctx.elements['vh-room-stage'] = {style: {}, appendChild: btn => markers.push(btn)};
  ctx.run('_vhRenderRooms()');
  assert.match(markers[0].innerHTML, /Keep Current Routing/);
  assert.match(markers[0].className, /vh-marker-ignore/);
});

test('image reference survives save, draft restore and picker confirmation', () => {
  const ctx = templateContext('videohub.html');
  ctx.run("_vhRouting = _vhRoutingFromPreset({routes:[{output:1,routing_preset:'saved-image'}]});");
  assert.deepEqual(JSON.parse(ctx.run('JSON.stringify(_vhRoutesFromRouting()[0])')),
    {output: 1, routing_preset: 'saved-image'});
  ctx.stored.vh_draft_test = JSON.stringify({routing: [{routing_preset: 'saved-image'}]});
  ctx.run('_vhTryLoadDraftFromQuery()');
  assert.equal(ctx.run('_vhRouting[0].routing_preset'), 'saved-image');
  const picker = templateContext('videohub_input_select.html');
  picker.run("_vhiDraftToken='test'; _vhiOutput=1;");
  picker.stored.vh_draft_test = JSON.stringify({routing: [{routing_preset: 'saved-image'}]});
  picker.run('_vhiLoadDraft(); _vhiSaveDraftAndReturn();');
  assert.equal(JSON.parse(picker.stored.vh_draft_test).routing[0].routing_preset, 'saved-image');
});

test('input picker offers Ignore outside filters and confirms null in draft', () => {
  const ctx = templateContext('videohub_input_select.html');
  ctx.run("_vhiDraftToken='test'; _vhiOutput=1; _vhiRoomsCfg={filtered_inputs:[7]};");
  ctx.stored.vh_draft_test = JSON.stringify({routing: [null, 7]});
  ctx.run('_vhiLoadDraft()');
  assert.equal(ctx.run('_vhiSelectedInput'), null);
  ctx.elements['vhi-grid'] = {};
  ctx.run('_vhiRenderGrid()');
  assert.match(ctx.elements['vhi-grid'].innerHTML, /data-input="ignore"/);
  assert.match(ctx.elements['vhi-grid'].innerHTML, /Keep Current Routing/);
  assert.match(ctx.elements['vhi-grid'].innerHTML, /active[^>]*data-input="ignore"/);
  ctx.run('_vhiSaveDraftAndReturn()');
  assert.equal(JSON.parse(ctx.stored.vh_draft_test).routing[0], null);
});
