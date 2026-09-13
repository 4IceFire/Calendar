const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const telemetrySource = fs.readFileSync(path.join(__dirname, '../static/client_telemetry.js'), 'utf8');
const noiseCases = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/client_error_noise.json'), 'utf8'));

function installTelemetry(options = {}) {
  const listeners = {};
  const requests = [];
  let now = 100000;
  const attributes = {
    'data-endpoint': '/api/client-errors',
    'data-build-id': 'test-build',
    'data-csrf-token': 'test-csrf',
  };
  const window = {
    location: {href: 'https://tdeck.example/config/tvs?token=private#details', pathname: '/config/tvs'},
    addEventListener(kind, listener) { listeners[kind] = listener; },
    fetch(endpoint, init) {
      requests.push({endpoint, init, report: JSON.parse(init.body)});
      if (options.fetchError) throw new Error('transport failed');
      return options.fetchReject ? Promise.reject(new Error('transport failed')) : Promise.resolve();
    },
  };
  vm.runInNewContext(telemetrySource, {
    window,
    document: {currentScript: {getAttribute(name) { return attributes[name]; }}},
    URL,
    Date: {now() { return now; }},
  }, {filename: 'client_telemetry.js'});
  return {
    requests,
    emit(kind, event) { listeners[kind](event); },
    advanceTime(ms) { now += ms; },
  };
}

function errorEvent(message, source = '', stack = '', line = 1) {
  return {message, filename: source, lineno: line, colno: 2, error: {message, stack}};
}

for (const entry of noiseCases) {
  test('window error classification: ' + entry.name, () => {
    const telemetry = installTelemetry();
    let prevented = false;
    const event = errorEvent(entry.message, entry.source, entry.stack);
    event.preventDefault = () => { prevented = true; };
    telemetry.emit('error', event);
    assert.equal(telemetry.requests.length, entry.ignored ? 0 : 1);
    assert.equal(prevented, false, 'browser console handling must remain unchanged');
  });

  // Rejections do not supply a script filename, so compare all source-free fixtures too.
  if (!entry.source) {
    test('promise rejection classification: ' + entry.name, () => {
      const telemetry = installTelemetry();
      let prevented = false;
      telemetry.emit('unhandledrejection', {
        reason: {message: entry.message, stack: entry.stack},
        preventDefault() { prevented = true; },
      });
      assert.equal(telemetry.requests.length, entry.ignored ? 0 : 1);
      if (!entry.ignored) assert.equal(telemetry.requests[0].report.kind, 'unhandledrejection');
      assert.equal(prevented, false, 'browser console handling must remain unchanged');
    });
  }
}

test('filtered browser errors do not consume the five-report budget', () => {
  const telemetry = installTelemetry();
  for (let index = 0; index < 12; index += 1) {
    telemetry.emit('error', errorEvent("Can't find variable: __firefox__", '', '', index));
    telemetry.emit('unhandledrejection', {reason: {message: 'DarkReader is not defined'}});
  }
  for (let index = 0; index < 6; index += 1) {
    telemetry.emit('error', errorEvent('TDeck failure ' + index, '/static/app.js'));
  }
  assert.equal(telemetry.requests.length, 5);
  assert.deepEqual(telemetry.requests.map(request => request.report.message), [
    'TDeck failure 0', 'TDeck failure 1', 'TDeck failure 2', 'TDeck failure 3', 'TDeck failure 4',
  ]);
});

test('filtered browser errors do not consume the dedupe entry for a later TDeck stack', () => {
  const telemetry = installTelemetry();
  const message = 'DarkReader is not defined';
  telemetry.emit('error', errorEvent(message));
  telemetry.emit('error', errorEvent(message, '', 'at init (https://tdeck.example/static/app.js:10:2)'));
  assert.equal(telemetry.requests.length, 1);
  assert.match(telemetry.requests[0].report.stack, /\/static\/app\.js/);
});

