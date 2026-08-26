(function installTDeckApiClient() {
  'use strict';

  if (window.__TDECK_API_CLIENT_INSTALLED__ || typeof window.fetch !== 'function') return;
  window.__TDECK_API_CLIENT_INSTALLED__ = true;

  var script = document.currentScript;
  var csrfToken = script ? String(script.getAttribute('data-csrf-token') || '') : '';
  var nativeFetch = window.fetch.bind(window);
  var writeMethods = {POST: true, PUT: true, PATCH: true, DELETE: true};

  window.tdeckFetch = function tdeckFetch(input, init) {
    var options = init ? Object.assign({}, init) : {};
    var requestInput = (typeof Request !== 'undefined' && input instanceof Request) ? input : null;
    var method = String(options.method || (requestInput && requestInput.method) || 'GET').toUpperCase();
    var rawUrl = requestInput ? requestInput.url : String(input || '');
    var parsed;
    try {
      parsed = new URL(rawUrl, window.location.href);
    } catch (error) {
      return nativeFetch(input, options);
    }

    var isSameOriginApi = parsed.origin === window.location.origin && parsed.pathname.indexOf('/api/') === 0;
    if (isSameOriginApi) {
      if (!options.credentials) options.credentials = 'same-origin';
      if (writeMethods[method]) {
        var sourceHeaders = options.headers || (requestInput && requestInput.headers) || {};
        var headers = new Headers(sourceHeaders);
        if (csrfToken && !headers.has('X-CSRF-Token')) {
          headers.set('X-CSRF-Token', csrfToken);
        }
        options.headers = headers;
      }
    }
    return nativeFetch(input, options);
  };

  // Keep existing pages working while they migrate to the named helper.  The
  // wrapper only changes same-origin /api writes and leaves every other fetch
  // call byte-for-byte equivalent at the network boundary.
  window.fetch = window.tdeckFetch;
})();
