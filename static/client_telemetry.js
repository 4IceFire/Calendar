(function installTDeckClientTelemetry() {
  'use strict';

  if (window.__TDECK_CLIENT_TELEMETRY_INSTALLED__) return;
  window.__TDECK_CLIENT_TELEMETRY_INSTALLED__ = true;

  var script = document.currentScript;
  if (!script) return;

  var endpoint = String(script.getAttribute('data-endpoint') || '');
  var buildId = String(script.getAttribute('data-build-id') || '');
  var csrfToken = String(script.getAttribute('data-csrf-token') || '');
  var sentAt = [];
  var recentSignatures = Object.create(null);
  var RATE_WINDOW_MS = 60000;
  var RATE_MAX = 5;
  var DEDUPE_MS = 60000;

  function stripSecrets(value, limit) {
    var text = String(value || '').replace(/\0/g, '');
    text = text.replace(/\bbearer\s+[A-Za-z0-9._~+\-/]+=*/gi, 'Bearer [redacted]');
    text = text.replace(
      /\b(password|passwd|token|secret|authorization|cookie|session|csrf)\b\s*[:=]\s*([^\s,;&]+)/gi,
      '$1=[redacted]'
    );
    text = text.replace(/((?:https?:\/\/|\/)[^\s?#]+)[?#][^\s]*/g, '$1');
    return text.slice(0, limit);
  }

  function cleanPath(value) {
    if (!value) return '';
    try {
      var parsed = new URL(String(value || ''), window.location.href);
      return stripSecrets(parsed.pathname || '/', 240);
    } catch (error) {
      return stripSecrets(String(value || '').split('?')[0].split('#')[0], 240);
    }
  }

  function correlationId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
    } catch (error) {
      // Fall through to a non-cryptographic diagnostic identifier.
    }
    return 'client-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }

  function canSend(signature) {
    var now = Date.now();
    sentAt = sentAt.filter(function keepRecent(ts) { return now - ts < RATE_WINDOW_MS; });
    Object.keys(recentSignatures).forEach(function discardOld(key) {
      if (now - recentSignatures[key] >= DEDUPE_MS) delete recentSignatures[key];
    });
    if (sentAt.length >= RATE_MAX || recentSignatures[signature]) return false;
    sentAt.push(now);
    recentSignatures[signature] = now;
    return true;
  }

  function isBrowserNoise(report) {
    // Keep errors with TDeck frames, even when their message resembles injected browser scripts.
    if (report.source.indexOf('/static/') === 0 || report.stack.indexOf('/static/') !== -1) return false;
    // Keep this narrow list in sync with the server and tests/fixtures/client_error_noise.json.
    var match = /^(?:Uncaught )?(?:(?:ReferenceError|TypeError): )?(?:Can't find variable: (?:__firefox__|DarkReader)|(?:__firefox__|DarkReader) is not defined|undefined is not an object \(evaluating '(?:window\.__firefox__\.reader|window\.ethereum\.selectedAddress\s*=\s*undefined)'\))$/.exec(report.message);
    return !!match && match[0] === report.message;
  }

  function send(report) {
    if (!endpoint || typeof window.fetch !== 'function') return;
    if (isBrowserNoise(report)) return;
    var signature = [report.kind, report.message, report.source, report.line, report.column].join('|');
    if (!canSend(signature)) return;

    var headers = {'Content-Type': 'application/json'};
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    try {
      window.fetch(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        keepalive: true,
        headers: headers,
        body: JSON.stringify(report),
      }).catch(function ignoreTelemetryFailure() {});
    } catch (error) {
      // Never report failures from the telemetry transport itself.
    }
  }

  function reportError(event) {
    var error = event && event.error;
    var message = stripSecrets((error && error.message) || (event && event.message) || 'Unknown client error', 600);
    send({
      kind: 'error',
      message: message,
      stack: stripSecrets(error && error.stack, 2400),
      source: cleanPath(event && event.filename),
      line: Math.max(0, Number(event && event.lineno) || 0),
      column: Math.max(0, Number(event && event.colno) || 0),
      route: cleanPath(window.location.pathname),
      buildId: stripSecrets(buildId, 80),
      correlationId: correlationId(),
    });
  }

  function reportUnhandledRejection(event) {
    var reason = event && event.reason;
    var isErrorLike = reason && typeof reason === 'object';
    var message = isErrorLike && reason.message
      ? reason.message
      : '[non-Error promise rejection]';
    send({
      kind: 'unhandledrejection',
      message: stripSecrets(message, 600),
      stack: stripSecrets(isErrorLike && reason.stack, 2400),
      source: '',
      line: 0,
      column: 0,
      route: cleanPath(window.location.pathname),
      buildId: stripSecrets(buildId, 80),
      correlationId: correlationId(),
    });
  }

  window.addEventListener('error', reportError);
  window.addEventListener('unhandledrejection', reportUnhandledRejection);
}());
