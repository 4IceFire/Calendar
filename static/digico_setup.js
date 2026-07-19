(function () {
  'use strict';

  const root = document.getElementById('digico-setup');
  if (!root) return;

  const message = document.getElementById('digico-setup-message');
  const auxTable = document.getElementById('digico-aux-table');
  const channelTable = document.getElementById('digico-channel-table');
  const deviceList = document.getElementById('digico-device-list');
  const iconSystem = window.TDeckDigicoIcons;
  const state = {payload: null, dirty: false, busy: false, initialized: false};
  let iconDialog = null;
  let iconTarget = null;

  function setMessage(text, kind) {
    message.className = text ? `alert alert-${kind || 'secondary'} mb-3` : 'mb-3';
    message.textContent = text || '';
  }

  async function requestJson(url, options) {
    const response = await fetch(url, {cache: 'no-store', ...(options || {})});
    let payload = {};
    try { payload = await response.json(); } catch (e) { /* handled below */ }
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function field(id) { return document.getElementById(id); }
  function value(id) { return field(id) ? field(id).value : ''; }
  function numberValue(id, fallback) {
    const number = Number(value(id));
    return Number.isFinite(number) ? number : fallback;
  }

  function setConnectionFields(config) {
    field('digico-enabled').checked = !!config.digico_enabled;
    field('digico-ip').value = config.digico_ip || '';
    field('digico-port').value = config.digico_port == null ? 9000 : config.digico_port;
    field('digico-listen-address').value = config.digico_listen_address || '0.0.0.0';
    field('digico-listen-port').value = config.digico_listen_port == null ? 8000 : config.digico_listen_port;
    field('digico-request-interval').value = config.digico_request_interval == null ? .1 : config.digico_request_interval;
    field('digico-retry-interval').value = config.digico_retry_interval == null ? 1 : config.digico_retry_interval;
    field('digico-stale-after').value = config.digico_stale_after == null ? 10 : config.digico_stale_after;
  }

  function configForNumber(items, number) {
    return Array.isArray(items) && items[number - 1] && typeof items[number - 1] === 'object' ? items[number - 1] : {};
  }

  function combinedItems(discovered, configured, kind) {
    const byNumber = new Map();
    for (const item of Array.isArray(discovered) ? discovered : []) {
      const number = Number(item.channel);
      if (number > 0) byNumber.set(number, {...item});
    }
    const maximum = Math.max(byNumber.size ? Math.max(...byNumber.keys()) : 0, Array.isArray(configured) ? configured.length : 0);
    const out = [];
    for (let number = 1; number <= maximum; number += 1) {
      const desk = byNumber.get(number) || {channel: number, deskLabel: `${kind === 'aux' ? 'Aux' : 'Channel'} ${number}`};
      const custom = configForNumber(configured, number);
      out.push({
        ...desk,
        ...custom,
        channel: number,
        deskLabel: desk.deskLabel || desk.label || `${kind === 'aux' ? 'Aux' : 'Channel'} ${number}`,
        label: custom.label || desk.label || desk.deskLabel || '',
        enabled: custom.enabled == null ? (desk.enabled == null ? true : !!desk.enabled) : !!custom.enabled,
        order: custom.order == null ? (desk.order == null ? number : desk.order) : custom.order,
      });
    }
    out.sort((left, right) => {
      const leftOrder = Number(left.order) || Number(left.channel);
      const rightOrder = Number(right.order) || Number(right.channel);
      return leftOrder - rightOrder || Number(left.channel) - Number(right.channel);
    });

    // Older TDeck configs stored the same group name on every channel in a
    // group. Section headings now belong only to the first channel beneath
    // them, so collapse those consecutive legacy values when editing.
    if (kind === 'channel') {
      let previousHeading = '';
      for (const item of out) {
        const heading = String(item.group || item.title || '').trim();
        if (heading && heading === previousHeading) item.group = '';
        else if (heading) item.group = heading;
        previousHeading = heading;
      }
    }
    return out;
  }

  function labeledInput(labelText, className, valueText, type) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement('input');
    input.className = `form-control form-control-sm ${className}`;
    input.type = type || 'text';
    input.value = valueText == null ? '' : String(valueText);
    if (input.type === 'number') input.min = '1';
    label.appendChild(input);
    return label;
  }

  function updateIconButton(button, input, valueText, kind) {
    const icon = iconSystem.normalize(valueText);
    input.value = icon;
    button.replaceChildren();
    button.appendChild(iconSystem.create(icon, 'digico-icon-select-preview'));
    const text = document.createElement('span');
    text.textContent = iconSystem.find(icon).label;
    button.appendChild(text);
    const subject = kind === 'channel' ? 'channel' : 'AUX';
    button.setAttribute('aria-label', `Choose ${subject} icon. Current selection: ${text.textContent}`);
  }

  function ensureIconDialog() {
    if (iconDialog) return iconDialog;
    const dialog = document.createElement('dialog');
    dialog.className = 'digico-icon-dialog';
    dialog.setAttribute('aria-labelledby', 'digico-icon-dialog-title');

    const header = document.createElement('div');
    header.className = 'digico-icon-dialog-header';
    const heading = document.createElement('h2');
    heading.id = 'digico-icon-dialog-title';
    heading.className = 'h5 mb-0';
    heading.textContent = 'Choose an icon';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('aria-label', 'Close icon picker');
    close.addEventListener('click', () => dialog.close());
    header.append(heading, close);

    const help = document.createElement('p');
    help.className = 'text-muted small mb-3';
    help.textContent = 'AUXes and input channels use the same line style throughout Personal Mixes.';
    const grid = document.createElement('div');
    grid.className = 'digico-icon-grid';
    for (const item of iconSystem.items) {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'digico-icon-option';
      option.dataset.icon = item.id;
      option.appendChild(iconSystem.create(item.id, 'digico-icon-option-image'));
      const label = document.createElement('span');
      label.textContent = item.label;
      option.appendChild(label);
      option.addEventListener('click', () => {
        if (!iconTarget) return;
        updateIconButton(iconTarget.button, iconTarget.input, item.id, iconTarget.kind);
        state.dirty = true;
        dialog.close();
      });
      grid.appendChild(option);
    }
    dialog.append(header, help, grid);
    dialog.addEventListener('click', event => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener('close', () => { iconTarget = null; });
    document.body.appendChild(dialog);
    iconDialog = dialog;
    return dialog;
  }

  function iconControl(item, kind) {
    const wrap = document.createElement('div');
    wrap.className = 'digico-icon-select-wrap';
    const label = document.createElement('span');
    label.className = 'digico-icon-select-label';
    label.textContent = 'Icon';
    const input = document.createElement('input');
    input.type = 'hidden';
    input.className = 'digico-item-icon';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'digico-icon-select';
    button.setAttribute('aria-haspopup', 'dialog');
    updateIconButton(button, input, item.icon || '', kind);
    button.addEventListener('click', () => {
      const dialog = ensureIconDialog();
      iconTarget = {button, input, kind};
      const selected = input.value;
      for (const option of dialog.querySelectorAll('.digico-icon-option')) {
        const active = option.dataset.icon === selected;
        option.classList.toggle('is-selected', active);
        option.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
      dialog.showModal();
    });
    wrap.append(label, button, input);
    return wrap;
  }

  function updateMoveButtons(target) {
    const rows = Array.from(target.querySelectorAll('.digico-config-row'));
    rows.forEach((row, index) => {
      const up = row.querySelector('.digico-move-up');
      const down = row.querySelector('.digico-move-down');
      const position = row.querySelector('.digico-order-position');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === rows.length - 1;
      if (position) position.textContent = `${index + 1} of ${rows.length}`;
    });
  }

  function moveRow(row, direction) {
    const target = row.parentElement;
    if (!target) return;
    const neighbour = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!neighbour || !neighbour.classList.contains('digico-config-row')) return;
    if (direction < 0) target.insertBefore(row, neighbour);
    else target.insertBefore(neighbour, row);
    updateMoveButtons(target);
    state.dirty = true;
  }

  function moveControls(row, item, kind) {
    const wrap = document.createElement('div');
    wrap.className = 'digico-order-controls';
    const label = document.createElement('span');
    label.className = 'digico-order-label';
    label.textContent = 'Move';
    const buttons = document.createElement('div');
    buttons.className = 'btn-group btn-group-sm';
    const itemName = item.label || `${kind === 'aux' ? 'AUX' : 'channel'} ${item.channel}`;

    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'btn btn-outline-secondary digico-move-up';
    up.textContent = '↑';
    up.title = `Move ${itemName} up`;
    up.setAttribute('aria-label', up.title);
    up.addEventListener('click', () => moveRow(row, -1));

    const down = document.createElement('button');
    down.type = 'button';
    down.className = 'btn btn-outline-secondary digico-move-down';
    down.textContent = '↓';
    down.title = `Move ${itemName} down`;
    down.setAttribute('aria-label', down.title);
    down.addEventListener('click', () => moveRow(row, 1));

    const position = document.createElement('span');
    position.className = 'digico-order-position';
    buttons.append(up, down);
    wrap.append(label, buttons, position);
    return wrap;
  }

  function renderMixerItems(kind, items) {
    const target = kind === 'aux' ? auxTable : channelTable;
    target.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'digico-config-empty';
      empty.textContent = 'Nothing discovered yet. Enable the integration, verify the desk IP and ports, then select Rediscover Desk.';
      target.appendChild(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement('div');
      row.className = `digico-config-row ${kind === 'channel' ? 'is-channel' : 'is-aux'}`;
      row.dataset.channel = String(item.channel);
      row.dataset.deskLabel = item.deskLabel || '';

      const enabledWrap = document.createElement('div');
      enabledWrap.className = 'form-check form-switch';
      const enabled = document.createElement('input');
      enabled.className = 'form-check-input digico-item-enabled';
      enabled.type = 'checkbox';
      enabled.checked = !!item.enabled;
      enabled.title = 'Show in Personal Mixes';
      enabledWrap.appendChild(enabled);

      const number = document.createElement('div');
      number.className = 'digico-config-number';
      number.textContent = String(item.channel);
      number.title = item.deskLabel || '';
      row.append(enabledWrap, number, labeledInput('Label', 'digico-item-label', item.label));
      if (kind === 'channel') {
        const heading = labeledInput('Section heading', 'digico-item-group', item.group || '');
        heading.querySelector('input').placeholder = 'Only where a new section starts';
        row.appendChild(heading);
        row.appendChild(iconControl(item, kind));
      } else {
        row.appendChild(labeledInput('Colour', 'digico-item-colour', item.colour || '#3478f6', 'color'));
        row.appendChild(iconControl(item, kind));
      }
      row.appendChild(moveControls(row, item, kind));
      target.appendChild(row);
    }
    updateMoveButtons(target);
  }

  function renderDevice(raw) {
    const device = raw || {};
    const row = document.createElement('div');
    row.className = 'digico-device-row';

    function check(labelText, className, checked, title) {
      const wrap = document.createElement('div');
      wrap.className = 'form-check digico-device-check';
      const input = document.createElement('input');
      input.className = `form-check-input ${className}`;
      input.type = 'checkbox';
      input.checked = !!checked;
      const label = document.createElement('label');
      label.className = 'form-check-label small';
      label.textContent = labelText;
      if (title) wrap.title = title;
      wrap.append(input, label);
      return wrap;
    }

    row.appendChild(check('Enabled', 'digico-device-enabled', device.enabled == null ? true : device.enabled));
    row.appendChild(labeledInput('Name', 'digico-device-name', device.name || ''));
    row.appendChild(labeledInput('IP / hostname', 'digico-device-ip', device.ip || ''));
    const port = labeledInput('Receive port', 'digico-device-port', device.port == null ? 8000 : device.port, 'number');
    port.querySelector('input').max = '65535';
    row.appendChild(port);
    row.appendChild(check('Receive desk updates', 'digico-device-broadcast', device.broadcast == null ? true : device.broadcast, 'Forwards desk and other configured device packets to this device.'));
    row.appendChild(check('Echo own packets', 'digico-device-loopback', !!device.loopback, 'Usually leave off to prevent duplicate updates.'));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn-sm btn-outline-danger digico-device-remove';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => { row.remove(); state.dirty = true; });
    row.appendChild(remove);
    deviceList.appendChild(row);
  }

  function renderDevices(devices) {
    deviceList.replaceChildren();
    for (const device of Array.isArray(devices) ? devices : []) renderDevice(device);
    if (!deviceList.children.length) {
      const empty = document.createElement('div');
      empty.className = 'digico-config-empty digico-device-empty';
      empty.textContent = 'No external OSC devices configured. Web mixer users do not need an entry here.';
      deviceList.appendChild(empty);
    }
  }

  function renderStatus(status) {
    const data = status || {};
    field('digico-status-desk').textContent = data.desk || 'Not configured';
    field('digico-status-connection').textContent = data.connected ? 'Online' : (data.running ? 'Waiting for replies' : (data.enabled ? 'Stopped' : 'Disabled'));
    field('digico-status-discovery').textContent = data.ready ? `${data.channels || 0} channels / ${data.auxes || 0} AUXes` : (data.missingDiscoveryRequest || 'Waiting');
    field('digico-status-snapshot').textContent = data.snapshot || '—';
    field('digico-aux-count').textContent = String(data.auxes || 0);
    field('digico-channel-count').textContent = String(data.channels || 0);

    const diagnostics = document.getElementById('digico-diagnostics');
    diagnostics.replaceChildren();
    const fields = [
      ['Running', data.running ? 'Yes' : 'No'],
      ['Connected', data.connected ? 'Yes' : 'No'],
      ['Ready', data.ready ? 'Yes' : 'No'],
      ['Listening', data.listen || '—'],
      ['Last desk packet', data.lastDeskPacketAge == null ? 'Never' : `${Number(data.lastDeskPacketAge).toFixed(1)}s ago`],
      ['Packets received', data.packetsReceived == null ? 0 : data.packetsReceived],
      ['Packets sent', data.packetsSent == null ? 0 : data.packetsSent],
      ['Unknown packets ignored', data.ignoredPackets == null ? 0 : data.ignoredPackets],
      ['Relay packets', data.relayPackets == null ? 0 : data.relayPackets],
      ['Pending requests', data.pendingRequests == null ? 0 : data.pendingRequests],
      ['Cached addresses', data.cacheEntries == null ? 0 : data.cacheEntries],
      ['OSC parse errors', data.parseErrors == null ? 0 : data.parseErrors],
      ['Last error', data.lastError || 'None'],
    ];
    for (const [label, val] of fields) {
      const card = document.createElement('div');
      card.className = 'digico-diagnostic';
      const small = document.createElement('span');
      small.textContent = label;
      const strong = document.createElement('strong');
      strong.textContent = String(val);
      card.append(small, strong);
      diagnostics.appendChild(card);
    }
    field('digico-raw-status').textContent = JSON.stringify(data, null, 2);
  }

  function renderPayload(payload, full) {
    state.payload = payload;
    renderStatus(payload.status || {});
    if (!full) return;
    const config = payload.config || {};
    setConnectionFields(config);
    renderMixerItems('aux', combinedItems((payload.discovered || {}).auxes, config.digico_auxes, 'aux'));
    renderMixerItems('channel', combinedItems((payload.discovered || {}).channels, config.digico_channels, 'channel'));
    renderDevices(config.digico_external_devices);
    state.initialized = true;
    state.dirty = false;
  }

  async function loadSetup(forceFull) {
    if (state.busy) return;
    state.busy = true;
    try {
      const payload = await requestJson('/api/digico/setup');
      const discoveredChanged = !state.payload ||
        Number((state.payload.status || {}).channels || 0) !== Number((payload.status || {}).channels || 0) ||
        Number((state.payload.status || {}).auxes || 0) !== Number((payload.status || {}).auxes || 0);
      renderPayload(payload, !!forceFull || !state.initialized || (!state.dirty && discoveredChanged));
    } catch (error) {
      setMessage(error.message || 'Could not load DiGiCo setup.', 'danger');
    } finally {
      state.busy = false;
    }
  }

  function collectMixerItems(kind) {
    const target = kind === 'aux' ? auxTable : channelTable;
    return Array.from(target.querySelectorAll('.digico-config-row')).map((row, index) => {
      const item = {
        channel: Number(row.dataset.channel),
        enabled: !!row.querySelector('.digico-item-enabled').checked,
        label: row.querySelector('.digico-item-label').value.trim(),
        icon: row.querySelector('.digico-item-icon').value,
        order: index + 1,
      };
      if (kind === 'channel') item.group = row.querySelector('.digico-item-group').value.trim();
      else item.colour = row.querySelector('.digico-item-colour').value;
      return item;
    });
  }

  function collectDevices() {
    return Array.from(deviceList.querySelectorAll('.digico-device-row')).map(row => ({
      enabled: !!row.querySelector('.digico-device-enabled').checked,
      name: row.querySelector('.digico-device-name').value.trim(),
      ip: row.querySelector('.digico-device-ip').value.trim(),
      port: Number(row.querySelector('.digico-device-port').value) || 8000,
      broadcast: !!row.querySelector('.digico-device-broadcast').checked,
      loopback: !!row.querySelector('.digico-device-loopback').checked,
    })).filter(item => item.ip);
  }

  function collectConfig() {
    return {
      digico_enabled: !!field('digico-enabled').checked,
      digico_ip: value('digico-ip').trim(),
      digico_port: numberValue('digico-port', 9000),
      digico_listen_address: value('digico-listen-address').trim() || '0.0.0.0',
      digico_listen_port: numberValue('digico-listen-port', 8000),
      digico_request_interval: numberValue('digico-request-interval', .1),
      digico_retry_interval: numberValue('digico-retry-interval', 1),
      digico_stale_after: numberValue('digico-stale-after', 10),
      digico_auxes: collectMixerItems('aux'),
      digico_channels: collectMixerItems('channel'),
      digico_external_devices: collectDevices(),
    };
  }

  async function saveSetup() {
    const button = field('digico-setup-save');
    button.disabled = true;
    setMessage('Saving configuration and restarting the DiGiCo connection…', 'info');
    try {
      await requestJson('/api/digico/setup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectConfig()),
      });
      state.dirty = false;
      setMessage('DiGiCo setup saved. Desk discovery has restarted.', 'success');
      await loadSetup(true);
    } catch (error) {
      setMessage(error.message || 'Could not save DiGiCo setup.', 'danger');
    } finally {
      button.disabled = false;
    }
  }

  async function action(url, workingMessage, successMessage) {
    setMessage(workingMessage, 'info');
    try {
      const payload = await requestJson(url, {method: 'POST'});
      renderStatus(payload.status || {});
      setMessage(successMessage, 'success');
      window.setTimeout(() => loadSetup(false), 500);
    } catch (error) {
      setMessage(error.message || 'The action failed.', 'danger');
    }
  }

  root.addEventListener('input', event => {
    if (event.target && event.target.matches('input, select, textarea')) state.dirty = true;
  });
  root.addEventListener('change', event => {
    if (event.target && event.target.matches('input, select, textarea')) state.dirty = true;
  });
  field('digico-setup-save').addEventListener('click', saveSetup);
  field('digico-restart').addEventListener('click', () => action('/api/digico/restart', 'Restarting the UDP connection…', 'Connection restarted.'));
  field('digico-discover').addEventListener('click', () => action('/api/digico/discover', 'Clearing cached desk data and restarting discovery…', 'Desk discovery restarted.'));
  field('digico-refresh').addEventListener('click', () => loadSetup(false));
  field('digico-add-device').addEventListener('click', () => {
    const empty = deviceList.querySelector('.digico-device-empty');
    if (empty) empty.remove();
    renderDevice({enabled: true, port: 8000, broadcast: true, loopback: false});
    state.dirty = true;
  });
  for (const button of root.querySelectorAll('.digico-reset-discovered')) {
    button.addEventListener('click', () => {
      const target = button.dataset.kind === 'aux' ? auxTable : channelTable;
      for (const row of target.querySelectorAll('.digico-config-row')) {
        row.querySelector('.digico-item-label').value = row.dataset.deskLabel || '';
      }
      const rows = Array.from(target.querySelectorAll('.digico-config-row'));
      rows.sort((left, right) => Number(left.dataset.channel) - Number(right.dataset.channel));
      for (const row of rows) target.appendChild(row);
      updateMoveButtons(target);
      state.dirty = true;
      setMessage('Desk labels and physical desk order restored locally. Select Save & Restart to apply them.', 'info');
    });
  }

  loadSetup(true);
  window.setInterval(() => {
    if (!document.hidden) loadSetup(false);
  }, 3000);
})();
