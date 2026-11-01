function initializeTvPages() {
  const root = document.getElementById('tv-pages');
  if (!root) return;
  const diagnostic = root.dataset.mode === 'diagnostics';
  const el = function(id) { return document.getElementById(id); };
  let snapshot = {rooms: [], tvs: []};
  let roomId = '', tvId = '', busy = false, active = true, timer = null, inFlight = false, signature = '';
  let pendingRead = null, localFailure = null;
  let initialTv = root.dataset.selectedTv || '';
  let volumeDraft = null, volumeEditing = false, volumeGestureTv = '';
  const view = window.TvRoomView;
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
      const field = observation(tv, key);
      const staleLabel = key !== 'volume' && field.stale && field.value !== null && field.value !== undefined && field.value !== '' ? ' · stale' : '';
      cell.appendChild(node('strong', value(tv, key) + staleLabel));
      cell.appendChild(node('small', age(observation(tv, key)), 'text-muted'));
      box.appendChild(cell);
    });
    return box;
  }
  function renderOverview() {
    const box = el('tv-overview');
    const opened = {}, pictures = {};
    box.querySelectorAll('details[data-room]').forEach(function(n) { opened[n.dataset.room] = n.open; });
    box.querySelectorAll('.tv-room-section').forEach(function(section) { pictures[section.dataset.roomId] = section.querySelector('.tv-room-picture'); });
    clear(box);
    snapshot.rooms.forEach(function(room) {
      const section = node('section', '', 'tv-room-section'); section.dataset.roomId = room.id;
      section.appendChild(node('h2', room.name, 'h5'));
      const layout = room.layout || {positions: {}};
      const url = layout.backgroundUrl || '';
      const previous = pictures[room.id];
      const picture = previous && previous.dataset.backgroundUrl === url ? previous : view.canvas(url, room.name);
      picture.dataset.backgroundUrl = url;
      picture.querySelectorAll('.tv-map-tv').forEach(function(item) { item.parentNode.removeChild(item); });
      const image = picture.querySelector('img'); if (image) image.alt = room.name + ' room picture';
      const unplaced = node('div', '', 'tv-unplaced');
      room.tvIds.forEach(function(id) {
        const tv = snapshot.tvs.find(function(item) { return item.id === id; });
        if (!tv) return;
        const power = observation(tv, 'power');
        const known = !power.stale && (power.value === 'on' || power.value === 'off');
        const row = node(tv.canControl ? 'a' : 'article', '', 'tv-map-tv tv-power-' + (known ? power.value : 'unknown'));
        if (tv.canControl) { row.href = '/tv-controls?tv=' + encodeURIComponent(tv.id) + '&from=diagnostics'; row.setAttribute('aria-label', 'Control ' + tv.name); }
        row.dataset.tvId = tv.id;
        const screen = node('div', '', 'tv-map-screen'); screen.appendChild(view.icon('tv')); row.appendChild(screen);
        row.appendChild(node('h3', tv.name, 'tv-map-name'));
        row.appendChild(node('span', known ? (power.value === 'on' ? '● On' : '○ Off') : (power.stale && power.value ? '◌ Stale power' : '◌ Power unknown'), 'tv-map-power'));
        const volume = observation(tv, 'volume');
        const fresh = !volume.stale && typeof volume.value === 'number' && tv.observation.audio !== 'external-unverified';
        const meter = node('div', '', 'tv-volume-bar');
        const fill = node('span'); fill.style.width = fresh ? Math.max(0, Math.min(100, volume.value)) + '%' : '0%'; meter.appendChild(fill); meter.setAttribute('aria-hidden', 'true'); row.appendChild(meter);
        row.appendChild(node('span', fresh ? 'Volume ' + volume.value : value(tv, 'volume'), 'tv-map-volume'));
        const source = observation(tv, 'source');
        row.appendChild(node('span', 'Input: ' + value(tv, 'source') + (source.stale ? ' · stale' : ''), 'tv-map-source'));
        row.title = tv.name + ' · Power: ' + age(power) + ' · Volume: ' + age(volume) + ' · Input: ' + age(source);
        if (layout.positions && layout.positions[id]) { row.classList.add('tv-positioned'); view.place(row, layout.positions[id]); picture.appendChild(row); }
        else unplaced.appendChild(row);
      });
      section.appendChild(picture);
      if (unplaced.childNodes.length) { section.appendChild(node('p', 'Unplaced TVs', 'tv-unplaced-label text-muted small')); section.appendChild(unplaced); }
      // Keep detailed failures available without covering the room picture.
      const details = node('details', '', 'tv-room-feedback'); details.dataset.room = room.id; details.open = !!opened[room.id]; details.appendChild(node('summary', 'Feedback details'));
      room.tvIds.forEach(function(id) {
        const tv = snapshot.tvs.find(function(item) { return item.id === id; }); if (!tv) return;
        const failure = node('div'); errors(tv, failure);
        const command = tv.observation && tv.observation.command;
        if (failure.childNodes.length || command) {
          details.appendChild(node('strong', tv.name)); details.appendChild(failure);
          if (command) details.appendChild(node('p', 'Last request: ' + command.operation + ' · ' + command.status, 'small'));
        }
      });
      section.appendChild(details); box.appendChild(section);
    });
    if (!snapshot.tvs.length) box.appendChild(node('p', 'No TVs are assigned to your permitted rooms. Ask an administrator for room and TV access.'));
    box.setAttribute('aria-busy', 'false');
  }
  function tile(label, kind, action) {
    const button = node('button', '', 'tv-tile'); button.type = 'button';
    button.appendChild(view.icon(kind)); button.appendChild(node('span', label));
    button.addEventListener('click', action); return button;
  }
  function renderSelection() {
    const room = snapshot.rooms.find(function(item) { return item.id === roomId; });
    if (!room) { roomId = ''; tvId = ''; }
    if (room && room.tvIds.indexOf(tvId) === -1) tvId = '';
    root.dataset.step = tvId ? 'remote' : (roomId ? 'tvs' : 'rooms');
    const main = root.closest('main'); if (main) main.classList.toggle('tv-controls-active', !!tvId);
    el('tv-selection-back').hidden = !roomId; el('tv-room-tiles').hidden = !!roomId; el('tv-choice-tiles').hidden = !roomId;
    el('tv-selection-title').textContent = room ? room.name : 'Rooms';
    const next = JSON.stringify([snapshot.rooms, snapshot.tvs.map(function(tv) { return [tv.id, tv.name]; }), roomId, tvId]);
    if (signature === next) return; signature = next;
    clear(el('tv-room-tiles')); clear(el('tv-choice-tiles'));
    snapshot.rooms.forEach(function(item) {
      const button = tile(item.name, 'room', function() { if (!busy) { roomId = item.id; tvId = ''; render(); el('tv-selection-back').focus(); } });
      button.dataset.room = item.id; el('tv-room-tiles').appendChild(button);
    });
    if (room) room.tvIds.forEach(function(id) {
      const tv = snapshot.tvs.find(function(item) { return item.id === id; }); if (!tv) return;
      const button = tile(tv.name, 'tv', function() { if (!busy) { tvId = id; render(); el('tv-back').focus(); } });
      button.dataset.tv = id; button.setAttribute('aria-pressed', String(tvId === id)); el('tv-choice-tiles').appendChild(button);
    });
    if (!snapshot.rooms.length) el('tv-room-tiles').appendChild(node('p', 'No permitted rooms. Ask an administrator for room and TV access.'));
    el('tv-room-tiles').setAttribute('aria-busy', 'false');
  }
  function renderRemote() {
    const tv = chosen(); el('tv-remote').setAttribute('aria-disabled', String(!tv));
    el('tv-remote-controls').disabled = !tv || busy; el('tv-back').disabled = !tv || busy;
    if (!tv) {
      localFailure = null; volumeDraft = null; volumeEditing = false;
      el('tv-name').textContent = 'Choose a TV'; el('tv-source-tiles').dataset.signature = ''; clear(el('tv-source-tiles')); clear(el('tv-observed')); clear(el('tv-observed-details')); clear(el('tv-failures'));
      el('tv-command').textContent = ''; el('tv-media').hidden = true; el('tv-media').removeAttribute('href');
      el('tv-media-unavailable').hidden = false; el('tv-media-unavailable').textContent = 'Select a TV to choose media.';
      el('tv-volume-level').disabled = true; el('tv-volume-value').textContent = 'Unavailable';
      el('tv-media-help').textContent = 'Select a room and TV to enable the remote.'; return;
    }
    el('tv-name').textContent = tv.name;
    clear(el('tv-observed')); el('tv-observed').appendChild(node('p', status(tv), 'text-muted small mb-2')); el('tv-observed').appendChild(readings(tv));
    el('tv-observed').dataset.audio = tv.observation && tv.observation.audio || '';
    clear(el('tv-observed-details')); el('tv-observed-details').appendChild(readings(tv));
    errors(tv, el('tv-failures'));
    if (tv.readError || (tv.observation && tv.observation.command && tv.observation.command.status === 'failed')) el('tv-feedback-details').open = true;
    const freshVolume = observation(tv, 'volume');
    const slider = el('tv-volume-level');
    const volumeAvailable = !freshVolume.stale && typeof freshVolume.value === 'number' && tv.observation.audio !== 'external-unverified';
    slider.disabled = busy || !volumeAvailable;
    if (slider.dataset.volumeTv !== tv.id) { volumeDraft = null; volumeEditing = false; slider.dataset.volumeTv = tv.id; }
    if (!volumeAvailable) { volumeDraft = null; volumeEditing = false; }
    if (volumeDraft && volumeDraft.tv === tv.id && !volumeEditing && !busy && volumeAvailable && freshVolume.value === volumeDraft.value) volumeDraft = null;
    if (volumeAvailable && !volumeEditing && !volumeDraft) slider.value = String(freshVolume.value);
    el('tv-volume-value').textContent = !volumeAvailable ? 'Unavailable' : (volumeDraft ? volumeDraft.value + ' requested' : String(freshVolume.value));
    slider.setAttribute('aria-valuetext', !volumeAvailable ? 'Volume unavailable or unverified' : (volumeDraft ? 'Requested ' + volumeDraft.value + '; reported ' + freshVolume.value : 'Reported volume ' + freshVolume.value));
    const sources = el('tv-source-tiles');
    const sourceSignature = JSON.stringify([tv.id, tv.sources]);
    if (sources.dataset.signature !== sourceSignature) {
      clear(sources); sources.dataset.signature = sourceSignature;
      (tv.sources || []).forEach(function(source) {
        const button = tile(source.name || source.id, /hdmi/i.test(source.id + source.name) ? 'hdmi' : 'source', function() { command('source', {source: source.id}); });
        button.dataset.source = source.id; sources.appendChild(button);
      });
      if (!sources.childNodes.length) sources.appendChild(node('p', 'Inputs unavailable. Check TV feedback.', 'small text-muted'));
    }
    sources.querySelectorAll('button').forEach(function(button) { button.setAttribute('aria-pressed', String(!observation(tv, 'source').stale && observation(tv, 'source').value === button.dataset.source)); });
    const commandState = tv.observation && tv.observation.command;
    if (!busy) el('tv-command').textContent = commandState ? 'Requested ' + commandState.operation.replace(/_/g, ' ') + (commandState.requested !== null ? ' ' + commandState.requested : '') + ' · ' + commandState.status + (commandState.status === 'pending' ? ' — awaiting fresh TV feedback' : '') : '';
    if (!busy && localFailure && localFailure.tv === tv.id) el('tv-command').textContent = localFailure.message;
    el('tv-media').hidden = !tv.mediaMapping;
    el('tv-media-unavailable').hidden = !!tv.mediaMapping;
    el('tv-media-unavailable').textContent = 'Media not configured for this TV.';
    if (tv.mediaMapping) {
      el('tv-media').href = '/tv-controls/' + encodeURIComponent(tv.id) + '/media';
      el('tv-media-help').textContent = 'Feed input: ' + tv.mediaMapping.source + '. Media does not change TV power or input.';
    } else el('tv-media-help').textContent = 'No media mapping. Ask an administrator to configure this TV’s VideoHub output and feed input in TV Setup.';
  }
  function render() {
    if (diagnostic) { renderOverview(); return; }
    if (initialTv) {
      const room = snapshot.rooms.find(function(item) { return item.tvIds.indexOf(initialTv) !== -1; });
      if (room) { roomId = room.id; tvId = initialTv; }
      initialTv = '';
    }
    if (!chosen()) tvId = '';
    renderSelection(); renderRemote();
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
    catch (_error) {
      if (active && !document.hidden) {
        snapshot.tvs.forEach(function(tv) { const fields = tv.observation && tv.observation.fields || {}; Object.keys(fields).forEach(function(key) { fields[key].stale = true; }); });
        render();
        el('tv-message').textContent = 'Could not refresh TV feedback. Displayed values may be stale. Refresh or check your access.';
      }
    }
    finally { inFlight = false; if (active && !document.hidden) timer = setTimeout(poll, 1000); }
  }
  async function command(action, body) {
    if (busy || !chosen()) return;
    const target = tvId; busy = true; localFailure = null;
    if (action === 'volume' && body.level === undefined) { volumeDraft = null; volumeEditing = false; }
    el('tv-remote-controls').disabled = true; el('tv-back').disabled = true;
    root.querySelectorAll('#tv-selection button').forEach(function(n) { n.disabled = true; });
    el('tv-command').textContent = 'Sending request — not yet confirmed';
    try { await api('/api/tvs/' + encodeURIComponent(target) + '/' + action, body); }
    catch (error) { localFailure = {tv: target, message: error.message + ' — do not automatically retry an uncertain request.'}; el('tv-command').textContent = localFailure.message; }
    finally {
      busy = false; root.querySelectorAll('#tv-selection button').forEach(function(n) { n.disabled = false; }); renderRemote();
      if (timer) clearTimeout(timer); if (!inFlight) poll();
    }
  }
  if (!diagnostic) {
    root.querySelectorAll('[data-icon]').forEach(function(n) { n.appendChild(view.icon(n.dataset.icon)); });
    el('tv-back').addEventListener('click', function() {
      if (busy) return;
      if (root.dataset.returnTo === 'diagnostics') { window.location.assign('/tv-diagnostics'); return; }
      tvId = ''; render(); el('tv-selection-back').focus();
    });
    el('tv-selection-back').addEventListener('click', function() { if (!busy) { tvId = ''; roomId = ''; render(); const first = el('tv-room-tiles').querySelector('button'); if (first) first.focus(); } });
    el('tv-power-on').addEventListener('click', function() { command('power', {state: 'on'}); });
    el('tv-power-off').addEventListener('click', function() { command('power', {state: 'off'}); });
    el('tv-volume-up').addEventListener('click', function() { command('volume', {action: 'up'}); });
    el('tv-volume-down').addEventListener('click', function() { command('volume', {action: 'down'}); });
    const slider = el('tv-volume-level');
    slider.addEventListener('pointerdown', function() { if (!slider.disabled) { volumeGestureTv = tvId; volumeEditing = true; } });
    slider.addEventListener('input', function() {
      if (slider.disabled || busy || !chosen() || (volumeGestureTv && volumeGestureTv !== tvId)) return;
      const level = Number(slider.value);
      if (!Number.isInteger(level) || level < 0 || level > 100) return;
      volumeEditing = true; volumeDraft = {tv: tvId, value: level, dirty: true};
      el('tv-volume-value').textContent = level + ' requested';
      slider.setAttribute('aria-valuetext', 'Requested volume ' + level);
    });
    slider.addEventListener('change', function() {
      volumeEditing = false;
      if (slider.disabled || busy || !volumeDraft || !volumeDraft.dirty || volumeDraft.tv !== tvId || (volumeGestureTv && volumeGestureTv !== tvId)) return;
      volumeDraft.dirty = false;
      command('volume', {level: volumeDraft.value});
    });
    // iOS/WebKit can emit input but no change for a tap on the track.
    // Commit once after release; the dirty flag deduplicates native change.
    slider.addEventListener('pointerup', function() {
      const target = volumeGestureTv;
      setTimeout(function() {
        if (target && target === tvId && volumeGestureTv === target) slider.dispatchEvent(new Event('change', {bubbles: true}));
        volumeGestureTv = '';
      }, 0);
    });
    slider.addEventListener('pointercancel', function() { volumeGestureTv = ''; volumeDraft = null; volumeEditing = false; renderRemote(); });
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