test('normal duplicate suppression and rate-window recovery remain intact', () => {
  const telemetry = installTelemetry();
  telemetry.emit('error', errorEvent('First TDeck failure', '/static/app.js'));
  telemetry.emit('error', errorEvent('First TDeck failure', '/static/app.js'));
  assert.equal(telemetry.requests.length, 1);
  for (let index = 0; index < 5; index += 1) {
    telemetry.emit('error', errorEvent('More TDeck failures ' + index, '/static/app.js'));
  }
  assert.equal(telemetry.requests.length, 5);
  telemetry.advanceTime(60000);
  telemetry.emit('error', errorEvent('First TDeck failure', '/static/app.js'));
  assert.equal(telemetry.requests.length, 6);
});

test('missing error filenames remain empty and event-only messages are classified', () => {
  const telemetry = installTelemetry();
  telemetry.emit('error', {message: "Can't find variable: __firefox__"});
  telemetry.emit('error', {message: 'Script error.'});
  assert.equal(telemetry.requests.length, 1);
  assert.equal(telemetry.requests[0].report.source, '');
  assert.equal(telemetry.requests[0].report.route, '/config/tvs');
});

test('TDeck source URLs retain matching errors after removing query and fragment', () => {
  const telemetry = installTelemetry();
  telemetry.emit('error', errorEvent('DarkReader is not defined',
    'https://tdeck.example/static/app.js?token=private#details'));
  assert.equal(telemetry.requests.length, 1);
  assert.equal(telemetry.requests[0].report.source, '/static/app.js');
});

test('reports retain sanitized diagnostic fields and authenticated request options', () => {
  const telemetry = installTelemetry();
  telemetry.emit('error', errorEvent(
    'Failed password=hunter2 Bearer abc.def token=private at https://tdeck.example/config?secret=private',
    'https://tdeck.example/static/app.js?session=private#details',
    'at run (https://tdeck.example/static/app.js?token=private)',
    12
  ));
  const {endpoint, init, report} = telemetry.requests[0];
  assert.equal(endpoint, '/api/client-errors');
  assert.equal(init.method, 'POST');
  assert.equal(init.credentials, 'same-origin');
  assert.equal(init.cache, 'no-store');
  assert.equal(init.keepalive, true);
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.equal(init.headers['X-CSRF-Token'], 'test-csrf');
  assert.deepEqual(Object.keys(report).sort(), [
    'buildId', 'column', 'correlationId', 'kind', 'line', 'message', 'route', 'source', 'stack',
  ]);
  assert.equal(report.kind, 'error');
  assert.equal(report.source, '/static/app.js');
  assert.equal(report.route, '/config/tvs');
  assert.equal(report.buildId, 'test-build');
  assert.equal(report.line, 12);
  assert.equal(report.column, 2);
  assert.match(report.correlationId, /^client-/);
  assert.match(report.message, /password=\[redacted\]/);
  assert.match(report.message, /Bearer \[redacted\]/);
  assert.match(report.message, /token=\[redacted\]/);
  assert.doesNotMatch(JSON.stringify(report), /hunter2|abc\.def|private|[?#]/);
});

test('report size limits and non-Error rejection privacy remain intact', () => {
  const telemetry = installTelemetry();
  telemetry.emit('error', errorEvent('x'.repeat(800), '', 's'.repeat(3000)));
  telemetry.emit('unhandledrejection', {reason: {password: 'private'}});
  assert.equal(telemetry.requests[0].report.message.length, 600);
  assert.equal(telemetry.requests[0].report.stack.length, 2400);
  assert.equal(telemetry.requests[1].report.message, '[non-Error promise rejection]');
  assert.doesNotMatch(JSON.stringify(telemetry.requests[1].report), /private|password/);
});

test('telemetry transport failures do not produce recursive reports', async () => {
  for (const options of [{fetchError: true}, {fetchReject: true}]) {
    const telemetry = installTelemetry(options);
    assert.doesNotThrow(() => telemetry.emit('error', errorEvent('TDeck failure', '/static/app.js')));
    await Promise.resolve();
    assert.equal(telemetry.requests.length, 1);
  }
});
