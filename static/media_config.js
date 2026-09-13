function initializeMediaConfigPage() {
  const root = document.getElementById('media-config-page');
  if (!root) return;

  const el = function(id) { return document.getElementById(id); };
  let permissions = {};
  try { permissions = JSON.parse(root.dataset.permissions || '{}'); } catch (_error) {}
  let items = [];
  let selectedId = '';
  let state = null;
  let stateValid = false;
  let stateInFlight = false;
  let pollTimer = null;
  let pollDelay = 2000;
  let libraryInFlight = false;
  let uploadInFlight = false;
  let editInFlight = false;
  let loadInFlight = false;
  let setupInFlight = false;
  let destinationSequence = 0;
  let stateRevision = 0;
  let libraryRevision = 0;
  let pageActive = true;

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, visible) { if (node) node.classList.toggle('d-none', !visible); }
  function text(node, value) { if (node) node.textContent = value || ''; }
  function node(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = value;
    return result;
  }
  function message(value, kind) {
    const box = el('media-message');
    box.className = 'alert alert-' + (kind || 'info') + ' mb-3';
    text(box, value);
  }
  function errorMessage(error) { return error && error.message ? error.message : 'The request could not be completed.'; }
  function selectedItem() { return items.find(function(item) { return item.id === selectedId; }); }
  function activeJob() {
    if (!state || !state.job) return false;
    return ['succeeded', 'failed', 'completed', 'complete', 'success', 'error', 'cancelled'].indexOf(state.job.status) === -1;
  }
  function formatBytes(value) {
    const bytes = Number(value) || 0;
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  async function request(path, options) {
    const settings = Object.assign({ cache: 'no-store', credentials: 'same-origin' }, options || {});
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timeout = null;
    if (controller) {
      settings.signal = controller.signal;
      // The ATEM upload runs as a background job; an HTTP call should remain short.
      timeout = setTimeout(function() { controller.abort(); }, path === '/api/media/upload' ? 60000 : 15000);
    }
    try {
      const response = await fetch(path, settings);
      const payload = await response.json().catch(function() { return {}; });
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || ('Request failed (' + response.status + '). Refresh the page if your session has expired.'));
      }
      if (response.redirected || !response.headers.get('content-type') || response.headers.get('content-type').indexOf('application/json') === -1) {
        throw new Error('Your session may have expired. Refresh the page to sign in again.');
      }
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Refresh the status before trying again.');
      throw error;
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }
  function jsonRequest(path, method, body) {
    return request(path, { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  function updateControls() {
    if (el('media-upload-button')) {
      el('media-upload-button').disabled = uploadInFlight || !permissions.upload;
      el('media-upload-file').disabled = uploadInFlight || !permissions.upload;
      el('media-upload-name').disabled = uploadInFlight || !permissions.upload;
      text(el('media-upload-button'), uploadInFlight ? 'Uploading…' : 'Upload to library');
    }
    if (el('media-save-image')) {
      const disabled = editInFlight || !permissions.manage || !selectedItem();
      ['media-save-image', 'media-delete-image', 'media-edit-name', 'media-edit-preset'].forEach(function(id) { el(id).disabled = disabled; });
    }
    if (!el('media-load-button')) return;
    const hasPlayers = !!(state && state.destinations && state.destinations.length);
    const busy = loadInFlight || activeJob();
    el('media-player').disabled = !hasPlayers || busy || !permissions.load;
    let help = 'The image is fitted to the switcher format and loaded into the chosen player.';
    if (!stateValid) help = 'Waiting for a current ATEM media status.';
    else if (!state.enabled) help = 'ATEM media uploads are disabled. Ask someone with Config access to enable them.';
    else if (!state.connected) help = 'The ATEM media connection is offline. You can still use the image library.';
    else if (state.ready === false) help = 'Waiting for the ATEM video format and media player state.';
    else if (!hasPlayers) help = 'No media players are configured. Ask someone with Config access to reserve players and still slots.';
    else if (busy) help = 'An image is loading. Wait for this job to finish before loading another.';
    else if (!selectedItem()) help = 'Select an image from the library.';
    else if (!el('media-player').value) help = 'Choose a media player.';
    text(el('media-load-help'), help);
    el('media-load-button').disabled = !permissions.load || !stateValid || !state || !state.enabled || !state.connected || state.ready === false || !hasPlayers || !selectedItem() || !el('media-player').value || busy;
    text(el('media-load-button'), busy ? 'Loading into ATEM…' : 'Load selected image');
  }

  function renderLibrary() {
    const grid = el('media-grid');
    const query = el('media-search').value.trim().toLowerCase();
    const presetsOnly = el('media-filter').value === 'presets';
    const visibleItems = items.filter(function(item) {
      return (!presetsOnly || item.preset) && String(item.name || '').toLowerCase().indexOf(query) !== -1;
    });
    clear(grid);
    text(el('media-count'), '(' + items.length + ')');
    text(el('media-library-status'), visibleItems.length ? visibleItems.length + ' image' + (visibleItems.length === 1 ? '' : 's') + '. Select one to preview.' : (items.length ? 'No images match this search or filter.' : 'Your library is empty.' + (permissions.upload ? ' Upload an image to get started.' : ' Ask someone with upload access to add images.')));
    visibleItems.forEach(function(item) {
      const tile = node('button', 'media-tile');
      tile.type = 'button';
      tile.dataset.mediaId = item.id;
      tile.setAttribute('aria-pressed', item.id === selectedId ? 'true' : 'false');
      tile.setAttribute('aria-label', 'Select ' + item.name + (item.preset ? ', preset' : ''));
      const picture = node('img', 'media-tile-image');
      picture.src = item.thumbnail_url;
      picture.alt = '';
      picture.loading = 'lazy';
      const caption = node('span', 'media-tile-caption');
      caption.appendChild(node('span', 'media-tile-name', item.name));
      caption.appendChild(node('span', 'media-tile-meta', item.width + ' × ' + item.height));
      if (item.preset) caption.appendChild(node('span', 'media-tile-preset', 'Preset'));
      tile.appendChild(picture);
      tile.appendChild(caption);
      tile.addEventListener('click', function() { selectImage(item.id); });
      grid.appendChild(tile);
    });
    grid.setAttribute('aria-busy', 'false');
  }

  function selectImage(id) {
    if (editInFlight) return;
    selectedId = id;
    Array.prototype.forEach.call(el('media-grid').querySelectorAll('.media-tile'), function(tile) {
      tile.setAttribute('aria-pressed', tile.dataset.mediaId === id ? 'true' : 'false');
    });
    renderSelection();
  }
  function renderSelection() {
    const item = selectedItem();
    show(el('media-selection-empty'), !item);
    show(el('media-selection-content'), !!item);
    if (item) {
      el('media-preview-image').src = item.url;
      el('media-preview-image').alt = item.name;
      text(el('media-selected-name'), item.name);
      text(el('media-selected-details'), item.width + ' × ' + item.height + ' · ' + formatBytes(item.size_bytes) + (item.preset ? ' · Preset' : ''));
      if (el('media-edit-name')) {
        el('media-edit-name').value = item.name;
        el('media-edit-preset').checked = !!item.preset;
      }
    } else {
      el('media-preview-image').removeAttribute('src');
      el('media-preview-image').alt = '';
    }
    updateControls();
  }

  async function refreshLibrary() {
    if (libraryInFlight || uploadInFlight || editInFlight) return;
    libraryInFlight = true;
    const revision = libraryRevision;
    el('media-refresh-library').disabled = true;
    el('media-grid').setAttribute('aria-busy', 'true');
    try {
      const data = await request('/api/media');
      if (revision !== libraryRevision) return;
      items = data.items || [];
      if (data.permissions) permissions = data.permissions;
      if (!selectedItem()) selectedId = '';
      renderLibrary();
      renderSelection();
    } catch (error) {
      text(el('media-library-status'), 'Could not refresh the library. ' + errorMessage(error));
      el('media-grid').setAttribute('aria-busy', 'false');
    } finally {
      libraryInFlight = false;
      el('media-grid').setAttribute('aria-busy', 'false');
      el('media-refresh-library').disabled = false;
    }
  }

  function renderPlayerDetail() {
    if (!el('media-player-detail')) return;
    const player = Number(el('media-player').value);
    const current = state && (state.players || []).find(function(item) { return Number(item.player) === player; });
    if (!current) { text(el('media-player-detail'), ''); return; }
    const still = (state.stills || []).find(function(item) { return Number(item.slot) === Number(current.slot); });
    const isStill = current.type === 'still' || current.type === 1;
    text(el('media-player-detail'), isStill && current.slot ? 'Current source: still slot ' + current.slot + (still && still.name ? ' · ' + still.name : '') : (current.type ? 'Current source: ' + String(current.type) : 'Current source unavailable.'));
  }

  function renderState() {
    const status = el('media-connection-status');
    let label = 'ATEM media offline';
    let detail = state.error || 'Your image library is still available.';
    if (!state.enabled) {
      label = 'ATEM media uploads disabled';
      detail = 'Your image library is available. Enable uploads in ATEM media setup when the reserved players and slots are ready.';
    } else if (state.connected) {
      label = 'ATEM media connected';
      const mode = state.videoMode || {};
      detail = (state.product || 'ATEM') + (mode.name ? ' · ' + mode.name : '') + (mode.width && mode.height ? ' · ' + mode.width + ' × ' + mode.height : '');
      if (state.error) detail += ' · ' + state.error;
    }
    status.classList.toggle('text-success', !!state.connected && !!state.enabled);
    text(status, label);
    text(el('media-connection-detail'), detail);
    const capabilities = state.capabilities || {};
    text(el('media-setup-capabilities'), capabilities.players && capabilities.stills ? 'Connected switcher: ' + capabilities.players + ' media players, ' + capabilities.stills + ' still slots. Player and port numbers start at 1.' : 'Switcher capabilities will appear when connected. Player and port numbers start at 1.');
    if (el('media-player')) {
      const select = el('media-player');
      const destinations = state.destinations || [];
      const signature = JSON.stringify(destinations.map(function(item) { return [item.player, item.label]; }));
      if (select.dataset.signature !== signature) {
        const previous = select.value;
        clear(select);
        if (!destinations.length) {
          const emptyOption = node('option', '', 'No media players configured');
          emptyOption.value = '';
          select.appendChild(emptyOption);
        }
        destinations.forEach(function(item) {
          const option = node('option', '', item.label ? item.label + ' (Player ' + item.player + ')' : 'Media Player ' + item.player);
          option.value = String(item.player);
          select.appendChild(option);
        });
        if (destinations.some(function(item) { return String(item.player) === previous; })) select.value = previous;
        select.dataset.signature = signature;
      }
      renderPlayerDetail();
    }
    renderJob();
    updateControls();
  }

  function renderJob() {
    const box = el('media-job');
    if (!box) return;
    const job = state && state.job;
    show(box, !!job);
    if (!job) return;
    const labels = { queued: 'Queued', preparing: 'Preparing image', uploading: 'Uploading image', selecting: 'Selecting image in player' };
    let kind = 'info';
    let copy = (labels[job.status] || 'Loading image') + ': ' + (job.mediaName || 'Selected image') + ' → Media Player ' + job.player + (job.slot ? ', still slot ' + job.slot : '') + '.';
    if (['succeeded', 'completed', 'complete', 'success'].indexOf(job.status) !== -1) {
      kind = 'success';
      copy = (job.mediaName || 'Image') + ' loaded into Media Player ' + job.player + (job.slot ? ' using still slot ' + job.slot : '') + '.';
    } else if (job.status === 'failed' || job.status === 'error') {
      kind = 'danger';
      copy = 'Could not load ' + (job.mediaName || 'the image') + ' into Media Player ' + job.player + '. ' + (job.error || 'Refresh the status and try again.');
    } else if (job.status === 'cancelled') {
      kind = 'warning';
      copy = 'The media load was cancelled.';
    }
    box.className = 'alert alert-' + kind + ' mt-3 mb-0';
    if (box.textContent !== copy) text(box, copy);
  }

  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (pageActive && !document.hidden) pollTimer = setTimeout(refreshState, pollDelay);
  }
  async function refreshState() {
    if (stateInFlight || document.hidden || !pageActive) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    stateInFlight = true;
    const revision = stateRevision;
    el('media-refresh-state').disabled = true;
    try {
      const data = await request('/api/atem/media/state');
      // A refresh started before a write must not replace its newer job/setup state.
      if (revision !== stateRevision) return;
      state = data;
      stateValid = true;
      pollDelay = 2000;
      renderState();
    } catch (error) {
      stateValid = false;
      pollDelay = Math.min(pollDelay * 2, 30000);
      text(el('media-connection-status'), 'ATEM media status unavailable');
      el('media-connection-status').classList.remove('text-success');
      text(el('media-connection-detail'), errorMessage(error));
      updateControls();
    } finally {
      stateInFlight = false;
      el('media-refresh-state').disabled = false;
      schedulePoll();
    }
  }

  async function uploadImage(event) {
    event.preventDefault();
    if (uploadInFlight || !permissions.upload) return;
    const file = el('media-upload-file').files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    form.append('name', el('media-upload-name').value.trim());
    uploadInFlight = true;
    updateControls();
    try {
      const data = await request('/api/media/upload', { method: 'POST', body: form });
      libraryRevision += 1;
      if (data.item) {
        items.unshift(data.item);
        selectedId = data.item.id;
      }
      el('media-upload-form').reset();
      el('media-search').value = '';
      el('media-filter').value = 'all';
      renderLibrary();
      renderSelection();
      message('Image added to the library. You can edit its name or save it as a preset.', 'success');
    } catch (error) {
      message('Upload failed. ' + errorMessage(error), 'danger');
    } finally {
      uploadInFlight = false;
      updateControls();
    }
  }
  async function saveImage(event) {
    event.preventDefault();
    const item = selectedItem();
    if (!item || editInFlight || !permissions.manage) return;
    const name = el('media-edit-name').value.trim();
    if (!name) { message('Enter a name for the image.', 'warning'); return; }
    const body = { name: name, preset: el('media-edit-preset').checked };
    editInFlight = true;
    updateControls();
    try {
      const data = await jsonRequest('/api/media/' + encodeURIComponent(item.id), 'PATCH', body);
      libraryRevision += 1;
      items = items.map(function(current) { return current.id === item.id ? data.item : current; });
      renderLibrary();
      renderSelection();
      message('Image details saved.', 'success');
    } catch (error) {
      message('Could not save image details. ' + errorMessage(error), 'danger');
    } finally {
      editInFlight = false;
      updateControls();
    }
  }
  async function deleteImage() {
    const item = selectedItem();
    if (!item || editInFlight || !permissions.manage) return;
    if (!window.confirm('Delete “' + item.name + '” from the TDeck library? This does not clear any image already loaded into ATEM.')) return;
    editInFlight = true;
    updateControls();
    try {
      await request('/api/media/' + encodeURIComponent(item.id), { method: 'DELETE' });
      libraryRevision += 1;
      items = items.filter(function(current) { return current.id !== item.id; });
      selectedId = '';
      renderLibrary();
      renderSelection();
      message('Image deleted from the library.', 'success');
    } catch (error) {
      message('Could not delete the image. ' + errorMessage(error), 'danger');
    } finally {
      editInFlight = false;
      updateControls();
    }
  }
  async function loadImage() {
    const item = selectedItem();
    if (!item || el('media-load-button').disabled || loadInFlight) return;
    loadInFlight = true;
    updateControls();
    try {
      const data = await jsonRequest('/api/atem/media/load', 'POST', { media_id: item.id, player: Number(el('media-player').value) });
      stateRevision += 1;
      if (data.job) state.job = data.job;
      renderJob();
      show(el('media-message'), false);
    } catch (error) {
      message('Could not start loading the image. ' + errorMessage(error), 'danger');
      // The server may have accepted a job even if the response was lost.
      stateRevision += 1;
      stateValid = false;
    } finally {
      loadInFlight = false;
      updateControls();
      refreshState();
    }
  }

  function addDestination(destination) {
    destinationSequence += 1;
    const row = node('div', 'media-destination');
    const fields = node('div', 'media-destination-fields');
    const values = destination || {};
    const definitions = [
      { key: 'player', label: 'Media player number', type: 'number', value: values.player || '' },
      { key: 'label', label: 'Display name', type: 'text', value: values.label || '' },
      { key: 'slots', label: 'Reserved still slots', type: 'text', value: (values.slots || []).join(', ') },
      { key: 'videohub_input', label: 'VideoHub input', type: 'number', value: values.videohub_input || '' }
    ];
    definitions.forEach(function(definition) {
      const field = node('div');
      const id = 'media-destination-' + destinationSequence + '-' + definition.key;
      const label = node('label', 'form-label', definition.label);
      label.htmlFor = id;
      const input = node('input', 'form-control');
      input.id = id;
      input.type = definition.type;
      input.dataset.field = definition.key;
      input.value = definition.value;
      if (definition.key === 'player') { input.min = '1'; input.step = '1'; input.required = true; }
      if (definition.key === 'videohub_input') { input.min = '1'; input.step = '1'; input.placeholder = 'Not assigned'; }
      if (definition.key === 'label') input.maxLength = 100;
      if (definition.key === 'slots') { input.placeholder = 'Comma-separated slot numbers'; input.required = true; }
      field.appendChild(label);
      field.appendChild(input);
      fields.appendChild(field);
    });
    row.appendChild(fields);
    row.appendChild(node('p', 'form-text mb-0 mt-2', 'Choose the VideoHub input that already receives this media player. Leave it blank to use this player only for testing.'));
    const remove = node('button', 'btn btn-sm btn-outline-danger mt-3', 'Remove player');
    remove.type = 'button';
    remove.addEventListener('click', function() { row.parentNode.removeChild(row); });
    row.appendChild(remove);
    el('media-destinations').appendChild(row);
  }
  async function fetchSetup() {
    if (setupInFlight) return;
    setupInFlight = true;
    el('media-setup-fields').disabled = true;
    show(el('media-retry-setup'), false);
    text(el('media-setup-status'), 'Loading setup…');
    try {
      const data = await request('/api/config/atem-media');
      const config = data.config || {};
      el('media-setup-enabled').checked = !!config.atem_media_enabled;
      el('media-node-path').value = config.atem_media_node_path || '';
      clear(el('media-destinations'));
      (config.atem_media_destinations || []).forEach(addDestination);
      el('media-setup-fields').disabled = false;
      text(el('media-setup-status'), '');
    } catch (error) {
      text(el('media-setup-status'), 'Could not load setup. ' + errorMessage(error));
      show(el('media-retry-setup'), true);
    } finally {
      setupInFlight = false;
    }
  }
  function collectDestinations() {
    const destinations = [];
    const players = {};
    const slots = {};
    const videohubInputs = {};
    Array.prototype.forEach.call(el('media-destinations').children, function(row) {
      const playerText = row.querySelector('[data-field="player"]').value.trim();
      const slotText = row.querySelector('[data-field="slots"]').value.trim();
      const player = Number(playerText);
      const label = row.querySelector('[data-field="label"]').value.trim();
      const videohubText = row.querySelector('[data-field="videohub_input"]').value.trim();
      if (!/^\d+$/.test(playerText) || player < 1 || players[player]) throw new Error('Each destination needs a different media player number, starting at 1.');
      players[player] = true;
      const numbers = slotText.split(',').map(function(value) {
        const trimmed = value.trim();
        if (!/^\d+$/.test(trimmed) || Number(trimmed) < 1) throw new Error('Enter reserved still slots as positive numbers separated by commas.');
        return Number(trimmed);
      });
      if (numbers.length < 2) throw new Error('Reserve at least two different still slots for each media player.');
      numbers.forEach(function(slot) {
        if (slots[slot]) throw new Error('Still slot ' + slot + ' is listed more than once. Each slot must belong to only one player.');
        slots[slot] = true;
      });
      const capabilities = state && state.connected ? state.capabilities || {} : {};
      if (capabilities.players && player > capabilities.players) throw new Error('Media Player ' + player + ' is outside the connected switcher’s ' + capabilities.players + ' players.');
      if (capabilities.stills && numbers.some(function(slot) { return slot > capabilities.stills; })) throw new Error('A reserved still slot is outside the connected switcher’s ' + capabilities.stills + ' slots.');
      const destination = { player: player, label: label || 'Media Player ' + player, slots: numbers };
      if (videohubText) {
        if (!/^\d+$/.test(videohubText) || Number(videohubText) < 1) {
          throw new Error('Enter a VideoHub input as a positive whole number, or leave it blank for testing only.');
        }
        const videohubInput = Number(videohubText);
        if (videohubInputs[videohubInput]) throw new Error('VideoHub input ' + videohubInput + ' is listed more than once. Each player needs its own VideoHub input.');
        videohubInputs[videohubInput] = true;
        destination.videohub_input = videohubInput;
      }
      destinations.push(destination);
    });
    return destinations;
  }
  async function saveSetup(event) {
    event.preventDefault();
    if (setupInFlight) return;
    let destinations;
    try { destinations = collectDestinations(); } catch (error) {
      text(el('media-setup-status'), errorMessage(error));
      return;
    }
    const body = {
      atem_media_enabled: el('media-setup-enabled').checked,
      atem_media_node_path: el('media-node-path').value.trim(),
      atem_media_destinations: destinations
    };
    if (body.atem_media_enabled && !destinations.length) {
      text(el('media-setup-status'), 'Add a media player and reserve its still slots before enabling uploads.');
      return;
    }
    setupInFlight = true;
    el('media-setup-fields').disabled = true;
    text(el('media-setup-status'), 'Saving setup…');
    try {
      await jsonRequest('/api/config/atem-media', 'PUT', body);
      stateRevision += 1;
      text(el('media-setup-status'), 'Setup saved. Refreshing the ATEM media connection…');
      stateValid = false;
      updateControls();
      refreshState();
    } catch (error) {
      text(el('media-setup-status'), 'Could not save setup. ' + errorMessage(error));
    } finally {
      setupInFlight = false;
      el('media-setup-fields').disabled = false;
    }
  }

  if (el('media-search')) el('media-search').addEventListener('input', renderLibrary);
  if (el('media-filter')) el('media-filter').addEventListener('change', renderLibrary);
  if (el('media-refresh-library')) el('media-refresh-library').addEventListener('click', refreshLibrary);
  el('media-refresh-state').addEventListener('click', refreshState);
  if (el('media-upload-form')) el('media-upload-form').addEventListener('submit', uploadImage);
  if (el('media-edit-form')) el('media-edit-form').addEventListener('submit', saveImage);
  if (el('media-delete-image')) el('media-delete-image').addEventListener('click', deleteImage);
  if (el('media-load-button')) el('media-load-button').addEventListener('click', loadImage);
  if (el('media-player')) el('media-player').addEventListener('change', function() { renderPlayerDetail(); updateControls(); });
  if (el('media-setup')) {
    el('media-add-destination').addEventListener('click', function() { addDestination(); });
    el('media-retry-setup').addEventListener('click', fetchSetup);
    el('media-setup-form').addEventListener('submit', saveSetup);
    if (window.location.hash === '#media-setup') el('media-setup').open = true;
    window.addEventListener('hashchange', function() { if (window.location.hash === '#media-setup') el('media-setup').open = true; });
    fetchSetup();
  }
  document.addEventListener('visibilitychange', function() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (!document.hidden) refreshState();
  });
  window.addEventListener('pagehide', function() { pageActive = false; if (pollTimer) clearTimeout(pollTimer); });
  window.addEventListener('pageshow', function(event) { pageActive = true; if (event.persisted) refreshState(); });
  updateControls();
  refreshLibrary();
  refreshState();
}

initializeMediaConfigPage();
