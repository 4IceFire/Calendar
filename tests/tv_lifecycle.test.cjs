'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('page hide aborts the shared-state read; cached-page restore resumes once', async () => {
  const events = {};
  const reads = [];
  const timers = new Map();
  let nextTimer = 0;
  const root = {dataset: {mode: 'diagnostics'}};
  const message = {textContent: ''};
  const document = {
    hidden: false,
    getElementById(id) { return id === 'tv-pages' ? root : (id === 'tv-message' ? message : null); },
    addEventListener(name, handler) { events[name] = handler; },
  };
  const context = vm.createContext({
    document,
    window: {addEventListener(name, handler) { events[name] = handler; }},
    AbortController,
    setTimeout(handler, delay) { timers.set(++nextTimer, {handler, delay}); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      reads.push({url, signal: options.signal});
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => { const error = new Error('aborted'); error.name = 'AbortError'; reject(error); });
      });
    },
  });
  const script = process.env.TDECK_TV_SCRIPT || path.join(__dirname, '..', 'static', 'tv_pages.js');
  vm.runInContext(fs.readFileSync(script, 'utf8'), context);
  assert.equal(reads.length, 1);
  assert.equal(typeof events.beforeunload, 'function', 'cancel reads before navigation policy checks, not only after pagehide');
  events.beforeunload();
  assert.equal(reads[0].signal.aborted, true);
  events.pagehide();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 0);
  assert.equal(message.textContent, '');
  events.pageshow();
  assert.equal(reads.length, 2);
  events.pageshow();
  assert.equal(reads.length, 2, 'restoring must not multiply shared snapshot reads');
  events.pagehide();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads[1].signal.aborted, true);
  assert.equal(timers.size, 0);
});
