const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const timers = source.slice(source.indexOf('// --- Timers page ---'), source.indexOf('\n_initTimersPage();'));
const deferred = () => {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};};
const response = data => ({ok: true, json: async () => data});
const tick = () => new Promise(r => setImmediate(r));
function setup() {
  const elements = {};
  function el(value = '') {return {value, disabled: false, dataset: {}, handlers: {},
    addEventListener(type, fn) {this.handlers[type] = fn;}, setAttribute() {}, removeAttribute() {}};}
  for (const id of ['timers-page', 'timers-stage-delay-send', 'timers-presets-body']) elements[id] = el();
  elements['timers-stage-delay-preset'] = el('2');
  elements['timers-stage-delay-minutes'] = el('2');
  elements['timers-stage-update-timer'] = el(); elements['timers-stage-update-timer'].checked = true;
  const c = vm.createContext({console, setTimeout, clearTimeout, crypto: webcrypto, Uint8Array,
    document: {getElementById: id => elements[id] || null},
    ui: [{name: 'A', time: '09:00'}, {name: 'B', time: '10:00'}], elements});
  vm.runInContext(timers, c);
  vm.runInContext(`
    _timersLoad = async () => {};
    _timersSetStatus = () => {}; _timersClearStatus = () => {};
    _timersStageSetDelayHint = () => {}; _timersStageSetDelayPreview = () => {};
    _timersStageSetPreview = () => {}; _timersStageRenderOptions = () => {};
    _timersApplyStagePresetId = () => {}; _timersReadStagePresetFromUI = () => 1;
    _timersReadPresetsForStage = () => ui; _timersReadPresetsFromUI = () => ui;
    function makeRow(p, i) {
      const row = {dataset: {index: String(i)}, isConnected: true,
        querySelector: s => s.includes('preset-time') ? row.time : s.includes('preset-name') ? row.name : null};
      row.time = {value: p.time, disabled: false}; row.name = {value: p.name, disabled: false}; return row;
    }
    let rows = ui.map(makeRow);
    _timersActualRenderPresets = _timersRenderPresets;
    _timersRenderPresets = p => {rows.forEach(r => {_timersClearRowSaveTimer(r); r.isConnected = false;});
      ui = JSON.parse(JSON.stringify(p)); rows = ui.map(makeRow); _timersRefreshEditingLock();};
    elements['timers-presets-body'].querySelectorAll = s => s.includes('data-dirty') ? rows.filter(r => r.dataset.dirty === '1') : rows;
    elements['timers-page'].querySelectorAll = () => [...Object.values(elements).filter(e => e !== elements['timers-page']), ...rows.flatMap(r => [r.time, r.name])];
    _timersRevision = 'R0'; _initTimersPage();
  `, c);
  // The stub uses the real lock when present, and remains runnable on the RED baseline.
  if (!c._timersRefreshEditingLock) c._timersRefreshEditingLock = () => {};
  return c;
}

test('delay handler re-reads selected position after pending move flush', async () => {
  const c = setup(), move = deferred(); let sent;
  c.fetch = async (url, opts) => {if (url.includes('/mutate')) return move.promise;
    sent = JSON.parse(opts.body); return response({ok: true, sent: true});};
  vm.runInContext(`_timersQueueMutation({action: 'move_preset', preset: 2, direction: 'up'}, {render: true});
    elements['timers-stage-delay-send'].handlers.click({preventDefault() {}});`, c);
  await tick();
  move.resolve(response({ok: true, revision: 'R1', stream_start_preset: 2, stream_delay_preset: 1,
    timer_presets: [{name: 'B', time: '10:00'}, {name: 'A', time: '09:00'}]}));
  await tick(); await tick();
  assert.equal(sent.preset, 1, 'old position targets A instead of moved B');
  assert.equal(sent.revision, 'R1');
  assert.match(sent.operation_id || '', /^[A-Za-z0-9_-]{16,128}$/);
  const firstId = sent.operation_id;
  vm.runInContext(`elements['timers-stage-delay-send'].handlers.click({preventDefault() {}});`, c);
  await tick(); await tick();
  assert.notEqual(sent.operation_id, firstId, 'each explicit click must have a fresh ID');
});

