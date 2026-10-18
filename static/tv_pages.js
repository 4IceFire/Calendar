function initializeTvPages() {
  const root = document.getElementById('tv-pages');
  if (!root) return;
  const diagnostic = root.dataset.mode === 'diagnostics';
  const el = function(id) { return document.getElementById(id); };
  let snapshot = {rooms: [], tvs: []};
  let roomId = '', tvId = '', busy = false, active = true, timer = null, inFlight = false, signature = '';
  let pendingRead = null;
  function node(tag, text, cls) { const n = document.createElement(tag); n.textContent = text || ''; if (cls) n.className = cls; return n; }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function chosen() { return snapshot.tvs.find(function(tv) { return tv.id === tvId; }); }
  function age(field) { return !field || !field.sampledAt ? 'Never observed' : (field.stale ? 'Stale · ' : '') + new Date(field.sampledAt * 1000).toLocaleTimeString(); }
  function observation(tv, key) { return tv.observation && tv.observation.fields && tv.observation.fields[key] || {}; }
  function value(tv, key) {
    const field = observation(tv, key);
    if (key === 'volume' && tv.observation && tv.observation.audio === 'external-unverified') return 'External audio — volume unavailable/unverified';
    if (key === 'volume' && (field.value === null || field.value === undefined || field.stale)) return 'Volume unavailable / unverified';
    return field.value === null || field.value === undefined || field.value === '' ? 'Unknown' : String(field.value);
  }
  function status(tv) { return tv.expectedOff ? 'Off requested · not physical confirmation' : (tv.connected ? 'Connected' : 'Offline / unknown'); }
  function errors(tv, box) {
    clear(box);
    if (tv.readError) box.appendChild(node('p', 'Active: ' + tv.readError, 'alert alert-danger py-2 mb-2'));
    const failures = tv.observation && tv.observation.failures || [];
    failures.forEach(function(failure) {
      box.appendChild(node('p', (failure.operation === 'status_read' ? 'Read failure history: ' : 'Command failure: ') + failure.operation + ' · ' + new Date(failure.time * 1000).toLocaleString() + ' — ' + failure.summary, 'tv-failure'));
    });
  }
  function readings(tv) {
    const box = node('div', '', 'tv-readings');
    ['power', 'volume', 'source'].forEach(function(key) {
      const cell = node('div', '', 'tv-reading');
      cell.appendChild(node('span', key === 'source' ? 'Input' : key.charAt(0).toUpperCase() + key.slice(1), 'text-muted small'));
      cell.appendChild(node('strong', value(tv, key)));
      cell.appendChild(node('small', age(observation(tv, key)), 'text-muted'));
      box.appendChild(cell);
    });
    return box;
  }
  function renderOverview() {
    const box = el('tv-overview'); clear(box);
    snapshot.rooms.forEach(function(room) {
      const section = node('section', '', 'tv-room-section');
      section.appendChild(node('h2', room.name, 'h5'));
      room.tvIds.forEach(function(id) {
        const tv = snapshot.tvs.find(function(item) { return item.id === id; });
        if (!tv) return;
        const row = node('article', '', 'tv-overview-row');
        const title = node('div'); title.appendChild(node('h3', tv.name, 'h6 mb-1')); title.appendChild(node('small', status(tv), 'text-muted'));
        row.appendChild(title); row.appendChild(readings(tv));
        const failure = node('div', '', 'tv-error-list'); errors(tv, failure); row.appendChild(failure);
        const command = tv.observation && tv.observation.command;
        if (command) row.appendChild(node('small', 'Last request: ' + command.operation + ' · ' + command.status, 'tv-last-command'));
        section.appendChild(row);
      });
      box.appendChild(section);
    });
    if (!snapshot.tvs.length) box.appendChild(node('p', 'No TVs are assigned to your permitted rooms. Ask an administrator for room and TV access.'));
    box.setAttribute('aria-busy', 'false');
  }
  function options(select, entries, value, placeholder) {
    clear(select); const blank = node('option', placeholder); blank.value = ''; select.appendChild(blank);
    entries.forEach(function(item) { const option = node('option', item.name); option.value = item.id; select.appendChild(option); }); select.value = value;
  }
  function renderRemote() {
    const tv = chosen(); el('tv-remote').hidden = !tv;
    if (!tv) return;
    el('tv-name').textContent = tv.name;
    clear(el('tv-observed')); el('tv-observed').appendChild(node('p', status(tv), 'text-muted')); el('tv-observed').appendChild(readings(tv));
    errors(tv, el('tv-failures'));
    const freshVolume = observation(tv, 'volume');
    el('tv-volume-set').disabled = busy || freshVolume.stale || typeof freshVolume.value !== 'number';
    el('tv-volume-level').disabled = el('tv-volume-set').disabled;
    if (el('tv-source').dataset.tv !== tv.id) {
      options(el('tv-source'), tv.sources || [], '', 'Choose an input'); el('tv-source').dataset.tv = tv.id;
    }
    const command = tv.observation && tv.observation.command;
    el('tv-command').textContent = command ? 'Requested ' + command.operation.replace(/_/g, ' ') + (command.requested !== null ? ' ' + command.requested : '') + ' · ' + command.status + (command.status === 'pending' ? ' — awaiting fresh TV feedback' : '') : 'No command requested in this session.';
    el('tv-media').hidden = !tv.mediaMapping;
    if (tv.mediaMapping) {
      el('tv-media').href = '/tv-controls/' + encodeURIComponent(tv.id) + '/media';
      el('tv-media-help').textContent = 'Feed input: ' + tv.mediaMapping.source + '. Selecting media does not change TV power or input. Shared-player changes require confirmation.';
    } else el('tv-media-help').textContent = 'No media mapping. Ask an administrator to configure this TV’s VideoHub output and feed input in TV Setup.';
  }
  function render() {
    if (diagnostic) { renderOverview(); return; }
    const next = JSON.stringify(snapshot.rooms);
    if (next !== signature) {
      signature = next;
      if (!snapshot.rooms.some(function(room) { return room.id === roomId; })) roomId = '';
      options(el('tv-room'), snapshot.rooms, roomId, 'Choose a room'); updateTVs();
    }
    if (!chosen()) tvId = '';
    renderRemote();
  }
  function updateTVs() {
    const room = snapshot.rooms.find(function(item) { return item.id === roomId; });
    const tvs = room ? snapshot.tvs.filter(function(tv) { return room.tvIds.indexOf(tv.id) !== -1; }) : [];
    if (!tvs.some(function(tv) { return tv.id === tvId; })) tvId = '';
    options(el('tv-choice'), tvs, tvId, 'Choose a TV'); renderRemote();
  }
  async function api(path, body) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    if (!body) pendingRead = controller;
    const timeout = controller ? setTimeout(function() { controller.abort(); }, 10000) : null;
    try {
      const response = await fetch(path, {method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller ? controller.signal : undefined,
        headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
      const data = await response.json(); if (!response.ok || data.ok === false) throw new Error(data.error || 'TV request failed'); return data;
    } finally { if (timeout) clearTimeout(timeout); if (pendingRead === controller) pendingRead = null; }
  }
  async function poll() {
    if (inFlight || !active || document.hidden) return;
    inFlight = true;
    try { snapshot = await api('/api/tv-pages/state?mode=' + root.dataset.mode); render(); el('tv-message').textContent = ''; }
    catch (_error) { if (active && !document.hidden) el('tv-message').textContent = 'Could not refresh TV feedback. Displayed values may be stale. Refresh or check your access.'; }
    finally { inFlight = false; if (active && !document.hidden) timer = setTimeout(poll, 1000); }
  }
  async function command(action, body) {
    if (busy || !chosen()) return;
    const target = tvId; busy = true;
    root.querySelectorAll('#tv-remote button, #tv-room, #tv-choice').forEach(function(n) { n.disabled = true; });
    el('tv-command').textContent = 'Sending request — not yet confirmed';
    try { await api('/api/tvs/' + encodeURIComponent(target) + '/' + action, body); }
    catch (error) { el('tv-command').textContent = error.message + ' — do not automatically retry an uncertain request.'; }
    finally {
      busy = false; root.querySelectorAll('#tv-remote button, #tv-room, #tv-choice').forEach(function(n) { n.disabled = false; });
      if (timer) clearTimeout(timer); if (!inFlight) poll();
    }
  }
  if (!diagnostic) {
    el('tv-room').addEventListener('change', function() { roomId = this.value; updateTVs(); });
    el('tv-choice').addEventListener('change', function() { tvId = this.value; renderRemote(); });
    el('tv-power-on').addEventListener('click', function() { command('power', {state: 'on'}); });
    el('tv-power-off').addEventListener('click', function() { command('power', {state: 'off'}); });
    el('tv-volume-up').addEventListener('click', function() { command('volume', {action: 'up'}); });
    el('tv-volume-down').addEventListener('click', function() { command('volume', {action: 'down'}); });
    el('tv-volume-form').addEventListener('submit', function(event) { event.preventDefault(); if (el('tv-volume-level').value !== '') command('volume', {level: Number(el('tv-volume-level').value)}); });
    el('tv-source-form').addEventListener('submit', function(event) { event.preventDefault(); if (el('tv-source').value) command('source', {source: el('tv-source').value}); });
  }
  document.addEventListener('visibilitychange', function() { if (timer) clearTimeout(timer); if (document.hidden && pendingRead) pendingRead.abort(); if (!document.hidden) poll(); });
  // WebKit can reject a pending read during navigation policy checks,
  // before pagehide fires. Cancel reads early; never abort/replay commands.
  window.addEventListener('beforeunload', function() { if (pendingRead) pendingRead.abort(); });
  window.addEventListener('pagehide', function() { active = false; if (timer) clearTimeout(timer); if (pendingRead) pendingRead.abort(); });
  window.addEventListener('pageshow', function() { if (!active) { active = true; poll(); } });
  poll();
}
initializeTvPages();
