(function () {
  'use strict';

  const root = document.getElementById('digico-mixer');
  if (!root) return;

  const notice = document.getElementById('digico-mixer-notice');
  const picker = document.getElementById('digico-aux-picker');
  const auxGrid = document.getElementById('digico-aux-grid');
  const channelSection = document.getElementById('digico-channel-section');
  const channelGroups = document.getElementById('digico-channel-groups');
  const auxChange = document.getElementById('digico-aux-change');
  const auxLabel = document.getElementById('digico-aux-current-label');
  const auxIcon = document.getElementById('digico-aux-current-icon');
  const connection = document.getElementById('digico-mixer-connection');
  const snapshot = document.getElementById('digico-snapshot');
  const retry = document.getElementById('digico-mixer-retry');
  const iconSystem = window.TDeckDigicoIcons;
  const CONTROL_SEND_INTERVAL_MS = 40;

  const state = {
    config: null,
    selectedAux: null,
    pollBusy: false,
    configBusy: false,
    channelControls: new Map(),
    activeControls: new Set(),
    controlSends: new Map(),
    lastRevision: null,
    errorSince: 0,
  };

  function showNotice(message, kind) {
    if (!message) {
      notice.classList.add('d-none');
      return;
    }
    notice.className = `alert alert-${kind || 'secondary'}`;
    notice.textContent = message;
  }

  function setConnection(status) {
    const online = !!(status && status.connected);
    const running = !!(status && status.running);
    connection.className = `digico-connection ${online ? 'is-online' : (running ? 'is-waiting' : 'is-offline')}`;
    connection.innerHTML = '<span></span>' + (online ? ' Desk online' : (running ? ' Waiting for desk' : ' Desk offline'));
  }

  async function getJson(url, options) {
    const response = await fetch(url, {cache: 'no-store', ...(options || {})});
    let payload = {};
    try { payload = await response.json(); } catch (e) { /* handled below */ }
    if (!response.ok || payload.ok === false) {
      const error = new Error(payload.error || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function selectedAuxFromStorage(auxes) {
    let saved = '';
    try { saved = window.localStorage.getItem('tdeck.digico.aux') || ''; } catch (e) { /* ignore */ }
    return auxes.find(aux => String(aux.channel) === saved) || null;
  }

  function showPicker() {
    state.selectedAux = null;
    state.lastRevision = null;
    auxChange.classList.add('d-none');
    channelSection.classList.add('d-none');
    picker.classList.remove('d-none');
    renderAuxPicker();
  }

  function chooseAux(aux) {
    state.selectedAux = aux;
    state.lastRevision = null;
    try { window.localStorage.setItem('tdeck.digico.aux', String(aux.channel)); } catch (e) { /* ignore */ }
    root.style.setProperty('--digico-tint', aux.colour || '#3478f6');
    auxLabel.textContent = aux.label || `Aux ${aux.channel}`;
    const icon = iconSystem.normalize(aux.icon);
    auxIcon.replaceChildren();
    auxIcon.classList.toggle('d-none', !icon);
    if (icon) auxIcon.appendChild(iconSystem.create(icon, 'digico-current-icon-image'));
    auxChange.classList.remove('d-none');
    picker.classList.add('d-none');
    channelSection.classList.remove('d-none');
    buildChannels(state.config ? state.config.channels : []);
    pollAuxState();
  }

  function renderAuxPicker() {
    auxGrid.replaceChildren();
    const auxes = state.config ? (state.config.auxes || []) : [];
    if (!auxes.length) {
      const empty = document.createElement('div');
      empty.className = 'digico-config-empty';
      empty.textContent = 'No Personal Mix AUXes are available for this account.';
      auxGrid.appendChild(empty);
      return;
    }
    for (const aux of auxes) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'digico-aux-tile';
      button.style.setProperty('--tile-tint', aux.colour || '#3478f6');
      const icon = iconSystem.normalize(aux.icon);
      if (icon) button.appendChild(iconSystem.create(icon, 'digico-aux-icon'));
      const text = document.createElement('span');
      text.textContent = aux.label || `Aux ${aux.channel}`;
      button.appendChild(text);
      button.addEventListener('click', () => chooseAux(aux));
      auxGrid.appendChild(button);
    }
  }

  function sliderToDb(value) {
    const v = Number(value);
    if (v <= 0) return -150;
    return ((Math.log(v * 100) / Math.log(100)) * 100) - 90;
  }

  function dbToSlider(db) {
    const value = Number(db);
    if (!Number.isFinite(value) || value <= -90) return 0;
    return Math.max(0, Math.min(1, Math.pow(100, (value + 90) / 100) / 100));
  }

  function formatDb(db) {
    const value = Number(db);
    if (!Number.isFinite(value) || value <= -90) return '−∞';
    return `${value > 0 ? '+' : ''}${value.toFixed(1)} dB`;
  }

  function formatPan(value) {
    const pan = Number(value);
    if (!Number.isFinite(pan) || Math.abs(pan - .5) < .015) return 'C';
    const amount = Math.round(Math.abs(pan - .5) * 200);
    return `${pan < .5 ? 'L' : 'R'} ${amount}`;
  }

  function updateSliderVisual(input, valueLabel, field) {
    input.style.setProperty('--value', `${Number(input.value) * 100}%`);
    valueLabel.textContent = field === 'level' ? formatDb(sliderToDb(input.value)) : formatPan(input.value);
  }

  function controlSendState(key) {
    if (!state.controlSends.has(key)) {
      state.controlSends.set(key, {
        inFlight: false,
        queued: null,
        timer: null,
        lastSentAt: 0,
      });
    }
    return state.controlSends.get(key);
  }

  function scheduleControlSend(key, immediate) {
    const entry = controlSendState(key);
    if (entry.inFlight || entry.timer || !entry.queued) return;
    const elapsed = Date.now() - Number(entry.lastSentAt || 0);
    const delay = immediate ? 0 : Math.max(0, CONTROL_SEND_INTERVAL_MS - elapsed);
    if (delay <= 0) {
      flushControlSend(key);
      return;
    }
    entry.timer = window.setTimeout(() => {
      entry.timer = null;
      flushControlSend(key);
    }, delay);
  }

  async function flushControlSend(key) {
    const entry = controlSendState(key);
    if (entry.inFlight || !entry.queued) return;
    const change = entry.queued;
    entry.queued = null;
    entry.inFlight = true;
    entry.lastSentAt = Date.now();
    try {
      await getJson(`/api/digico/aux/${change.aux}/channel/${change.channel}/${change.field}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({value: change.value, final: change.final}),
      });
    } catch (error) {
      showNotice(error.message || 'Could not send the mix change.', 'danger');
    } finally {
      entry.inFlight = false;
      if (entry.queued) scheduleControlSend(key, !!entry.queued.final);
    }
  }

  function sendControl(channel, field, rawValue, final) {
    if (!state.selectedAux) return;
    const key = `${state.selectedAux.channel}:${channel}:${field}`;
    const entry = controlSendState(key);
    entry.queued = {
      aux: Number(state.selectedAux.channel),
      channel: Number(channel),
      field,
      value: field === 'level' ? sliderToDb(rawValue) : Number(rawValue),
      final: !!final,
    };
    if (final && entry.timer) {
      window.clearTimeout(entry.timer);
      entry.timer = null;
    }
    scheduleControlSend(key, !!final);
  }

  function buildControl(channel, field, initialValue) {
    const control = document.createElement('div');
    control.className = `digico-control ${field === 'pan' ? 'digico-pan-control' : ''}`;
    const label = document.createElement('label');
    label.className = 'digico-control-label';
    const title = document.createElement('span');
    title.textContent = field === 'level' ? 'Level' : 'Pan';
    const value = document.createElement('span');
    value.className = 'digico-value';
    label.append(title, value);
    const input = document.createElement('input');
    input.className = 'digico-range';
    input.type = 'range';
    input.min = '0';
    input.max = '1';
    input.step = '.001';
    input.value = field === 'level' ? dbToSlider(initialValue) : (Number.isFinite(Number(initialValue)) ? Number(initialValue) : .5);
    const activeKey = `${channel}:${field}`;
    const start = () => state.activeControls.add(activeKey);
    const finish = () => {
      sendControl(channel, field, input.value, true);
      window.setTimeout(() => state.activeControls.delete(activeKey), 250);
    };
    input.addEventListener('pointerdown', start);
    input.addEventListener('touchstart', start, {passive: true});
    input.addEventListener('input', () => {
      updateSliderVisual(input, value, field);
      sendControl(channel, field, input.value, false);
    });
    input.addEventListener('change', finish);
    input.addEventListener('dblclick', () => {
      input.value = field === 'level' ? dbToSlider(0) : .5;
      updateSliderVisual(input, value, field);
      finish();
    });
    label.htmlFor = `digico-${field}-${channel}`;
    input.id = label.htmlFor;
    updateSliderVisual(input, value, field);
    control.append(label, input);
    return {element: control, input, value};
  }

  function updateSendToggle(control, sendOn) {
    const known = typeof sendOn === 'boolean';
    if (known) control.sendOn = sendOn;
    control.button.disabled = !known || control.busy;
    control.button.classList.toggle('is-on', known && sendOn);
    control.button.classList.toggle('is-off', known && !sendOn);
    control.button.classList.toggle('is-loading', !known);
    control.button.textContent = known ? (sendOn ? 'Send On' : 'Send Off') : 'Loading…';
    control.button.setAttribute('aria-pressed', known && sendOn ? 'true' : 'false');
  }

  function buildSendToggle(channel) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'digico-send-toggle is-loading';
    button.setAttribute('aria-label', `${channel.label || `Channel ${channel.channel}`} AUX send`);
    const control = {button, sendOn: null, busy: false, holdUntil: 0};
    updateSendToggle(control, channel.sendOn);
    button.addEventListener('click', async () => {
      if (!state.selectedAux || control.busy || typeof control.sendOn !== 'boolean') return;
      const previous = control.sendOn;
      const next = !previous;
      const aux = Number(state.selectedAux.channel);
      control.busy = true;
      control.holdUntil = Date.now() + 750;
      updateSendToggle(control, next);
      try {
        const payload = await getJson(`/api/digico/aux/${aux}/channel/${channel.channel}/on`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({value: next, final: true}),
        });
        control.busy = false;
        updateSendToggle(control, !!payload.value);
      } catch (error) {
        control.busy = false;
        updateSendToggle(control, previous);
        showNotice(error.message || 'Could not change the channel send.', 'danger');
      }
    });
    return control;
  }

  function buildChannels(channels) {
    state.channelControls.clear();
    channelGroups.replaceChildren();
    let previousHeading = '';
    let list = null;
    for (const channel of channels || []) {
      const headingText = String(channel.group || '').trim();
      if (headingText && headingText !== previousHeading) {
        const heading = document.createElement('h3');
        heading.className = 'digico-channel-group-title';
        heading.textContent = headingText;
        channelGroups.appendChild(heading);
        list = null;
      }
      previousHeading = headingText;
      if (!list) {
        list = document.createElement('div');
        list.className = 'digico-channel-list';
        channelGroups.appendChild(list);
      }
      const row = document.createElement('div');
      row.className = `digico-channel ${state.selectedAux && state.selectedAux.stereo ? 'is-stereo' : ''}`;
      const name = document.createElement('div');
      name.className = 'digico-channel-name';
      const icon = iconSystem.normalize(channel.icon);
      if (icon) name.appendChild(iconSystem.create(icon, 'digico-channel-icon'));
      const nameText = document.createElement('span');
      nameText.textContent = channel.label || `Channel ${channel.channel}`;
      name.appendChild(nameText);
      const identity = document.createElement('div');
      identity.className = 'digico-channel-identity';
      const send = buildSendToggle(channel);
      identity.append(name, send.button);
      const level = buildControl(channel.channel, 'level', channel.level);
      row.append(identity, level.element);
      let pan = null;
      if (state.selectedAux && state.selectedAux.stereo) {
        pan = buildControl(channel.channel, 'pan', channel.pan);
        row.appendChild(pan.element);
      }
      state.channelControls.set(Number(channel.channel), {send, level, pan});
      list.appendChild(row);
    }
  }

  function applyAuxState(payload) {
    if (Number.isFinite(Number(payload.revision))) state.lastRevision = Number(payload.revision);
    setConnection(payload.status || {});
    snapshot.textContent = payload.snapshot ? `Snapshot: ${payload.snapshot}` : '';
    for (const channel of payload.channels || []) {
      const controls = state.channelControls.get(Number(channel.channel));
      if (!controls) continue;
      if (typeof channel.sendOn === 'boolean' && !controls.send.busy && Date.now() >= controls.send.holdUntil) {
        updateSendToggle(controls.send, channel.sendOn);
      }
      if (channel.level !== null && !state.activeControls.has(`${channel.channel}:level`)) {
        controls.level.input.value = dbToSlider(channel.level);
        updateSliderVisual(controls.level.input, controls.level.value, 'level');
      }
      if (controls.pan && channel.pan !== null && !state.activeControls.has(`${channel.channel}:pan`)) {
        controls.pan.input.value = Number(channel.pan);
        updateSliderVisual(controls.pan.input, controls.pan.value, 'pan');
      }
    }
    if (payload.status && payload.status.connected) {
      state.errorSince = 0;
      showNotice('', 'secondary');
    } else if (!state.errorSince) {
      state.errorSince = Date.now();
    } else if ((Date.now() - state.errorSince) > 4000) {
      showNotice('TDeck is running, but the desk is not replying. Your controls will remain available while it reconnects.', 'warning');
    }
  }

  async function pollAuxState() {
    if (state.pollBusy || !state.selectedAux || document.hidden) return;
    const selectedAux = Number(state.selectedAux.channel);
    state.pollBusy = true;
    try {
      const revisionQuery = Number.isFinite(state.lastRevision) ? `?revision=${encodeURIComponent(state.lastRevision)}` : '';
      const payload = await getJson(`/api/digico/aux/${selectedAux}/state${revisionQuery}`);
      if (!state.selectedAux || Number(state.selectedAux.channel) !== selectedAux) return;
      if (payload.unchanged) {
        setConnection(payload.status || {});
        return;
      }
      applyAuxState(payload);
    } catch (error) {
      setConnection({running: false, connected: false});
      showNotice(error.status === 401 ? 'Your session has expired. Sign in again to continue mixing.' : (error.message || 'Could not load this mix.'), 'danger');
    } finally {
      state.pollBusy = false;
    }
  }

  async function loadConfig(forcePicker) {
    if (state.configBusy) return;
    state.configBusy = true;
    try {
      const payload = await getJson('/api/digico/mixer/config');
      state.config = payload;
      setConnection(payload.status || {});
      snapshot.textContent = payload.snapshot ? `Snapshot: ${payload.snapshot}` : '';
      const auxes = payload.auxes || [];
      const current = state.selectedAux ? auxes.find(aux => Number(aux.channel) === Number(state.selectedAux.channel)) : null;
      if (forcePicker || (!current && !state.selectedAux)) {
        const saved = forcePicker ? null : selectedAuxFromStorage(auxes);
        if (saved) chooseAux(saved);
        else showPicker();
      } else if (current) {
        state.selectedAux = current;
      } else {
        showPicker();
      }
      if (!auxes.length) showNotice('No enabled AUXes are available. Ask a TDeck administrator to check desk discovery, AUX setup and your group permissions.', 'warning');
      else showNotice('', 'secondary');
    } catch (error) {
      setConnection({running: false, connected: false});
      showNotice(error.status === 401 ? 'Your session has expired. Sign in again to continue.' : (error.message || 'Could not load the desk configuration.'), 'danger');
      picker.classList.remove('d-none');
    } finally {
      state.configBusy = false;
    }
  }

  auxChange.addEventListener('click', showPicker);
  retry.addEventListener('click', () => loadConfig(false));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      loadConfig(false);
      pollAuxState();
    }
  });

  loadConfig(false);
  window.setInterval(pollAuxState, 750);
  window.setInterval(() => loadConfig(false), 30000);
})();