test('execution freezes editing and keyboard/structural paths, flushes drafts and restores controls', async () => {
  const c = setup(), delay = deferred(); const writes = []; let sent;
  c.fetch = async (url, opts) => {
    const payload = JSON.parse(opts.body);
    if (url.includes('/stream_delay')) {sent = payload; return delay.promise;}
    writes.push(payload);
    return response({ok: true, revision: 'draft', stream_delay_preset: 2,
      timer_presets: [{name: 'A', time: '09:00'}, {name: 'DRAFT', time: '10:00'}]});
  };
  vm.runInContext(`rows[1].name.value = 'DRAFT'; _timersMarkRowDirty(rows[1]);
    elements['timers-stage-delay-send'].handlers.click({preventDefault() {}});`, c);
  await tick(); await tick();
  assert.equal(writes[0].patch.name, 'DRAFT');
  assert.equal(sent.revision, 'draft');
  assert.equal(vm.runInContext('rows[1].name.disabled', c), true, 'editing remains enabled during device execution');
  for (const type of ['click', 'input', 'change', 'keydown', 'beforeinput']) {
    const handler = c.elements['timers-page'].handlers[type];
    assert.equal(typeof handler, 'function', `${type} fallback guard missing`);
    let blocked = false;
    handler({preventDefault() {blocked = true;}, stopImmediatePropagation() {}});
    assert.equal(blocked, true);
  }
  delay.resolve(response({ok: true, saved: true, applied: true, sent: true, revision: 'adjusted', stream_delay_preset: 2,
    timer_presets: [{name: 'A', time: '09:00'}, {name: 'DRAFT', time: '10:02'}]}));
  await tick(); await tick();
  assert.equal(c.ui[1].name, 'DRAFT');
  assert.equal(c.ui[1].time, '10:02');
  assert.equal(vm.runInContext('rows[1].name.disabled', c), false);
  await new Promise(r => setTimeout(r, 750));
  assert.equal(writes.length, 1, 'detached autosave reverted adjusted time');
});

test('actual renderer cancels row debounces and detached rows cannot save', async () => {
  const c = setup(); let writes = 0;
  c.fetch = async () => {writes += 1; return response({ok: true});};
  vm.runInContext(`_timersMarkRowDirty(rows[1], {delayMs: 25});
    _timersActualRenderPresets([]);`, c);
  await new Promise(r => setTimeout(r, 50));
  assert.equal(writes, 0);
  vm.runInContext(`rows[1].isConnected = false; _timersQueueRowUpdate(rows[1]);`, c);
  await tick();
  assert.equal(writes, 0);
});

test('failed draft flush or uncertain request restores controls without replay', async () => {
  for (const failure of ['save', 'network', 'json']) {
    const c = setup(); let calls = 0;
    c.fetch = async () => {
      calls += 1;
      if (failure === 'save') return {ok: false, json: async () => ({ok: false, error: 'synthetic save failure'})};
      if (failure === 'network') throw new Error('synthetic network failure');
      return {ok: true, json: async () => {throw new Error('synthetic invalid JSON');}};
    };
    vm.runInContext(`${failure === 'save' ? "rows[1].name.value = 'KEEP DRAFT'; _timersMarkRowDirty(rows[1]);" : ''}
      elements['timers-stage-delay-send'].handlers.click({preventDefault() {}});`, c);
    await tick(); await tick();
    assert.equal(calls, 1);
    assert.equal(vm.runInContext('rows[1].name.disabled', c), false);
    assert.equal(c.elements['timers-stage-delay-send'].disabled, false);
    if (failure === 'save') assert.equal(vm.runInContext('rows[1].name.value', c), 'KEEP DRAFT');
  }
});
