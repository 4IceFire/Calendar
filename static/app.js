function _applyServiceIndicator(serviceKey, label, connected) {
  const pairs = [
    [`${serviceKey}-dot`, `${serviceKey}-label`],
    [`${serviceKey}-dot-home`, `${serviceKey}-label-home`],
  ];
  const statusText = connected ? `${label}: Online` : `${label}: Offline`;
  pairs.forEach(([dotId, labelId]) => {
    const dot = document.getElementById(dotId);
    const el = document.getElementById(labelId);
    if (!dot || !el) return;
    dot.classList.remove('bg-success', 'bg-danger');
    dot.classList.add(connected ? 'bg-success' : 'bg-danger');
    el.textContent = statusText;
  });
}

function _escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function _applyServiceIndicatorUnknown(serviceKey, label) {
  const pairs = [
    [`${serviceKey}-dot`, `${serviceKey}-label`],
    [`${serviceKey}-dot-home`, `${serviceKey}-label-home`],
  ];
  pairs.forEach(([dotId, labelId]) => {
    const dot = document.getElementById(dotId);
    const el = document.getElementById(labelId);
    if (!dot || !el) return;
    dot.classList.remove('bg-success');
    dot.classList.add('bg-danger');
    el.textContent = `${label}: Unknown`;
  });
}

async function updateStatusIndicators() {
  try {
    const res = await fetch('/api/status/summary', {cache: 'no-store'});
    if (!res.ok) throw new Error('fetch failed');
    const data = await res.json();
    _applyServiceIndicator('companion', 'Companion', !!(data && data.companion && data.companion.connected));
    _applyServiceIndicator('propresenter', 'ProPresenter', !!(data && data.propresenter && data.propresenter.connected));
    _applyServiceIndicator('videohub', 'VideoHub', !!(data && data.videohub && data.videohub.connected));
    _applyServiceIndicator('atem', 'ATEM', !!(data && data.atem && data.atem.connected));
  } catch (e) {
    _applyServiceIndicatorUnknown('companion', 'Companion');
    _applyServiceIndicatorUnknown('propresenter', 'ProPresenter');
    _applyServiceIndicatorUnknown('videohub', 'VideoHub');
    _applyServiceIndicatorUnknown('atem', 'ATEM');
  }
}

// initial check
updateStatusIndicators();
// refresh every 15s while the server keeps the backend snapshot warm in the background
setInterval(updateStatusIndicators, 15000);

async function updateActivityLogAlertBadge() {
  const badge = document.getElementById('activity-log-alert-badge');
  const menuLink = document.getElementById('activity-log-menu-link');
  const menuBadge = document.getElementById('activity-log-menu-badge');
  if (!badge && !menuLink && !menuBadge) return null;
  try {
    const res = await fetch('/api/activity-log/alerts', {cache: 'no-store'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data || !data.ok) throw new Error((data && data.error) || 'Failed to load log alerts');
    const count = Number(data.count || 0);
    const severity = String(data.severity || '') === 'failure' ? 'failure' : 'warning';
    const label = count > 99 ? '99+' : String(count);
    if (badge) badge.classList.remove('tdeck-log-alert-warning', 'tdeck-log-alert-failure');
    if (menuLink) menuLink.classList.remove('tdeck-log-menu-warning', 'tdeck-log-menu-failure');
    if (menuBadge) menuBadge.classList.remove('tdeck-log-menu-badge-warning', 'tdeck-log-menu-badge-failure');
    if (count > 0) {
      if (badge) {
        badge.textContent = label;
        badge.classList.add(severity === 'failure' ? 'tdeck-log-alert-failure' : 'tdeck-log-alert-warning');
        badge.classList.remove('d-none');
        badge.setAttribute('aria-label', `${count} unseen log warning${count === 1 ? '' : 's'} or failure${count === 1 ? '' : 's'}`);
      }
      if (menuLink) {
        menuLink.classList.add(severity === 'failure' ? 'tdeck-log-menu-failure' : 'tdeck-log-menu-warning');
        menuLink.setAttribute('aria-label', `Log, ${count} unseen warning/error alert${count === 1 ? '' : 's'}`);
      }
      if (menuBadge) {
        menuBadge.textContent = label;
        menuBadge.classList.add(severity === 'failure' ? 'tdeck-log-menu-badge-failure' : 'tdeck-log-menu-badge-warning');
        menuBadge.classList.remove('d-none');
      }
    } else {
      if (badge) {
        badge.textContent = '0';
        badge.classList.add('d-none');
        badge.removeAttribute('aria-label');
      }
      if (menuLink) menuLink.removeAttribute('aria-label');
      if (menuBadge) {
        menuBadge.textContent = '0';
        menuBadge.classList.add('d-none');
      }
    }
    return data;
  } catch (e) {
    if (badge) badge.classList.add('d-none');
    if (menuLink) menuLink.classList.remove('tdeck-log-menu-warning', 'tdeck-log-menu-failure');
    if (menuBadge) menuBadge.classList.add('d-none');
    return null;
  }
}

updateActivityLogAlertBadge();
setInterval(updateActivityLogAlertBadge, 15000);

function _uiMessageTimeoutMs() {
  try {
    const ms = window.TDECK_UI && window.TDECK_UI.messageTimeoutMs;
    const n = Number(ms);
    if (!Number.isFinite(n) || n <= 0) return 0;
    return Math.floor(n);
  } catch (e) {
    return 0;
  }
}

function _uiClearAutoHide(el) {
  if (!el) return;
  const id = el._tdeckAutoHideTimeoutId;
  if (id) {
    clearTimeout(id);
    el._tdeckAutoHideTimeoutId = null;
  }
}

function _uiScheduleAutoHide(el, clearFn) {
  if (!el) return;
  _uiClearAutoHide(el);
  const ms = _uiMessageTimeoutMs();
  if (!ms) return;
  el._tdeckAutoHideTimeoutId = setTimeout(() => {
    el._tdeckAutoHideTimeoutId = null;
    try {
      clearFn();
    } catch (e) {
      // ignore
    }
  }, ms);
}

document.querySelectorAll('form[data-confirm]').forEach(form => {
  if (form.getAttribute('data-confirm-bound') === '1') return;
  form.setAttribute('data-confirm-bound', '1');
  form.addEventListener('submit', (e) => {
    const msg = String(form.getAttribute('data-confirm') || 'Are you sure?');
    if (!window.confirm(msg)) {
      e.preventDefault();
    }
  });
});

// --- Routing page (quick VideoHub route) ---
function _routingSetStatus(msg, kind) {
  const el = document.getElementById('routing-status');
  if (!el) return;
  if (!msg) {
    _uiClearAutoHide(el);
    el.className = '';
    el.textContent = '';
    return;
  }
  const cls = kind === 'error' ? 'alert alert-danger' : (kind === 'warn' ? 'alert alert-warning' : 'alert alert-success');
  el.className = cls;
  el.textContent = msg;
  _uiScheduleAutoHide(el, () => {
    el.className = '';
    el.textContent = '';
  });
}

function _routingLabel(item) {
  if (!item) return '';
  const n = parseInt(item.number, 10);
  const label = String(item.label || '').trim();
  if (!Number.isFinite(n) || n <= 0) return '';
  return label ? `${n}: ${label}` : String(n);
}

function _routingParseAllowList(raw) {
  try {
    const arr = JSON.parse(String(raw || '[]'));
    if (!Array.isArray(arr)) return [];
    return arr.map(x => parseInt(x, 10)).filter(n => Number.isFinite(n) && n > 0);
  } catch (e) {
    return [];
  }
}

if (document.getElementById('routing-page')) {
  const root = document.getElementById('routing-page');
  const allowedOutputs = _routingParseAllowList(root.getAttribute('data-allowed-outputs'));
  const allowedInputs = _routingParseAllowList(root.getAttribute('data-allowed-inputs'));

  const elOutputs = document.getElementById('routing-outputs');
  const elInputs = document.getElementById('routing-inputs');
  const elCurrent = document.getElementById('routing-current');
  const btnApply = document.getElementById('routing-apply');

  let state = { configured: false, inputs: [], outputs: [], routing: [] };
  let selectedOutput = null;
  let selectedInput = null;

  function _filterList(list, allow) {
    const arr = Array.isArray(list) ? list : [];
    if (Array.isArray(allow) && allow.length > 0) {
      const set = new Set(allow.map(n => parseInt(n, 10)).filter(n => Number.isFinite(n) && n > 0));
      return arr.filter(it => set.has(parseInt(it.number, 10)));
    }
    return arr;
  }

  function _getCurrentInputForOutput(outNum) {
    const idx = parseInt(outNum, 10) - 1;
    if (!Array.isArray(state.routing)) return null;
    const v = state.routing[idx];
    const n = parseInt(v, 10);
    return (Number.isFinite(n) && n > 0) ? n : null;
  }

  function _setApplyEnabled() {
    if (!btnApply) return;
    btnApply.disabled = !(selectedOutput && selectedInput);
  }

  function _renderOutputs() {
    if (!elOutputs) return;
    const outputs = _filterList(state.outputs, allowedOutputs);
    elOutputs.innerHTML = '';

    outputs.forEach((o) => {
      const n = parseInt(o.number, 10);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'list-group-item list-group-item-action';
      btn.textContent = _routingLabel(o);
      if (selectedOutput === n) btn.classList.add('active');
      btn.addEventListener('click', () => {
        selectedOutput = n;
        const curIn = _getCurrentInputForOutput(n);
        selectedInput = curIn;
        _renderOutputs();
        _renderInputs();
        _renderCurrent();
        _setApplyEnabled();
      });
      elOutputs.appendChild(btn);
    });

    if (!outputs.length) {
      const div = document.createElement('div');
      div.className = 'list-group-item text-muted';
      div.textContent = 'No outputs configured';
      elOutputs.appendChild(div);
    }
  }

  function _renderInputs() {
    if (!elInputs) return;
    const inputs = _filterList(state.inputs, allowedInputs);
    elInputs.innerHTML = '';

    inputs.forEach((i) => {
      const n = parseInt(i.number, 10);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'list-group-item list-group-item-action';
      btn.textContent = _routingLabel(i);
      if (selectedInput === n) btn.classList.add('active');
      btn.addEventListener('click', () => {
        selectedInput = n;
        _renderInputs();
        _renderCurrent();
        _setApplyEnabled();
      });
      elInputs.appendChild(btn);
    });

    if (!inputs.length) {
      const div = document.createElement('div');
      div.className = 'list-group-item text-muted';
      div.textContent = 'No inputs configured';
      elInputs.appendChild(div);
    }
  }

  function _findLabel(list, num) {
    const n = parseInt(num, 10);
    const arr = Array.isArray(list) ? list : [];
    const it = arr.find(x => parseInt(x.number, 10) === n);
    return it ? _routingLabel(it) : (Number.isFinite(n) && n > 0 ? String(n) : '');
  }

  function _renderCurrent() {
    if (!elCurrent) return;
    if (!selectedOutput) {
      elCurrent.textContent = 'Select an output…';
      return;
    }
    const outText = _findLabel(state.outputs, selectedOutput);
    const curIn = _getCurrentInputForOutput(selectedOutput);
    const curText = curIn ? _findLabel(state.inputs, curIn) : '(unknown)';
    const selText = selectedInput ? _findLabel(state.inputs, selectedInput) : '(none)';
    elCurrent.textContent = `Output ${outText} is currently ${curText}. Selected: ${selText}.`;
  }

  async function _applyRoute() {
    if (!(selectedOutput && selectedInput)) return;
    _routingSetStatus('', '');
    if (btnApply) btnApply.disabled = true;
    try {
      const res = await fetch('/api/videohub/route', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({output: selectedOutput, input: selectedInput, zero_based: false}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || 'Route failed');

      // Update local snapshot
      const idx = parseInt(selectedOutput, 10) - 1;
      if (Array.isArray(state.routing) && idx >= 0) {
        while (state.routing.length <= idx) state.routing.push(null);
        state.routing[idx] = selectedInput;
      }
      _routingSetStatus('Routed successfully.', 'ok');
      _renderOutputs();
      _renderInputs();
      _renderCurrent();
    } catch (e) {
      _routingSetStatus(String(e.message || e), 'error');
    } finally {
      _setApplyEnabled();
    }
  }

  if (btnApply) btnApply.addEventListener('click', (ev) => {
    ev.preventDefault();
    _applyRoute();
  });

  (async () => {
    try {
      const res = await fetch('/api/videohub/state', {cache: 'no-store'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || 'Unable to load VideoHub state');
      state = data;
      if (!data.configured) {
        _routingSetStatus('VideoHub not configured (set videohub_ip). Showing fallback ports.', 'warn');
      }
      _renderOutputs();
      _renderInputs();
      _renderCurrent();
      _setApplyEnabled();
    } catch (e) {
      _routingSetStatus(String(e.message || e), 'error');
    }
  })();
}

// --- Config page ---
function _configSetStatus(msg, kind) {
  const el = document.getElementById('config-status');
  if (!el) return;
  if (!msg) {
    _configClearStatus();
    return;
  }
  let cls;
  if (kind === 'error') cls = 'alert alert-danger';
  else if (kind === 'warn') cls = 'alert alert-warning';
  else cls = 'alert alert-success';

  el.className = cls;

  // allow html for link/warning UX
  el.innerHTML = msg;

  _uiScheduleAutoHide(el, () => {
    _configClearStatus();
  });
}

function _configClearStatus() {
  const el = document.getElementById('config-status');
  if (!el) return;
  _uiClearAutoHide(el);
  el.className = '';
  el.textContent = '';
}

function _isPlainObject(v) {
  return v && typeof v === 'object' && !Array.isArray(v);
}

function _titleCaseFromKey(key) {
  return String(key || '')
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .split(' ')
    .map(w => w ? (w[0].toUpperCase() + w.slice(1)) : w)
    .join(' ');
}

const CONFIG_META = {
  webserver_port: {
    label: 'Web UI Port',
    help: 'Port the web UI listens on.',
  },
  server_port: {
    label: 'Legacy Web UI Port',
    help: 'Backward-compatible alias for Web UI Port.',
  },
  poll_interval: {
    label: 'Config Watch Interval (seconds)',
    help: 'How often the server checks config.json for changes.',
  },
  debug: {
    label: 'Debug Mode',
    help: 'Enables extra logging and developer-friendly errors.',
  },

  dark_mode: {
    label: 'Dark Mode',
    help: 'Enable dark theme for the Web UI.',
  },

  webui_message_timeout_seconds: {
    label: 'Message Timeout (seconds)',
    help: 'How long status messages stay visible before auto-hiding. Set to 0 to disable auto-hide.',
  },

  auth_enabled: {
    label: 'Require Login (Pages)',
    help: 'If enabled, the HTML pages require login. (The /api endpoints remain unauthenticated.)',
  },
  auth_idle_timeout_enabled: {
    label: 'Idle Timeout Enabled',
    help: 'Automatically logs a user out after inactivity.',
  },
  auth_idle_timeout_minutes: {
    label: 'Idle Timeout (minutes)',
    help: 'How many minutes of inactivity before logout.',
  },
  auth_min_password_length: {
    label: 'Minimum Password Length',
    help: 'Minimum length required for new passwords.',
  },
  auth_lockout_failed_attempts: {
    label: 'Failed Login Lockout Attempts',
    help: 'How many failed password attempts lock an account until an admin unlocks it.',
  },
  flask_secret_key: {
    label: 'Flask Secret Key',
    help: 'Used to sign session cookies. Changing this will log out all users and invalidate existing sessions.',
    sensitive: true,
    inputType: 'password',
  },

  companion_ip: {
    label: 'Companion Host',
    help: 'Bitfocus Companion IP or hostname.',
  },
  companion_port: {
    label: 'Companion Port',
    help: 'Bitfocus Companion HTTP port (often 8000).',
  },
  companion_timer_name: {
    label: 'Companion Timer Name Prefix',
    help: 'Creates custom variables like timer_name_1, timer_name_2, etc.',
  },

  propresenter_ip: {
    label: 'ProPresenter Host',
    help: 'ProPresenter machine IP or hostname.',
  },
  propresenter_port: {
    label: 'ProPresenter Port',
    help: 'ProPresenter HTTP API port.',
  },
  propresenter_timer_index: {
    label: 'ProPresenter Timer Index',
    help: 'Which ProPresenter timer this app sets/resets/starts.',
  },

  videohub_ip: {
    label: 'VideoHub Host',
    help: 'Blackmagic VideoHub IP or hostname.',
  },
  videohub_port: {
    label: 'VideoHub Port',
    help: 'Blackmagic VideoHub TCP port (default 9990).',
  },
  videohub_timeout: {
    label: 'VideoHub Timeout (seconds)',
    help: 'Socket timeout used for VideoHub TCP requests.',
  },
  videohub_presets_file: {
    label: 'VideoHub Presets File',
    help: 'JSON file where VideoHub routing presets are stored.',
  },

  atem_ip: {
    label: 'ATEM Host',
    help: 'Blackmagic ATEM switcher IP or hostname.',
  },
  atem_port: {
    label: 'ATEM Port',
    help: 'Blackmagic ATEM UDP port. Standard ATEM control uses 9910.',
  },
  atem_timeout: {
    label: 'ATEM Timeout (seconds)',
    help: 'Connection timeout used for ATEM audio control.',
  },

  EVENTS_FILE: {
    label: 'Events File',
    help: 'JSON file used to store scheduled events.',
  },

  // Legacy keys that may still exist in older config.json files
  timer_index: {
    label: 'Legacy ProPresenter Timer Index',
    help: 'Older name for ProPresenter Timer Index (propresenter_timer_index).',
  },
};

function _configMeta(key) {
  const meta = CONFIG_META[key] || {};
  const pretty = meta.label || _titleCaseFromKey(key);
  const help = meta.help || '';
  return {label: pretty, help};
}

function _renderConfigField(key, value) {
  const meta = _configMeta(key);

  const wrap = document.createElement('div');
  wrap.className = 'mb-3';

  const label = document.createElement('label');
  label.className = 'form-label';
  label.textContent = meta.label;

  const keyBadge = document.createElement('span');
  keyBadge.className = 'text-muted small ms-2';
  keyBadge.textContent = `(${key})`;
  label.appendChild(keyBadge);

  // Store both key + original type so we can parse on save.
  const type = (typeof value);

  let input;
  if (type === 'boolean') {
    const formCheck = document.createElement('div');
    formCheck.className = 'form-check';
    input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'form-check-input';
    input.checked = Boolean(value);
    const checkLabel = document.createElement('label');
    checkLabel.className = 'form-check-label';
    checkLabel.textContent = meta.label;

    const checkKeyBadge = document.createElement('span');
    checkKeyBadge.className = 'text-muted small ms-2';
    checkKeyBadge.textContent = `(${key})`;
    checkLabel.appendChild(checkKeyBadge);

    formCheck.appendChild(input);
    formCheck.appendChild(checkLabel);

    input.dataset.cfgKey = key;
    input.dataset.cfgType = 'boolean';
    wrap.appendChild(formCheck);

    if (meta.help) {
      const help = document.createElement('div');
      help.className = 'form-text';
      help.textContent = meta.help;
      wrap.appendChild(help);
    }

    return wrap;
  }

  if (type === 'number') {
    input = document.createElement('input');
    input.type = 'number';
    input.className = 'form-control';
    input.value = String(value);
    input.dataset.cfgType = Number.isInteger(value) ? 'int' : 'float';
  } else if (Array.isArray(value) || _isPlainObject(value)) {
    input = document.createElement('textarea');
    input.className = 'form-control';
    input.rows = 3;
    try {
      input.value = JSON.stringify(value, null, 2);
    } catch (e) {
      input.value = String(value);
    }
    input.dataset.cfgType = 'json';
  } else {
    input = document.createElement('input');
    input.type = meta.inputType || 'text';
    input.className = 'form-control';
    input.value = value == null ? '' : String(value);
    input.dataset.cfgType = 'string';
  }

  input.dataset.cfgKey = key;

  wrap.appendChild(label);
  wrap.appendChild(input);

  if (meta.help) {
    const help = document.createElement('div');
    help.className = 'form-text';
    help.textContent = meta.help;
    wrap.appendChild(help);
  }

  return wrap;
}

function _renderConfigGroups(cfg) {
  const nav = document.getElementById('config-nav');
  const panels = document.getElementById('config-panels');
  const legacyContainer = document.getElementById('config-groups');
  if (!nav || !panels) {
    // Backward-compatible: if template didn't get updated, do nothing.
    if (legacyContainer) legacyContainer.innerHTML = '';
    return;
  }
  nav.innerHTML = '';
  panels.innerHTML = '';

  const NAV_STORAGE_KEY = 'tdeck.config.activeGroup';

  function _groupIdFromTitle(title) {
    return String(title || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .trim() || 'group';
  }

  function _readActiveGroupId(defaultId) {
    try {
      const h = String(window.location.hash || '');
      if (h.startsWith('#cfg-')) return h.slice(5);
    } catch (e) {
      // ignore
    }
    try {
      const v = window.localStorage.getItem(NAV_STORAGE_KEY);
      if (v) return v;
    } catch (e) {
      // ignore
    }
    return defaultId;
  }

  function _setActiveGroupId(id) {
    const groupId = String(id || '').trim();
    if (!groupId) return;
    for (const btn of Array.from(nav.querySelectorAll('[data-group-id]'))) {
      btn.classList.toggle('active', String(btn.dataset.groupId) === groupId);
    }
    for (const panel of Array.from(panels.querySelectorAll('[data-group-id]'))) {
      panel.style.display = (String(panel.dataset.groupId) === groupId) ? '' : 'none';
    }
    try {
      window.localStorage.setItem(NAV_STORAGE_KEY, groupId);
    } catch (e) {
      // ignore
    }
    try {
      window.location.hash = `cfg-${groupId}`;
    } catch (e) {
      // ignore
    }
  }

  // Legacy keys that should not be edited anymore.
  const hiddenKeys = new Set([
    'videohub_allowed_outputs',
    'videohub_allowed_inputs',
  ]);
  const schedulingKeys = ['EVENTS_FILE'];
  const authKeys = [
    'auth_enabled',
    'auth_idle_timeout_enabled',
    'auth_idle_timeout_minutes',
    'auth_min_password_length',
    'auth_lockout_failed_attempts',
    'flask_secret_key',
  ];
  const proPresenterTimingKeys = [
    'propresenter_is_latest',
    'propresenter_timer_wait_stop_ms',
    'propresenter_timer_wait_set_ms',
    'propresenter_timer_wait_reset_ms',
  ];

  const baseGroups = [
    {
      title: 'Web UI',
      keys: ['webserver_port', 'server_port'],
    },
    {
      title: 'Companion',
      keys: ['companion_ip', 'companion_port', 'companion_timer_name'],
    },
    {
      title: 'ProPresenter',
      keys: ['propresenter_ip', 'propresenter_port', 'propresenter_timer_index'],
    },
    {
      title: 'VideoHub',
      keys: ['videohub_ip', 'videohub_port', 'videohub_timeout', 'videohub_presets_file'],
    },
    {
      title: 'ATEM',
      keys: ['atem_ip', 'atem_port', 'atem_timeout'],
    },
  ];

  const used = new Set();
  for (const hk of hiddenKeys) {
    if (Object.prototype.hasOwnProperty.call(cfg, hk)) used.add(hk);
  }

  const renderedGroups = [];
  let webUiBody = null;

  function _addGroupPanel({title, keys, postRender}) {
    const presentKeys = (keys || []).filter(k => Object.prototype.hasOwnProperty.call(cfg, k) && !hiddenKeys.has(k));
    if (!presentKeys.length) return;

    const id = _groupIdFromTitle(title);
    for (const k of presentKeys) used.add(k);

    const panel = document.createElement('div');
    panel.dataset.groupId = id;

    const card = document.createElement('div');
    card.className = 'card';
    const body = document.createElement('div');
    body.className = 'card-body';

    const h = document.createElement('h2');
    h.className = 'h5 mb-3';
    h.textContent = title;
    body.appendChild(h);

    for (const k of presentKeys) {
      body.appendChild(_renderConfigField(k, cfg[k]));
    }

    if (typeof postRender === 'function') {
      postRender(body);
    }

    card.appendChild(body);
    panel.appendChild(card);
    panels.appendChild(panel);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'list-group-item list-group-item-action';
    btn.textContent = title;
    btn.dataset.groupId = id;
    btn.addEventListener('click', () => _setActiveGroupId(id));
    nav.appendChild(btn);

    renderedGroups.push(id);
    if (title === 'Web UI') webUiBody = body;
  }

  for (const g of baseGroups) {
    _addGroupPanel({
      title: g.title,
      keys: g.keys,
      postRender: (body) => {
        if (g.title !== 'Web UI') return;

        const presentSchedulingKeys = schedulingKeys.filter(k => Object.prototype.hasOwnProperty.call(cfg, k));
        if (presentSchedulingKeys.length) {
          for (const k of presentSchedulingKeys) used.add(k);
          const sub = document.createElement('div');
          sub.className = 'border rounded p-2 mt-2 bg-body-tertiary';
          const hh = document.createElement('div');
          hh.className = 'fw-semibold mb-2';
          hh.textContent = 'Scheduling';
          sub.appendChild(hh);
          for (const k of presentSchedulingKeys) {
            sub.appendChild(_renderConfigField(k, cfg[k]));
          }
          body.appendChild(sub);
        }

        const presentAuthKeys = authKeys.filter(k => Object.prototype.hasOwnProperty.call(cfg, k));
        if (presentAuthKeys.length) {
          for (const k of presentAuthKeys) used.add(k);
          const sub = document.createElement('div');
          sub.className = 'border rounded p-2 mt-2 bg-body-tertiary';
          const hh = document.createElement('div');
          hh.className = 'fw-semibold mb-2';
          hh.textContent = 'Authentication';
          sub.appendChild(hh);
          for (const k of presentAuthKeys) {
            sub.appendChild(_renderConfigField(k, cfg[k]));
          }
          body.appendChild(sub);
        }
      },
    });
  }

  // ProPresenter: render timing-related keys as a nested sub-box.
  const proPresenterPanel = Array.from(panels.querySelectorAll('[data-group-id]'))
    .find(p => String(p.dataset.groupId) === _groupIdFromTitle('ProPresenter'));
  if (proPresenterPanel) {
    const body = proPresenterPanel.querySelector('.card-body');
    const presentTimingKeys = proPresenterTimingKeys.filter(k => Object.prototype.hasOwnProperty.call(cfg, k));
    if (body && presentTimingKeys.length) {
      for (const k of presentTimingKeys) used.add(k);
      const sub = document.createElement('div');
      sub.className = 'border rounded p-2 mt-2 bg-body-tertiary';
      const h = document.createElement('div');
      h.className = 'fw-semibold mb-2';
      h.textContent = 'Timings';
      sub.appendChild(h);
      for (const k of presentTimingKeys) {
        sub.appendChild(_renderConfigField(k, cfg[k]));
      }
      body.appendChild(sub);
    }
  }

  // Everything else (sorted) gets its own section so it's easy to find.
  const otherKeys = Object.keys(cfg || {}).filter(k => !used.has(k) && !hiddenKeys.has(k)).sort();
  if (otherKeys.length) {
    _addGroupPanel({title: 'Other', keys: otherKeys});
  }

  // Activate a section.
  const first = renderedGroups.length ? renderedGroups[0] : null;
  const active = _readActiveGroupId(first);
  if (active && renderedGroups.includes(active)) {
    _setActiveGroupId(active);
  } else if (first) {
    _setActiveGroupId(first);
  }
}

function _readConfigFromUI(originalCfg) {
  const cfg = Object.assign({}, originalCfg || {});
  const inputs = Array.from(document.querySelectorAll('[data-cfg-key]'));

  for (const el of inputs) {
    const key = el.dataset.cfgKey;
    const typ = el.dataset.cfgType || 'string';

    try {
      if (typ === 'boolean') {
        cfg[key] = Boolean(el.checked);
      } else if (typ === 'int') {
        cfg[key] = parseInt(String(el.value || '0'), 10);
      } else if (typ === 'float') {
        cfg[key] = parseFloat(String(el.value || '0'));
      } else if (typ === 'json') {
        const txt = String(el.value || '').trim();
        cfg[key] = txt ? JSON.parse(txt) : null;
      } else {
        cfg[key] = String(el.value || '');
      }
    } catch (e) {
      throw new Error(`Invalid value for ${key}`);
    }
  }

  return cfg;
}

if (document.getElementById('config-page')) {
  let _configOriginal = {};

  // --- Unsaved changes tracking / navigation guard ---
  let _configBaselineJson = '';
  let _configDirty = false;
  let _configDirtyDebounce = null;
  let _configPendingNavUrl = null;
  let _configAllowUnloadOnce = false;

  function _configStableStringify(v) {
    try {
      const seen = new WeakSet();
      const normalize = (x) => {
        if (x === null || x === undefined) return null;
        if (typeof x !== 'object') return x;
        if (seen.has(x)) return null;
        seen.add(x);
        if (Array.isArray(x)) return x.map(normalize);
        const out = {};
        for (const k of Object.keys(x).sort()) {
          out[k] = normalize(x[k]);
        }
        return out;
      };
      return JSON.stringify(normalize(v));
    } catch (e) {
      try {
        return JSON.stringify(v);
      } catch (e2) {
        return '';
      }
    }
  }

  function _configComputeCurrentJson() {
    // If there are invalid fields, treat it as "dirty" so the user gets warned.
    try {
      const cfg = _readConfigFromUI(_configOriginal);
      return _configStableStringify(cfg);
    } catch (e) {
      return null;
    }
  }

  function _configSetDirtyFlagFromUI() {
    const cur = _configComputeCurrentJson();
    const isDirty = (cur === null) ? true : (cur !== _configBaselineJson);
    _configDirty = !!isDirty;
  }

  function _configScheduleDirtyRecalc() {
    if (_configDirtyDebounce) {
      clearTimeout(_configDirtyDebounce);
      _configDirtyDebounce = null;
    }
    _configDirtyDebounce = setTimeout(() => {
      _configDirtyDebounce = null;
      _configSetDirtyFlagFromUI();
    }, 150);
  }

  function _configEnsureUnsavedModal() {
    let modalEl = document.getElementById('config-unsaved-modal');
    if (modalEl) return modalEl;

    modalEl = document.createElement('div');
    modalEl.id = 'config-unsaved-modal';
    modalEl.className = 'modal fade';
    modalEl.tabIndex = -1;
    modalEl.setAttribute('aria-hidden', 'true');
    modalEl.innerHTML = `
      <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Unsaved changes</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body">
            You have unsaved changes in Config.
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-outline-secondary" data-action="cancel" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-outline-danger" data-action="discard">Continue without saving</button>
            <button type="button" class="btn btn-primary" data-action="save">Save and continue</button>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(modalEl);
    return modalEl;
  }

  function _configShowUnsavedPrompt(navigateUrl) {
    _configPendingNavUrl = String(navigateUrl || '').trim() || null;
    const modalEl = _configEnsureUnsavedModal();

    // Bind buttons (idempotent)
    if (!modalEl._tdeckBound) {
      modalEl._tdeckBound = true;
      modalEl.addEventListener('click', async (ev) => {
        const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-action]') : null;
        if (!btn) return;
        const action = String(btn.getAttribute('data-action') || '');
        if (action === 'discard') {
          try {
            const inst = window.bootstrap && window.bootstrap.Modal ? window.bootstrap.Modal.getInstance(modalEl) : null;
            if (inst) inst.hide();
          } catch (e) {}
          if (_configPendingNavUrl) {
            _configAllowUnloadOnce = true;
            setTimeout(() => { _configAllowUnloadOnce = false; }, 2000);
            window.location.href = _configPendingNavUrl;
          }
        } else if (action === 'save') {
          const ok = await _configSaveNow({ showStatus: true });
          if (ok) {
            try {
              const inst = window.bootstrap && window.bootstrap.Modal ? window.bootstrap.Modal.getInstance(modalEl) : null;
              if (inst) inst.hide();
            } catch (e) {}
            if (_configPendingNavUrl) {
              _configAllowUnloadOnce = true;
              setTimeout(() => { _configAllowUnloadOnce = false; }, 2000);
              window.location.href = _configPendingNavUrl;
            }
          }
        }
      });
    }

    try {
      if (window.bootstrap && window.bootstrap.Modal) {
        const m = new window.bootstrap.Modal(modalEl);
        m.show();
      } else {
        // Fallback if Bootstrap isn't loaded for some reason.
        const ok = window.confirm('You have unsaved changes. Leave without saving?');
        if (ok && _configPendingNavUrl) window.location.href = _configPendingNavUrl;
      }
    } catch (e) {
      const ok = window.confirm('You have unsaved changes. Leave without saving?');
      if (ok && _configPendingNavUrl) window.location.href = _configPendingNavUrl;
    }
  }

  // Warn on tab close / reload.
  window.addEventListener('beforeunload', (e) => {
    if (_configAllowUnloadOnce) return;
    try {
      _configSetDirtyFlagFromUI();
    } catch (err) {
      _configDirty = true;
    }
    if (!_configDirty) return;
    e.preventDefault();
    // Most browsers ignore custom text; returning a value triggers the prompt.
    e.returnValue = '';
    return '';
  });

  // Intercept in-app navigation clicks to offer Save/Discard/Cancel.
  document.addEventListener('click', (e) => {
    // Compute just-in-time so fast click-after-edit still prompts.
    try {
      _configSetDirtyFlagFromUI();
    } catch (err) {
      _configDirty = true;
    }
    if (!_configDirty) return;
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    const href = String(a.getAttribute('href') || '').trim();
    if (!href) return;
    // Ignore section hash changes and noop links.
    if (href.startsWith('#')) return;
    if (href.toLowerCase().startsWith('javascript:')) return;
    // Ignore ctrl/cmd clicks and other "open in new tab" behaviors.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;

    // Only guard same-origin navigations.
    try {
      const url = new URL(href, window.location.href);
      if (url.origin !== window.location.origin) return;
      // If it's just changing the hash on the same page, ignore.
      if ((url.pathname === window.location.pathname) && (url.search === window.location.search)) {
        if (url.hash && url.hash.startsWith('#cfg-')) return;
      }
      e.preventDefault();
      _configShowUnsavedPrompt(url.href);
    } catch (err) {
      // If URL parsing fails, don't block navigation.
    }
  }, true);

  (async () => {
    try {
      const res = await fetch('/api/config');
      if (!res.ok) throw new Error('Failed to load config');
      const cfg = await res.json();
      _configOriginal = cfg || {};
      _renderConfigGroups(_configOriginal);

      _configBaselineJson = _configStableStringify(_configOriginal);
      _configDirty = false;
    } catch (e) {
      _configSetStatus(String(e.message || e), 'error');
    }
  })();

  // Track changes as the user edits fields.
  document.addEventListener('input', (e) => {
    const t = e.target;
    if (!t || !t.getAttribute) return;
    if (!t.getAttribute('data-cfg-key')) return;
    _configScheduleDirtyRecalc();
  }, true);
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (!t || !t.getAttribute) return;
    if (!t.getAttribute('data-cfg-key')) return;
    _configSetDirtyFlagFromUI();
  }, true);

  async function _configSaveNow({ showStatus = true } = {}) {
    const saveBtn = document.getElementById('config-save');
    if (saveBtn) saveBtn.disabled = true;
    if (showStatus) _configClearStatus();
    try {
      const cfg = _readConfigFromUI(_configOriginal);
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(cfg),
      });

      // Server may restart if webserver_port changes, so response can fail.
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || 'Save failed');
      }

      _configOriginal = data.config || cfg;
      _renderConfigGroups(_configOriginal);

      _configBaselineJson = _configStableStringify(_configOriginal);
      _configDirty = false;

      // Apply message timeout immediately (no reload required)
      if (window.TDECK_UI && Object.prototype.hasOwnProperty.call(_configOriginal, 'webui_message_timeout_seconds')) {
        const s = Number(_configOriginal.webui_message_timeout_seconds);
        const clampedSeconds = Math.max(0, Math.min(600, Number.isFinite(s) ? s : 0));
        window.TDECK_UI.messageTimeoutMs = Math.floor(clampedSeconds * 1000);
      }

      // Apply theme immediately (no reload required)
      if (Object.prototype.hasOwnProperty.call(_configOriginal, 'dark_mode')) {
        const theme = _configOriginal.dark_mode ? 'dark' : 'light';
        document.documentElement.setAttribute('data-bs-theme', theme);
      }

      if (showStatus) {
        if (data.restart_required && data.port) {
          const proto = window.location.protocol;
          const host = window.location.hostname;
          const newUrl = `${proto}//${host}:${data.port}/config`;
          _configSetStatus(
            `Saved. <strong>Web UI Port changed</strong> — the server will restart on <strong>${data.port}</strong>. ` +
            `Open: <a href="${newUrl}">${newUrl}</a>`,
            'warn'
          );
        } else {
          _configSetStatus('Saved.', 'ok');
        }
      }

      return true;
    } catch (e) {
      if (showStatus) _configSetStatus(String(e.message || e), 'error');
      return false;
    } finally {
      const saveBtn2 = document.getElementById('config-save');
      if (saveBtn2) saveBtn2.disabled = false;
    }
  }

  const saveBtn = document.getElementById('config-save');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      await _configSaveNow({ showStatus: true });
    });
  }
}

// --- Activity Log page ---
function _activityLogSetStatus(msg, kind) {
  const el = document.getElementById('activity-log-status');
  if (!el) return;
  if (!msg) {
    _uiClearAutoHide(el);
    el.className = '';
    el.textContent = '';
    return;
  }
  const cls = kind === 'error' ? 'alert alert-danger' : 'alert alert-success';
  el.className = cls;
  el.textContent = msg;
  _uiScheduleAutoHide(el, () => {
    el.className = '';
    el.textContent = '';
  });
}

if (document.getElementById('activity-log-page')) {
  const bodyEl = document.getElementById('activity-log-body');
  const searchEl = document.getElementById('activity-log-search');
  const sourceEl = document.getElementById('activity-log-source');
  const statusEl = document.getElementById('activity-log-status-filter');
  const rangeEl = document.getElementById('activity-log-range');
  const startEl = document.getElementById('activity-log-start');
  const endEl = document.getElementById('activity-log-end');
  const clearDateTimeBtn = document.getElementById('activity-log-clear-datetime');
  const refreshBtn = document.getElementById('activity-log-refresh');
  const ackAlertsBtn = document.getElementById('activity-log-ack-alerts');
  const alertSummaryEl = document.getElementById('activity-log-alert-summary');
  const firstBtn = document.getElementById('activity-log-first');
  const prevBtn = document.getElementById('activity-log-prev');
  const nextBtn = document.getElementById('activity-log-next');
  const lastBtn = document.getElementById('activity-log-last');
  const pageJumpEl = document.getElementById('activity-log-page-jump');
  const pageInfoEl = document.getElementById('activity-log-page-info');
  const eventsById = new Map();
  const pageSize = 50;
  let currentPage = 1;
  let totalPages = 1;
  let totalRows = 0;
  let liveSince = 0;
  let loading = false;
  let pollInFlight = false;
  let searchTimer = null;

  function _activityStatusClass(status) {
    const s = String(status || '').toLowerCase();
    if (s === 'success') return 'text-bg-success';
    if (s === 'failure') return 'text-bg-danger';
    if (s === 'warning') return 'text-bg-warning';
    return 'text-bg-secondary';
  }

  function _activityRowClass(status) {
    const s = String(status || '').toLowerCase();
    if (s === 'success') return 'activity-log-row activity-log-row-success';
    if (s === 'failure') return 'activity-log-row activity-log-row-failure';
    if (s === 'warning') return 'activity-log-row activity-log-row-warning';
    return 'activity-log-row activity-log-row-info';
  }

  function _activitySourceLabel(source) {
    const s = String(source || 'system');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function _activityDetailsText(ev) {
    if (!ev) return '';
    if (ev.details_json) return String(ev.details_json);
    if (ev.details != null) {
      try { return JSON.stringify(ev.details, null, 2); } catch (e) { return String(ev.details); }
    }
    return '';
  }

  function _activityPad(n) {
    return String(n).padStart(2, '0');
  }

  function _activityDateTimeValue(d) {
    if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '';
    return `${d.getFullYear()}-${_activityPad(d.getMonth() + 1)}-${_activityPad(d.getDate())}T${_activityPad(d.getHours())}:${_activityPad(d.getMinutes())}`;
  }

  function _activityStartOfDay(d) {
    const out = new Date(d);
    out.setHours(0, 0, 0, 0);
    return out;
  }

  function _activityEndOfDay(d) {
    const out = new Date(d);
    out.setHours(23, 59, 0, 0);
    return out;
  }

  function _activityAddDays(d, days) {
    const out = new Date(d);
    out.setDate(out.getDate() + days);
    return out;
  }

  function _activityApplyRangePreset(preset) {
    const now = new Date();
    let start = null;
    let end = null;
    if (preset === 'today') {
      start = _activityStartOfDay(now);
      end = _activityEndOfDay(now);
    } else if (preset === 'yesterday') {
      const y = _activityAddDays(now, -1);
      start = _activityStartOfDay(y);
      end = _activityEndOfDay(y);
    } else if (preset === 'last7') {
      start = _activityStartOfDay(_activityAddDays(now, -6));
      end = _activityEndOfDay(now);
    } else if (preset === 'last30') {
      start = _activityStartOfDay(_activityAddDays(now, -29));
      end = _activityEndOfDay(now);
    } else if (preset === 'thisMonth') {
      start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
      end = _activityEndOfDay(now);
    } else if (preset === 'lastMonth') {
      start = new Date(now.getFullYear(), now.getMonth() - 1, 1, 0, 0, 0, 0);
      end = new Date(now.getFullYear(), now.getMonth(), 0, 23, 59, 0, 0);
    } else if (preset === 'custom') {
      return;
    }
    if (startEl) startEl.value = start ? _activityDateTimeValue(start) : '';
    if (endEl) endEl.value = end ? _activityDateTimeValue(end) : '';
  }

  function _activityClearDateTimeFilter() {
    if (rangeEl) rangeEl.value = '';
    if (startEl) startEl.value = '';
    if (endEl) endEl.value = '';
    _activityLoad({reset: true});
  }

  function _activityUpdateAlertControls(data) {
    const count = Number((data && data.count) || 0);
    const failures = Number((data && data.failures) || 0);
    const warnings = Number((data && data.warnings) || 0);
    if (ackAlertsBtn) {
      ackAlertsBtn.classList.toggle('d-none', count <= 0);
      ackAlertsBtn.disabled = count <= 0;
      ackAlertsBtn.classList.remove('btn-outline-secondary', 'btn-warning', 'btn-danger');
      if (count > 0) {
        ackAlertsBtn.classList.add(failures > 0 ? 'btn-danger' : 'btn-warning');
        ackAlertsBtn.textContent = `Mark ${count} Alert${count === 1 ? '' : 's'} Seen`;
      } else {
        ackAlertsBtn.classList.add('btn-outline-secondary');
        ackAlertsBtn.textContent = 'Mark Warnings/Errors Seen';
      }
    }
    if (alertSummaryEl) {
      if (count > 0) {
        const parts = [];
        if (failures) parts.push(`${failures} failure${failures === 1 ? '' : 's'}`);
        if (warnings) parts.push(`${warnings} warning${warnings === 1 ? '' : 's'}`);
        alertSummaryEl.textContent = `Unseen: ${parts.join(', ')}`;
      } else {
        alertSummaryEl.textContent = '';
      }
    }
  }

  async function _activityRefreshAlerts() {
    const data = await updateActivityLogAlertBadge();
    _activityUpdateAlertControls(data);
  }

  async function _activityAcknowledgeAlerts() {
    if (!ackAlertsBtn) return;
    ackAlertsBtn.disabled = true;
    try {
      const res = await fetch('/api/activity-log/alerts/acknowledge', {method: 'POST', cache: 'no-store'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data || !data.ok) throw new Error((data && data.error) || 'Failed to acknowledge log alerts');
      _activityUpdateAlertControls(data);
      await updateActivityLogAlertBadge();
      _activityLogSetStatus('Warnings/errors marked as seen.', 'ok');
    } catch (e) {
      _activityLogSetStatus(String(e.message || e), 'error');
    } finally {
      await _activityRefreshAlerts();
    }
  }

  function _activityUpdatePager() {
    totalPages = Math.max(1, Number(totalPages || 1));
    currentPage = Math.min(Math.max(1, Number(currentPage || 1)), totalPages);
    if (pageJumpEl) {
      pageJumpEl.max = String(totalPages);
      pageJumpEl.value = String(currentPage);
      pageJumpEl.disabled = loading || totalPages <= 1;
    }
    if (pageInfoEl) {
      pageInfoEl.textContent = `of ${totalPages} (${totalRows} logs)`;
    }
    if (firstBtn) firstBtn.disabled = loading || currentPage <= 1;
    if (prevBtn) prevBtn.disabled = loading || currentPage <= 1;
    if (nextBtn) nextBtn.disabled = loading || currentPage >= totalPages;
    if (lastBtn) lastBtn.disabled = loading || currentPage >= totalPages;
  }

  function _activityGoToPage(page) {
    const nextPage = Math.min(Math.max(1, Number(page || 1)), Math.max(1, totalPages));
    if (nextPage === currentPage && !loading) {
      _activityUpdatePager();
      return;
    }
    currentPage = nextPage;
    _activityLoad();
  }

  function _activityRender() {
    if (!bodyEl) return;
    const events = Array.from(eventsById.values()).sort((a, b) => Number(b.id || 0) - Number(a.id || 0));
    bodyEl.textContent = '';
    if (!events.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 5;
      td.className = 'text-muted small p-3';
      td.textContent = 'No activity recorded.';
      tr.appendChild(td);
      bodyEl.appendChild(tr);
      return;
    }
    events.forEach((ev) => {
      const tr = document.createElement('tr');
      tr.className = _activityRowClass(ev.status);

      const timeTd = document.createElement('td');
      timeTd.className = 'small text-nowrap';
      timeTd.textContent = String(ev.ts || '');
      tr.appendChild(timeTd);

      const actorTd = document.createElement('td');
      actorTd.className = 'activity-log-actor';
      const actor = document.createElement('div');
      actor.className = 'fw-semibold text-truncate activity-log-actor-name';
      actor.textContent = String(ev.actor_display || ev.actor_username || 'System');
      actorTd.appendChild(actor);
      if (ev.ip) {
        const ip = document.createElement('div');
        ip.className = 'text-muted text-truncate activity-log-meta';
        ip.textContent = String(ev.ip);
        actorTd.appendChild(ip);
      }
      tr.appendChild(actorTd);

      const summaryTd = document.createElement('td');
      const summary = document.createElement('div');
      summary.className = 'activity-log-summary';
      summary.textContent = String(ev.summary || ev.action || '');
      summaryTd.appendChild(summary);
      const meta = document.createElement('div');
      meta.className = 'text-muted activity-log-meta';
      const target = [ev.target_type, ev.target_id].filter(Boolean).join(' ');
      meta.textContent = [String(ev.action || ''), target, String(ev.request_path || '')].filter(Boolean).join(' | ');
      summaryTd.appendChild(meta);
      const details = _activityDetailsText(ev);
      if (details) {
        const detailEl = document.createElement('details');
        detailEl.className = 'mt-1 activity-log-details';
        const detailSummary = document.createElement('summary');
        detailSummary.textContent = 'Details';
        const pre = document.createElement('pre');
        pre.className = 'border rounded p-2 mt-1 mb-0 bg-body-tertiary activity-log-pre';
        pre.style.whiteSpace = 'pre-wrap';
        pre.textContent = details;
        detailEl.appendChild(detailSummary);
        detailEl.appendChild(pre);
        summaryTd.appendChild(detailEl);
      }
      tr.appendChild(summaryTd);

      const sourceTd = document.createElement('td');
      sourceTd.className = 'small';
      sourceTd.textContent = _activitySourceLabel(ev.source);
      tr.appendChild(sourceTd);

      const statusTd = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `badge ${_activityStatusClass(ev.status)}`;
      badge.textContent = _activitySourceLabel(ev.status || 'info');
      statusTd.appendChild(badge);
      tr.appendChild(statusTd);

      bodyEl.appendChild(tr);
    });
    _activityUpdatePager();
  }

  function _activityParams() {
    const params = new URLSearchParams();
    params.set('limit', String(pageSize));
    params.set('page', String(currentPage));
    const q = String((searchEl && searchEl.value) || '').trim();
    const source = String((sourceEl && sourceEl.value) || '').trim();
    const status = String((statusEl && statusEl.value) || '').trim();
    const start = String((startEl && startEl.value) || '').trim();
    const end = String((endEl && endEl.value) || '').trim();
    if (q) params.set('q', q);
    if (source) params.set('source', source);
    if (status) params.set('status', status);
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    return params;
  }

  async function _activityLoad({reset = false} = {}) {
    if (loading) return;
    loading = true;
    _activityUpdatePager();
    if (reset) {
      currentPage = 1;
      liveSince = 0;
    }
    eventsById.clear();
    _activityRender();
    try {
      const params = _activityParams();
      const res = await fetch(`/api/activity-log?${params.toString()}`, {cache: 'no-store'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || 'Failed to load activity log');
      (data.events || []).forEach((ev) => {
        if (ev && ev.id != null) eventsById.set(Number(ev.id), ev);
      });
      currentPage = Number(data.page || currentPage) || 1;
      totalPages = Number(data.total_pages || 1) || 1;
      totalRows = Number(data.total || 0) || 0;
      const maxId = Math.max(...Array.from(eventsById.keys()), 0);
      if (maxId > liveSince) liveSince = maxId;
      _activityRender();
      _activityLogSetStatus('', 'ok');
    } catch (e) {
      _activityLogSetStatus(String(e.message || e), 'error');
    } finally {
      loading = false;
      _activityUpdatePager();
    }
  }

  async function _activityPoll() {
    if (pollInFlight || currentPage !== 1) return;
    pollInFlight = true;
    try {
      const params = _activityParams();
      params.set('since', String(liveSince));
      params.delete('limit');
      params.delete('page');
      const res = await fetch(`/api/activity-log/live?${params.toString()}`, {cache: 'no-store'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || 'Failed to load live activity');
      let changed = false;
      (data.events || []).forEach((ev) => {
        if (ev && ev.id != null) {
          eventsById.set(Number(ev.id), ev);
          changed = true;
        }
      });
      liveSince = Number(data.next || liveSince) || liveSince;
      if (changed) {
        const sortedIds = Array.from(eventsById.keys()).sort((a, b) => b - a);
        sortedIds.slice(pageSize).forEach((id) => eventsById.delete(id));
        totalRows += (data.events || []).length;
        totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
        _activityRender();
      }
    } catch (e) {
      _activityLogSetStatus(String(e.message || e), 'error');
    } finally {
      pollInFlight = false;
    }
  }

  function _activityScheduleReload() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => _activityLoad({reset: true}), 250);
  }

  if (refreshBtn) refreshBtn.addEventListener('click', () => _activityLoad({reset: true}));
  if (ackAlertsBtn) ackAlertsBtn.addEventListener('click', _activityAcknowledgeAlerts);
  if (firstBtn) firstBtn.addEventListener('click', () => _activityGoToPage(1));
  if (prevBtn) prevBtn.addEventListener('click', () => _activityGoToPage(currentPage - 1));
  if (nextBtn) nextBtn.addEventListener('click', () => _activityGoToPage(currentPage + 1));
  if (lastBtn) lastBtn.addEventListener('click', () => _activityGoToPage(totalPages));
  if (pageJumpEl) {
    pageJumpEl.addEventListener('change', () => _activityGoToPage(pageJumpEl.value));
    pageJumpEl.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        _activityGoToPage(pageJumpEl.value);
      }
    });
  }
  if (searchEl) searchEl.addEventListener('input', _activityScheduleReload);
  if (sourceEl) sourceEl.addEventListener('change', () => _activityLoad({reset: true}));
  if (statusEl) statusEl.addEventListener('change', () => _activityLoad({reset: true}));
  if (rangeEl) rangeEl.addEventListener('change', () => {
    _activityApplyRangePreset(String(rangeEl.value || ''));
    _activityLoad({reset: true});
  });
  if (clearDateTimeBtn) clearDateTimeBtn.addEventListener('click', _activityClearDateTimeFilter);
  [startEl, endEl].forEach((el) => {
    if (!el) return;
    el.addEventListener('change', () => {
      if (rangeEl) rangeEl.value = 'custom';
      _activityLoad({reset: true});
    });
  });

  _activityUpdatePager();
  _activityRefreshAlerts();
  _activityLoad({reset: true});
  setInterval(_activityPoll, 2000);
  setInterval(_activityRefreshAlerts, 15000);
}

// --- Timers page ---
let _timersButtonTemplates = [];
let _timersLastSavedPresets = null;
let _timersLastSavedStagePreset = 0;
let _timersMutationQueue = [];
let _timersDrainPromise = null;
let _timersEditVersion = 0;
const _timersIncompleteTimeRestoreDelayMs = 4000;
const _timersIncompleteTimeRestoreHandles = new WeakMap();

function _timersSetStatus(msg, kind) {
  const el = document.getElementById('timers-status');
  if (!el) return;
  // Timers page: only show error messages.
  if (kind !== 'error') {
    _timersClearStatus();
    return;
  }
  el.className = 'alert alert-danger';
  el.textContent = msg;
  _uiScheduleAutoHide(el, () => {
    _timersClearStatus();
  });
}

function _timersClearStatus() {
  const el = document.getElementById('timers-status');
  if (!el) return;
  _uiClearAutoHide(el);
  el.className = '';
  el.textContent = '';
}

function _timersSetLastSaved(presets, stagePreset) {
  // Deep-copy to keep it immutable.
  try {
    _timersLastSavedPresets = JSON.parse(JSON.stringify(presets || []));
  } catch (e) {
    _timersLastSavedPresets = Array.isArray(presets) ? presets.slice() : [];
  }
  const n = Number(stagePreset);
  _timersLastSavedStagePreset = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function _timersClearIncompleteTimeRestore(input) {
  if (!input) return;
  const handle = _timersIncompleteTimeRestoreHandles.get(input);
  if (handle) {
    clearTimeout(handle);
    _timersIncompleteTimeRestoreHandles.delete(input);
  }
}

function _timersResolvePreviousCompleteTime(input) {
  const explicit = _timersNormalizeTimeForInput(input && input.dataset && input.dataset.lastCompleteTime);
  if (explicit) return explicit;
  const tr = input ? input.closest('tr') : null;
  const idx = tr ? Number(tr.dataset.index) : NaN;
  if (Number.isFinite(idx) && _timersLastSavedPresets && _timersLastSavedPresets[idx]) {
    const saved = _timersNormalizeTimeForInput(_timersLastSavedPresets[idx].time);
    if (saved) return saved;
  }
  return '00:00';
}

function _timersRememberCompleteTime(input) {
  if (!input) return;
  const value = String(input.value || '').trim();
  if (!value) return;
  input.dataset.lastCompleteTime = value;
  _timersClearIncompleteTimeRestore(input);
}

function _timersScheduleIncompleteTimeRestore(input) {
  if (!input) return;
  _timersClearIncompleteTimeRestore(input);
  const handle = setTimeout(() => {
    _timersIncompleteTimeRestoreHandles.delete(input);
    if (_timersRestoreIncompletePresetTimeNow(input)) {
      _timersScheduleAutoSave({delayMs: 0, showStatus: false});
    }
  }, _timersIncompleteTimeRestoreDelayMs);
  _timersIncompleteTimeRestoreHandles.set(input, handle);
}

function _timersRestoreIncompletePresetTimeNow(input) {
  if (!input) return false;
  _timersClearIncompleteTimeRestore(input);
  if (!document.body || !document.body.contains(input)) return false;
  if (String(input.value || '').trim()) return false;
  const restored = _timersResolvePreviousCompleteTime(input);
  input.value = restored;
  if (input.value !== restored) {
    input.value = '00:00';
  }
  _timersRememberCompleteTime(input);
  return true;
}

function _timersEnsureCompletePresetTimes() {
  let restored = false;
  while (true) {
    const input = _timersFindIncompletePresetTimeInput();
    if (!input) break;
    if (!_timersRestoreIncompletePresetTimeNow(input)) break;
    restored = true;
  }
  if (restored) {
    _timersScheduleAutoSave({delayMs: 0, showStatus: false});
  }
  return restored;
}

function _timersFindIncompletePresetTimeInput() {
  const body = document.getElementById('timers-presets-body');
  if (!body) return null;
  const inputs = Array.from(body.querySelectorAll('input[data-role="preset-time"]'));
  for (const input of inputs) {
    if (!String(input.value || '').trim()) {
      return input;
    }
  }
  return null;
}

function _timersHasIncompletePresetTime() {
  return !!_timersFindIncompletePresetTimeInput();
}

function _timersNormalizeTimeForInput(value) {
  const s = String(value || '').trim();
  const m = s.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
  if (!m) return '';
  const h = parseInt(m[1], 10);
  const min = parseInt(m[2], 10);
  if (!Number.isFinite(h) || !Number.isFinite(min) || h < 0 || h > 23 || min < 0 || min > 59) {
    return '';
  }
  return `${String(h).padStart(2, '0')}:${String(min).padStart(2, '0')}`;
}

function _timersParseDurationMinutes(value) {
  const raw = String(value || '').trim().toLowerCase().replace(/,/g, ' ');
  if (!raw) return {ok: false, error: 'Enter an amount of time first.'};
  const compact = raw.replace(/\s+/g, '');

  let minutes = null;
  if (/^\d+$/.test(compact)) {
    minutes = parseInt(compact, 10);
  } else {
    const hm = compact.match(/^(\d+):(\d{1,2})$/);
    if (hm) {
      const hours = parseInt(hm[1], 10);
      const mins = parseInt(hm[2], 10);
      if (mins > 59) return {ok: false, error: 'For H:MM, minutes must be 0-59.'};
      minutes = (hours * 60) + mins;
    } else {
      const tokenRe = /(\d+)(hours|hour|hrs|hr|h|minutes|minute|mins|min|m)/g;
      let pos = 0;
      let hours = 0;
      let mins = 0;
      let match = null;
      let sawToken = false;
      while ((match = tokenRe.exec(compact)) !== null) {
        if (match.index !== pos) {
          return {ok: false, error: 'Use minutes, 15m, 1h 30m, or 1:30.'};
        }
        const amount = parseInt(match[1], 10);
        if (String(match[2] || '').startsWith('h')) hours += amount;
        else mins += amount;
        sawToken = true;
        pos = tokenRe.lastIndex;
      }
      if (!sawToken || pos !== compact.length) {
        return {ok: false, error: 'Use minutes, 15m, 1h 30m, or 1:30.'};
      }
      minutes = (hours * 60) + mins;
    }
  }

  if (!Number.isFinite(minutes) || minutes <= 0) {
    return {ok: false, error: 'Amount must be greater than 0 minutes.'};
  }
  if (minutes > 24 * 60) {
    return {ok: false, error: 'Amount must be 24 hours or less.'};
  }
  return {ok: true, minutes};
}

async function _timersAdjustAllPresets(sign) {
  const input = document.getElementById('timers-bulk-adjust-amount');
  const parsed = _timersParseDurationMinutes(input ? input.value : '');
  if (!parsed.ok) {
    _timersSetStatus(parsed.error || 'Invalid duration.', 'error');
    if (input) input.focus();
    return false;
  }

  if (_timersHasIncompletePresetTime()) {
    _timersEnsureCompletePresetTimes();
  }
  const ok = await _timersFlushAutoSave({showStatus: false});
  if (!ok) return false;

  const delta = (sign < 0 ? -1 : 1) * parsed.minutes;
  const result = await _timersQueueMutation(
    {action: 'adjust_all_presets', delta_minutes: delta},
    {render: true}
  );
  return !!(!result || result.ok !== false);
}

function _timersApplyMutationResponse(data, {render = false} = {}) {
  const presets = Array.isArray(data && data.timer_presets) ? data.timer_presets : _timersReadPresetsFromUI();
  const stagePreset = Number((data && data.stream_start_preset) ?? _timersReadStagePresetFromUI() ?? 0);
  _timersSetLastSaved(presets, stagePreset);
  if (render) {
    _timersRenderPresets(presets);
    _timersApplyStagePresetId(stagePreset);
    return;
  }
  _timersStageRenderOptions(presets);
  _timersStageSetPreview(_timersReadStagePresetFromUI(), presets);
  _timersStageSetButtonsEnabled(_timersReadStagePresetFromUI() > 0 && _timersReadStagePresetFromUI() <= presets.length);
}

function _timersQueueMutation(payload, {render = false, row = null, version = null} = {}) {
  const item = {payload, render, row, version};
  if (payload && payload.action === 'update_preset' && !payload.apply) {
    for (let i = _timersMutationQueue.length - 1; i >= 0; i -= 1) {
      const queued = _timersMutationQueue[i];
      if (
        queued &&
        queued.payload &&
        queued.payload.action === 'update_preset' &&
        queued.payload.preset === payload.preset &&
        !queued.payload.apply
      ) {
        queued.payload.patch = Object.assign({}, queued.payload.patch || {}, payload.patch || {});
        queued.row = row || queued.row;
        queued.version = version || queued.version;
        queued.render = queued.render || render;
        return _timersDrainMutationQueue();
      }
    }
  }
  _timersMutationQueue.push(item);
  return _timersDrainMutationQueue();
}

function _timersClearRowSaveTimer(tr) {
  if (tr && tr.__timerSaveHandle) {
    clearTimeout(tr.__timerSaveHandle);
    tr.__timerSaveHandle = null;
  }
}

function _timersReadPresetFromRow(tr) {
  if (!tr) return null;
  const timeInp = tr.querySelector('input[data-role="preset-time"]');
  const nameInp = tr.querySelector('input[data-role="preset-name"]');
  const time = String((timeInp && timeInp.value) || '').trim();
  if (!time) return null;
  const name = String((nameInp && nameInp.value) || '').trim();
  const obj = {time, name};

  const list = tr.querySelector('[data-role="presses-list"]');
  const pressesOut = [];
  if (list) {
    const pressRows = Array.from(list.querySelectorAll('[data-role="press-row"]'));
    for (const pr of pressRows) {
      const sel = pr.querySelector('select[data-role="press-template"]');
      const inp = pr.querySelector('input[data-role="press-url"]');
      let url = '';
      if (sel && String(sel.value || '') !== '') {
        url = _timersTemplateToURL(sel.value);
      } else {
        url = String((inp && inp.value) || '').trim();
      }
      const norm = _timersNormalizeButtonURL(url);
      if (!norm) continue;
      if (norm === '__INVALID__') {
        throw new Error(`Invalid button URL: ${url}. Use '1/2/3' or 'location/1/2/3/press'.`);
      }
      pressesOut.push({buttonURL: norm});
    }
  }
  obj.button_presses = pressesOut;
  return obj;
}

function _timersQueueRowUpdate(tr) {
  if (!tr) return Promise.resolve({ok: true, skipped: true});
  _timersClearRowSaveTimer(tr);
  const preset = Number(tr.dataset.index) + 1;
  if (!Number.isFinite(preset) || preset < 1) return Promise.resolve({ok: true, skipped: true});
  let patch = null;
  try {
    patch = _timersReadPresetFromRow(tr);
  } catch (e) {
    _timersSetStatus(String(e.message || e), 'error');
    return Promise.resolve({ok: false, error: String(e.message || e)});
  }
  if (!patch) {
    return Promise.resolve({ok: true, skipped: true, reason: 'incomplete-time'});
  }
  const version = String((tr.dataset && tr.dataset.dirtyVersion) || '');
  return _timersQueueMutation(
    {action: 'update_preset', preset, patch},
    {row: tr, version}
  );
}

function _timersMarkRowDirty(tr, {delayMs = 700} = {}) {
  if (!tr) return;
  tr.dataset.dirty = '1';
  tr.dataset.dirtyVersion = String(++_timersEditVersion);
  _timersClearRowSaveTimer(tr);
  tr.__timerSaveHandle = setTimeout(() => {
    tr.__timerSaveHandle = null;
    _timersQueueRowUpdate(tr);
  }, Math.max(0, Number(delayMs) || 0));
}

function _timersScheduleAutoSave({delayMs = 700, showStatus = false} = {}) {
  const body = document.getElementById('timers-presets-body');
  if (!body) return;
  const rows = Array.from(body.querySelectorAll('tr[data-dirty="1"]'));
  rows.forEach((tr) => _timersMarkRowDirty(tr, {delayMs}));
  if (showStatus) _timersClearStatus();
}

function _timersDrainMutationQueue() {
  if (_timersDrainPromise) return _timersDrainPromise;
  _timersDrainPromise = (async () => {
    while (_timersMutationQueue.length) {
      const item = _timersMutationQueue.shift();
      try {
        const res = await fetch('/api/timers/mutate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(item.payload || {}),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          throw new Error(data.error || 'Save failed');
        }
        if (item.row && item.version && item.row.dataset.dirtyVersion === String(item.version)) {
          delete item.row.dataset.dirty;
        }
        _timersApplyMutationResponse(data, {render: !!item.render});
        _timersClearStatus();
      } catch (e) {
        if (item.row) item.row.dataset.dirty = '1';
        _timersSetStatus(String(e.message || e), 'error');
        return {ok: false, error: String(e.message || e)};
      }
    }
    return {ok: true};
  })().finally(() => {
    _timersDrainPromise = null;
  });
  return _timersDrainPromise;
}

async function _timersFlushAutoSave({showStatus = true} = {}) {
  const body = document.getElementById('timers-presets-body');
  if (body) {
    const rows = Array.from(body.querySelectorAll('tr[data-dirty="1"]'));
    for (const tr of rows) {
      _timersClearRowSaveTimer(tr);
      const r = await _timersQueueRowUpdate(tr);
      if (r && r.ok === false) return false;
    }
  }
  const r = await _timersDrainMutationQueue();
  if (showStatus && r && r.ok === false) {
    _timersSetStatus(r.error || 'Save failed', 'error');
  }
  return !!(!r || r.ok !== false);
}

async function _timersApplyPreset(presetNumber) {
  const res = await fetch('/api/timers/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({preset: presetNumber}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || 'Apply failed');
  }

  // The server may return ok=true with an 'error' describing ProPresenter problems.
  if (data.error) {
    _timersSetStatus(String(data.error), 'error');
  } else {
    _timersSetStatus('', 'ok');
  }
  return data;
}

function _timersNormalizeButtonURL(buttonURL) {
  let s = String(buttonURL || '').trim();
  if (!s) return '';
  if (/^location\/\d+\/\d+\/\d+\/press$/.test(s)) return s;
  if (/^\d+\/\d+\/\d+$/.test(s)) return `location/${s}/press`;
  return '__INVALID__';
}

function _timersButtonURLToDisplay(buttonURL) {
  const s = String(buttonURL || '').trim();
  const m = s.match(/^location\/(\d+\/\d+\/\d+)\/press$/);
  if (m && m[1]) return m[1];
  return s;
}

function _timersTemplateToURL(idx) {
  const i = Number(idx);
  if (!Number.isFinite(i)) return '';
  const t = _timersButtonTemplates[i];
  if (!t) return '';
  const url = t.buttonURL || (t.pattern ? `location/${t.pattern}/press` : '');
  return String(url || '').trim();
}

function _timersNewPressRow(pressObj) {
  const row = document.createElement('div');
  row.className = 'timer-press-row mb-2';
  row.dataset.role = 'press-row';

  const sel = document.createElement('select');
  sel.className = 'form-select form-select-sm';
  // Keep select and input the same size
  sel.style.flex = '1 1 0';
  sel.style.minWidth = '0';
  sel.dataset.role = 'press-template';

  const optNone = document.createElement('option');
  optNone.value = '';
  optNone.textContent = '(custom)';
  sel.appendChild(optNone);
  _timersButtonTemplates.forEach((b, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = `${b.label || 'Template'} — ${b.pattern || ''}`.trim();
    sel.appendChild(opt);
  });

  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.className = 'form-control form-control-sm';
  urlInput.placeholder = 'button URL (e.g. 1/0/1 or location/1/0/1/press)';
  urlInput.dataset.role = 'press-url';

  const upBtn = document.createElement('button');
  upBtn.className = 'btn btn-sm btn-outline-secondary btn-icon-sm';
  upBtn.textContent = '▲';
  upBtn.title = 'Move up';
  upBtn.setAttribute('aria-label', 'Move up');
  upBtn.dataset.pressAction = 'up';

  const downBtn = document.createElement('button');
  downBtn.className = 'btn btn-sm btn-outline-secondary btn-icon-sm';
  downBtn.textContent = '▼';
  downBtn.title = 'Move down';
  downBtn.setAttribute('aria-label', 'Move down');
  downBtn.dataset.pressAction = 'down';

  const moveWrap = document.createElement('div');
  moveWrap.className = 'd-flex flex-column align-items-center gap-1';
  moveWrap.appendChild(upBtn);
  moveWrap.appendChild(downBtn);

  const delBtn = document.createElement('button');
  delBtn.className = 'btn btn-sm btn-outline-danger';
  delBtn.textContent = 'Remove';
  delBtn.dataset.pressAction = 'delete';

  row.appendChild(sel);
  row.appendChild(urlInput);
  row.appendChild(moveWrap);
  row.appendChild(delBtn);

  // Populate from existing data
  const rawUrl = pressObj && typeof pressObj === 'object' ? (pressObj.buttonURL || pressObj.url || '') : String(pressObj || '');
  const normalized = _timersNormalizeButtonURL(rawUrl);
  // Try to match a template
  let matched = false;
  if (normalized && normalized !== '__INVALID__') {
    _timersButtonTemplates.forEach((b, i) => {
      if (matched) return;
      const u = String(b.buttonURL || (b.pattern ? `location/${b.pattern}/press` : '')).trim();
      if (u && u === normalized) {
        sel.value = String(i);
        // Show short form in the input (but keep it disabled)
        urlInput.value = String(b.pattern || _timersButtonURLToDisplay(u) || '');
        urlInput.disabled = true;
        matched = true;
      }
    });
  }
  if (!matched) {
    sel.value = '';
    // For custom entries, prefer short display like 1/0/1
    urlInput.value = normalized === '__INVALID__'
      ? String(rawUrl || '')
      : _timersButtonURLToDisplay(normalized || '');
    urlInput.disabled = false;
  }

  return row;
}

function _timersUpdatePressSummary(tr) {
  const summary = tr.querySelector('[data-role="presses-summary"]');
  const list = tr.querySelector('[data-role="presses-list"]');
  if (!summary || !list) return;
  const count = list.querySelectorAll('[data-role="press-row"]').length;
  summary.textContent = `${count} press${count === 1 ? '' : 'es'}`;
}

function _timersWirePressesDetailsAnimation(detailsEl) {
  if (!detailsEl) return;
  const summaryEl = detailsEl.querySelector('summary');
  const bodyEl = detailsEl.querySelector('.timer-presses-body');
  if (!summaryEl || !bodyEl) return;

  // Avoid double-binding if re-rendered.
  if (detailsEl.__animatedBound) return;
  detailsEl.__animatedBound = true;

  summaryEl.addEventListener('click', (ev) => {
    // We'll manage open/close ourselves to allow animation.
    ev.preventDefault();

    const durationMs = 180;
    const isOpen = detailsEl.hasAttribute('open');

    // Clear any previous transitionend handler by cloning if needed
    // (keep it simple: use {once:true} handlers below).

    if (!isOpen) {
      detailsEl.setAttribute('open', '');

      // Start collapsed
      bodyEl.style.overflow = 'hidden';
      bodyEl.style.maxHeight = '0px';
      bodyEl.style.opacity = '0';
      bodyEl.style.transition = `max-height ${durationMs}ms ease, opacity ${durationMs}ms ease`;

      requestAnimationFrame(() => {
        const h = bodyEl.scrollHeight;
        bodyEl.style.maxHeight = `${h}px`;
        bodyEl.style.opacity = '1';
      });

      const onEndOpen = (e) => {
        if (e.propertyName !== 'max-height') return;
        bodyEl.removeEventListener('transitionend', onEndOpen);
        // Let it size naturally after opening.
        bodyEl.style.transition = '';
        bodyEl.style.maxHeight = '';
        bodyEl.style.opacity = '';
        bodyEl.style.overflow = '';
      };
      bodyEl.addEventListener('transitionend', onEndOpen);
    } else {
      // Animate closed, then remove [open]
      const h = bodyEl.scrollHeight;
      bodyEl.style.overflow = 'hidden';
      bodyEl.style.maxHeight = `${h}px`;
      bodyEl.style.opacity = '1';
      bodyEl.style.transition = `max-height ${durationMs}ms ease, opacity ${durationMs}ms ease`;

      requestAnimationFrame(() => {
        bodyEl.style.maxHeight = '0px';
        bodyEl.style.opacity = '0';
      });

      const onEndClose = (e) => {
        if (e.propertyName !== 'max-height') return;
        bodyEl.removeEventListener('transitionend', onEndClose);
        detailsEl.removeAttribute('open');
        bodyEl.style.transition = '';
        bodyEl.style.maxHeight = '';
        bodyEl.style.opacity = '';
        bodyEl.style.overflow = '';
      };
      bodyEl.addEventListener('transitionend', onEndClose);
    }
  });
}

function _timersStageFormatTime(timeStr) {
  const s = String(timeStr || '').trim();
  if (!/^\d{2}:\d{2}$/.test(s)) return '';
  const parts = s.split(':');
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return '';
  const hour = ((h % 12) === 0) ? 12 : (h % 12);
  const suffix = h < 12 ? 'AM' : 'PM';
  return `${hour}:${String(m).padStart(2, '0')}${suffix}`;
}

function _timersStageMessageForPreset(preset) {
  if (!preset) return '';
  const pretty = _timersStageFormatTime(preset.time);
  if (!pretty) return '';
  return `STREAM ${pretty}`;
}

function _timersReadPresetsForStage() {
  const body = document.getElementById('timers-presets-body');
  if (!body) return [];
  const rows = Array.from(body.querySelectorAll('tr'));
  const out = [];
  rows.forEach((r) => {
    const timeInp = r.querySelector('input[data-role="preset-time"]');
    const nameInp = r.querySelector('input[data-role="preset-name"]');
    const time = String((timeInp && timeInp.value) || '').trim();
    if (!time) return;
    const name = String((nameInp && nameInp.value) || '').trim();
    out.push({time, name});
  });
  return out;
}

function _timersStagePresetLabel(preset, id) {
  const time = String((preset && preset.time) || '').trim();
  const name = String((preset && preset.name) || '').trim();
  const label = name && name !== time ? `${name} (${time})` : time;
  return `${id} - ${label}`.trim();
}

function _timersStageRenderOptions(presets) {
  const list = document.getElementById('timers-stage-preset-list');
  if (!list) return;
  list.innerHTML = '';
  (presets || []).forEach((p, idx) => {
    const opt = document.createElement('option');
    opt.value = _timersStagePresetLabel(p, idx + 1);
    list.appendChild(opt);
  });
}

function _timersStageResolvePresetId(raw, presets) {
  const s = String(raw || '').trim();
  if (!s) return 0;
  const m = s.match(/^(\d+)/);
  if (m) {
    const id = parseInt(m[1], 10);
    if (Number.isFinite(id) && id >= 1 && id <= (presets || []).length) return id;
  }
  const lower = s.toLowerCase();
  const matches = [];
  (presets || []).forEach((p, idx) => {
    const name = String((p && p.name) || '').trim().toLowerCase();
    const time = String((p && p.time) || '').trim().toLowerCase();
    if ((name && name === lower) || (!name && time === lower) || time === lower) {
      matches.push(idx + 1);
    }
  });
  if (matches.length === 1) return matches[0];
  return 0;
}

function _timersReadStagePresetFromUI() {
  const hidden = document.getElementById('timers-stage-preset-id');
  if (!hidden) return 0;
  const n = parseInt(String(hidden.value || ''), 10);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function _timersStageSetHint(msg, kind) {
  const el = document.getElementById('timers-stage-hint');
  if (!el) return;
  if (!msg) {
    el.textContent = '';
    el.className = 'form-text text-muted';
    return;
  }
  el.textContent = String(msg || '');
  if (kind === 'error') {
    el.className = 'form-text text-danger';
  } else if (kind === 'warn') {
    el.className = 'form-text text-warning';
  } else {
    el.className = 'form-text text-muted';
  }
}

function _timersStageSetPreview(presetId, presets) {
  const el = document.getElementById('timers-stage-preview');
  if (!el) return;
  const id = Number(presetId) || 0;
  if (id < 1 || id > (presets || []).length) {
    el.textContent = 'STREAM 9:30AM';
    return;
  }
  const msg = _timersStageMessageForPreset(presets[id - 1]);
  el.textContent = msg || 'STREAM 9:30AM';
}

function _timersStageSetButtonsEnabled(enabled) {
  const btn = document.getElementById('timers-stage-send');
  if (btn) btn.disabled = !enabled;
}

function _timersApplyStagePresetId(presetId) {
  const input = document.getElementById('timers-stage-preset');
  const hidden = document.getElementById('timers-stage-preset-id');
  const presets = _timersReadPresetsForStage();
  const id = Number(presetId);
  const value = Number.isFinite(id) && id > 0 ? Math.floor(id) : 0;
  if (hidden) hidden.value = value > 0 ? String(value) : '';
  if (input) {
    if (value > 0 && value <= presets.length) {
      input.value = _timersStagePresetLabel(presets[value - 1], value);
    } else if (!value) {
      input.value = '';
    }
  }
  _timersStageSetHint(value ? '' : 'No stream-start preset selected.', value ? '' : 'warn');
  _timersStageSetPreview(value, presets);
  _timersStageSetButtonsEnabled(value > 0 && value <= presets.length);
}

function _timersSaveStagePresetId(presetId) {
  const id = Number(presetId);
  const value = Number.isFinite(id) && id > 0 ? Math.floor(id) : 0;
  return _timersQueueMutation({
    action: 'set_stream_start_preset',
    stream_start_preset: value,
  });
}

async function _timersSendStreamStartMessage() {
  const res = await fetch('/api/propresenter/stage/stream_start', {
    method: 'POST',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || 'Stage message failed');
  }
  if (!data.sent) {
    throw new Error('Stage message failed to send');
  }
  return data;
}

async function _timersSendCustomStageMessage(message) {
  const res = await fetch('/api/propresenter/stage/message', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || 'Custom stage message failed');
  }
  if (!data.sent) {
    throw new Error(data.detail || 'Custom stage message failed to send');
  }
  return data;
}

async function _timersClearStageMessage() {
  const res = await fetch('/api/propresenter/stage/clear', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || 'Clear stage message failed');
  }
  if (!data.cleared) {
    throw new Error('Clear stage message failed');
  }
  return data;
}

function _timersRenderPresets(presets) {
  const body = document.getElementById('timers-presets-body');
  if (!body) return;

  body.innerHTML = '';
  (presets || []).forEach((t, idx) => {
    const tr = document.createElement('tr');
    tr.dataset.index = String(idx);

    const presetObj = (t && typeof t === 'object') ? t : {time: String(t || ''), name: ''};

    const runTd = document.createElement('td');
    const runBtn = document.createElement('button');
    runBtn.type = 'button';
    runBtn.className = 'btn timer-apply-btn';
    runBtn.textContent = '▶';
    runBtn.title = 'Apply this preset';
    runBtn.setAttribute('aria-label', 'Apply this preset');
    runBtn.dataset.action = 'apply';
    runTd.appendChild(runBtn);

    const orderTd = document.createElement('td');
    orderTd.textContent = String(idx + 1);

    const nameTd = document.createElement('td');
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'form-control form-control-sm';
    nameInput.value = String(presetObj.name || '').trim();
    nameInput.placeholder = 'Name';
    nameInput.dataset.role = 'preset-name';
    nameTd.appendChild(nameInput);

    const timeTd = document.createElement('td');
    const input = document.createElement('input');
    input.type = 'time';
    input.step = '60';
    input.className = 'form-control form-control-sm';
    const normalizedTime = _timersNormalizeTimeForInput(presetObj.time) || '00:00';
    input.value = normalizedTime;
    input.dataset.lastCompleteTime = input.value;
    input.dataset.role = 'preset-time';
    timeTd.appendChild(input);

    const pressesTd = document.createElement('td');
    const details = document.createElement('details');
    details.className = 'timer-presses';
    details.dataset.role = 'presses-details';
    const summary = document.createElement('summary');
    summary.dataset.role = 'presses-summary';
    summary.textContent = '0 presses';
    const inner = document.createElement('div');
    inner.className = 'timer-presses-body mt-2';

    const header = document.createElement('div');
    header.className = 'timer-press-header';
    header.innerHTML = '<div>Template</div><div>Button</div><div class="text-center">Move</div><div></div>';

    const list = document.createElement('div');
    list.dataset.role = 'presses-list';
    const addPressBtn = document.createElement('button');
    addPressBtn.type = 'button';
    addPressBtn.className = 'btn btn-sm btn-outline-primary mt-1';
    addPressBtn.textContent = 'Add Press';
    addPressBtn.dataset.pressAction = 'add';
    inner.appendChild(header);
    inner.appendChild(list);
    inner.appendChild(addPressBtn);
    details.appendChild(summary);
    details.appendChild(inner);
    pressesTd.appendChild(details);
    _timersWirePressesDetailsAnimation(details);

    const actTd = document.createElement('td');
    actTd.className = 'timer-actions-cell d-flex align-items-start justify-content-end gap-2 pt-1';

    const upBtn = document.createElement('button');
    upBtn.className = 'btn btn-sm btn-outline-secondary me-1 btn-icon-sm';
    upBtn.textContent = '▲';
    upBtn.title = 'Move preset up';
    upBtn.setAttribute('aria-label', 'Move preset up');
    upBtn.dataset.action = 'up';

    const downBtn = document.createElement('button');
    downBtn.className = 'btn btn-sm btn-outline-secondary me-1 btn-icon-sm';
    downBtn.textContent = '▼';
    downBtn.title = 'Move preset down';
    downBtn.setAttribute('aria-label', 'Move preset down');
    downBtn.dataset.action = 'down';

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-sm btn-outline-danger';
    delBtn.textContent = 'Delete';
    delBtn.dataset.action = 'delete';

    const movePresetWrap = document.createElement('div');
    movePresetWrap.className = 'd-flex flex-column align-items-center gap-1';
    movePresetWrap.appendChild(upBtn);
    movePresetWrap.appendChild(downBtn);

    actTd.appendChild(movePresetWrap);
    actTd.appendChild(delBtn);

    tr.appendChild(runTd);
    tr.appendChild(orderTd);
    tr.appendChild(nameTd);
    tr.appendChild(timeTd);
    tr.appendChild(pressesTd);
    tr.appendChild(actTd);
    body.appendChild(tr);

    // Render existing presses
    const presses = (presetObj && typeof presetObj === 'object') ? (presetObj.button_presses || presetObj.buttonPresses || presetObj.actions || []) : [];
    if (Array.isArray(presses)) {
      presses.forEach(p => list.appendChild(_timersNewPressRow(p)));
    }
    _timersUpdatePressSummary(tr);
  });
  _timersStageRenderOptions(presets || []);
  const stagePresetId = _timersReadStagePresetFromUI();
  _timersStageSetPreview(stagePresetId, presets || []);
  _timersStageSetButtonsEnabled(stagePresetId > 0 && stagePresetId <= (presets || []).length);
}

function _timersReadPresetsFromUI() {
  const body = document.getElementById('timers-presets-body');
  if (!body) return [];
  const rows = Array.from(body.querySelectorAll('tr'));
  const values = [];
  for (const r of rows) {
    const timeInp = r.querySelector('input[data-role="preset-time"]');
    if (!timeInp) continue;
    const nameInp = r.querySelector('input[data-role="preset-name"]');

    const time = String(timeInp.value || '').trim();
    if (!time) continue;
    const name = String((nameInp && nameInp.value) || '').trim();

    // button presses
    const list = r.querySelector('[data-role="presses-list"]');
    const pressesOut = [];
    if (list) {
      const pressRows = Array.from(list.querySelectorAll('[data-role="press-row"]'));
      for (const pr of pressRows) {
        const sel = pr.querySelector('select[data-role="press-template"]');
        const inp = pr.querySelector('input[data-role="press-url"]');
        let url = '';
        if (sel && String(sel.value || '') !== '') {
          url = _timersTemplateToURL(sel.value);
        } else {
          url = String((inp && inp.value) || '').trim();
        }
        const norm = _timersNormalizeButtonURL(url);
        if (!norm) continue;
        if (norm === '__INVALID__') {
          throw new Error(`Invalid button URL: ${url}. Use '1/2/3' or 'location/1/2/3/press'.`);
        }
        pressesOut.push({buttonURL: norm});
      }
    }

    const obj = {time, name};
    if (pressesOut.length) obj.button_presses = pressesOut;
    values.push(obj);
  }
  return values;
}

async function _timersLoad() {
  // load button templates first so we can render presses with names
  try {
    const r = await fetch('/api/templates?_ts=' + Date.now(), {cache: 'no-store'});
    const data = await r.json().catch(() => ({}));
    _timersButtonTemplates = Array.isArray(data.buttons) ? data.buttons : [];
  } catch (e) {
    _timersButtonTemplates = [];
  }

  const res = await fetch('/api/timers?_ts=' + Date.now(), {cache: 'no-store'});
  if (!res.ok) throw new Error('Failed to load timers');
  const data = await res.json();

  const presets = data.timer_presets || [];
  const stagePreset = Number(data.stream_start_preset || 0);
  _timersSetLastSaved(presets, stagePreset);
  _timersRenderPresets(presets);
  _timersApplyStagePresetId(stagePreset);
}

if (document.getElementById('timers-page')) {
  // Initial load
  _timersLoad().catch(e => _timersSetStatus(String(e.message || e), 'error'));

  // Add
  const addBtn = document.getElementById('timers-add');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      if (_timersHasIncompletePresetTime()) {
        _timersEnsureCompletePresetTimes();
      }
      (async () => {
        const ok = await _timersFlushAutoSave({showStatus: false});
        if (!ok) return;
        await _timersQueueMutation(
          {action: 'create_preset', time: '00:00', name: ''},
          {render: true}
        );
      })();
    });
  }

  const bulkAdjustInput = document.getElementById('timers-bulk-adjust-amount');
  const bulkAddBtn = document.getElementById('timers-bulk-add');
  const bulkSubtractBtn = document.getElementById('timers-bulk-subtract');
  const runBulkAdjust = (sign) => {
    (async () => {
      _timersClearStatus();
      try {
        if (bulkAddBtn) bulkAddBtn.disabled = true;
        if (bulkSubtractBtn) bulkSubtractBtn.disabled = true;
        await _timersAdjustAllPresets(sign);
      } catch (e) {
        _timersSetStatus(String(e.message || e), 'error');
      } finally {
        if (bulkAddBtn) bulkAddBtn.disabled = false;
        if (bulkSubtractBtn) bulkSubtractBtn.disabled = false;
      }
    })();
  };
  if (bulkAddBtn) {
    bulkAddBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      runBulkAdjust(1);
    });
  }
  if (bulkSubtractBtn) {
    bulkSubtractBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      runBulkAdjust(-1);
    });
  }
  if (bulkAdjustInput) {
    bulkAdjustInput.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      runBulkAdjust(ev.shiftKey ? -1 : 1);
    });
  }

  // Stage message preset selection
  const stageInput = document.getElementById('timers-stage-preset');
  if (stageInput) {
    const previewUpdate = () => {
      const presets = _timersReadPresetsForStage();
      const raw = String(stageInput.value || '').trim();
      if (!raw) {
        _timersStageSetHint('No stream-start preset selected.', 'warn');
        _timersStageSetPreview(0, presets);
        _timersStageSetButtonsEnabled(false);
        return;
      }
      const id = _timersStageResolvePresetId(raw, presets);
      if (id) {
        _timersStageSetHint('', '');
        _timersStageSetPreview(id, presets);
        _timersStageSetButtonsEnabled(true);
      } else {
        _timersStageSetHint('No matching preset found.', 'error');
        _timersStageSetPreview(0, presets);
        _timersStageSetButtonsEnabled(false);
      }
    };

    const commitSelection = () => {
      const presets = _timersReadPresetsForStage();
      const raw = String(stageInput.value || '').trim();
      if (!raw) {
        _timersApplyStagePresetId(0);
        _timersSaveStagePresetId(0);
        return;
      }
      const id = _timersStageResolvePresetId(raw, presets);
      if (id) {
        _timersApplyStagePresetId(id);
        _timersSaveStagePresetId(id);
      } else {
        _timersStageSetHint('No matching preset found.', 'error');
        _timersApplyStagePresetId(_timersReadStagePresetFromUI());
      }
    };

    stageInput.addEventListener('input', previewUpdate);
    stageInput.addEventListener('change', commitSelection);
    stageInput.addEventListener('blur', commitSelection);
  }

  const stageSendBtn = document.getElementById('timers-stage-send');
  if (stageSendBtn) {
    stageSendBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      (async () => {
        _timersClearStatus();
        try {
          const presetId = _timersReadStagePresetFromUI();
          if (!presetId) {
            _timersSetStatus('Select a stream start preset first.', 'error');
            return;
          }
          await _timersSendStreamStartMessage();
        } catch (e) {
          _timersSetStatus(String(e.message || e), 'error');
        }
      })();
    });
  }

  const customStageInput = document.getElementById('timers-stage-custom-message');
  const customStageHint = document.getElementById('timers-stage-custom-hint');
  const customStageSendBtn = document.getElementById('timers-stage-custom-send');
  const setCustomStageHint = (msg, kind) => {
    if (!customStageHint) return;
    customStageHint.textContent = String(msg || '');
    if (kind === 'error') customStageHint.className = 'form-text text-danger';
    else if (kind === 'ok') customStageHint.className = 'form-text text-success';
    else customStageHint.className = 'form-text text-muted';
  };
  const sendCustomStageMessage = () => {
    (async () => {
      _timersClearStatus();
      try {
        const message = String((customStageInput && customStageInput.value) || '').trim();
        if (!message) {
          setCustomStageHint('Type a message first.', 'error');
          if (customStageInput) customStageInput.focus();
          return;
        }
        if (customStageSendBtn) customStageSendBtn.disabled = true;
        await _timersSendCustomStageMessage(message);
        setCustomStageHint('Custom stage message sent.', 'ok');
      } catch (e) {
        setCustomStageHint(String(e.message || e), 'error');
        _timersSetStatus(String(e.message || e), 'error');
      } finally {
        if (customStageSendBtn) customStageSendBtn.disabled = false;
      }
    })();
  };
  if (customStageSendBtn) {
    customStageSendBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      sendCustomStageMessage();
    });
  }
  if (customStageInput) {
    customStageInput.addEventListener('input', () => {
      setCustomStageHint('Press Send Custom Message, or Ctrl+Enter from the text box.', '');
    });
    customStageInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        sendCustomStageMessage();
      }
    });
  }

  const stageClearBtn = document.getElementById('timers-stage-clear');
  if (stageClearBtn) {
    stageClearBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      (async () => {
        _timersClearStatus();
        try {
          await _timersClearStageMessage();
        } catch (e) {
          _timersSetStatus(String(e.message || e), 'error');
        }
      })();
    });
  }

  // Row actions (up/down/delete)
  const body = document.getElementById('timers-presets-body');
  if (body) {
    body.addEventListener('click', (ev) => {
      const btn = ev.target;
      if (!btn || !btn.dataset) return;

      // Press-row actions
      const pressAction = btn.dataset.pressAction;
      if (pressAction) {
        const tr = btn.closest('tr');
        const pressRow = btn.closest('[data-role="press-row"]');
        const list = tr ? tr.querySelector('[data-role="presses-list"]') : null;
        if (!tr || !list) return;

        if (pressAction === 'add') {
          list.appendChild(_timersNewPressRow({buttonURL: ''}));
        } else if (pressAction === 'delete' && pressRow) {
          pressRow.remove();
        } else if ((pressAction === 'up' || pressAction === 'down') && pressRow) {
          const rows = Array.from(list.querySelectorAll('[data-role="press-row"]'));
          const i = rows.indexOf(pressRow);
          if (i >= 0) {
            if (pressAction === 'up' && i > 0) {
              list.insertBefore(pressRow, rows[i - 1]);
            }
            if (pressAction === 'down' && i < rows.length - 1) {
              list.insertBefore(rows[i + 1], pressRow);
            }
          }
        }

        _timersUpdatePressSummary(tr);
        _timersMarkRowDirty(tr, {delayMs: 0});
        return;
      }

      // Preset-row actions
      const action = btn.dataset.action;
      if (!action || !['delete', 'up', 'down', 'apply'].includes(action)) return;
      const tr = btn.closest('tr');
      if (!tr) return;
      const idx = Number(tr.dataset.index);
      if (!Number.isFinite(idx)) return;

      if (action === 'apply') {
        (async () => {
          _timersClearStatus();
          const ok = await _timersFlushAutoSave();
          if (!ok) return;
          try {
            await _timersApplyPreset(idx + 1);
          } catch (e) {
            _timersSetStatus(String(e.message || e), 'error');
          }
        })();
        return;
      }

      if (_timersHasIncompletePresetTime()) {
        _timersEnsureCompletePresetTimes();
      }

      (async () => {
        const ok = await _timersFlushAutoSave({showStatus: false});
        if (!ok) return;
        if (action === 'delete') {
          await _timersQueueMutation(
            {action: 'delete_preset', preset: idx + 1},
            {render: true}
          );
        } else if (action === 'up' && idx > 0) {
          await _timersQueueMutation(
            {action: 'move_preset', preset: idx + 1, direction: 'up'},
            {render: true}
          );
        } else if (action === 'down' && idx < body.querySelectorAll('tr').length - 1) {
          await _timersQueueMutation(
            {action: 'move_preset', preset: idx + 1, direction: 'down'},
            {render: true}
          );
        }
      })();
    });

    // Auto-save on edits
    body.addEventListener('input', (ev) => {
      const el = ev.target;
      if (!el) return;
      // NOTE: For custom press URLs, do NOT auto-save while typing.
      // Save happens on blur/change instead.
      if (el.matches && (el.matches('input[data-role="preset-time"]') || el.matches('input[data-role="preset-name"]'))) {
        if (el.matches('input[data-role="preset-time"]')) {
          if (!String(el.value || '').trim()) {
            _timersScheduleIncompleteTimeRestore(el);
            return;
          }
          _timersRememberCompleteTime(el);
        }
        _timersMarkRowDirty(el.closest('tr'), {delayMs: 700});
      }
    });
    body.addEventListener('blur', (ev) => {
      const el = ev.target;
      if (!el || !el.matches || !el.matches('input[data-role="preset-time"]')) return;
      if (!String(el.value || '').trim()) {
        _timersScheduleIncompleteTimeRestore(el);
        return;
      }
      _timersRememberCompleteTime(el);
    }, true);
    body.addEventListener('change', (ev) => {
      const el = ev.target;
      if (!el) return;
      if (el.matches && el.matches('input[data-role="preset-time"]')) {
        if (!String(el.value || '').trim()) {
          _timersScheduleIncompleteTimeRestore(el);
          return;
        }
        _timersRememberCompleteTime(el);
      }
      if (el.matches && (el.matches('input[data-role="preset-time"]') || el.matches('input[data-role="preset-name"]') || el.matches('select[data-role="press-template"]') || el.matches('input[data-role="press-url"]'))) {
        _timersMarkRowDirty(el.closest('tr'), {delayMs: 0});
      }
    });

    // Template selection autofill behavior
    body.addEventListener('change', (ev) => {
      const sel = ev.target;
      if (!sel || sel.tagName !== 'SELECT') return;
      if (sel.dataset.role !== 'press-template') return;
      const pr = sel.closest('[data-role="press-row"]');
      if (!pr) return;
      const inp = pr.querySelector('input[data-role="press-url"]');
      if (!inp) return;

      if (String(sel.value || '') !== '') {
        const i = Number(sel.value);
        const t = Number.isFinite(i) ? _timersButtonTemplates[i] : null;
        const u = _timersTemplateToURL(sel.value);
        // Show short form like 1/0/1 for templates too
        inp.value = String((t && t.pattern) ? t.pattern : _timersButtonURLToDisplay(u));
        inp.disabled = true;
      } else {
        inp.disabled = false;
      }

      _timersMarkRowDirty(sel.closest('tr'), {delayMs: 0});
    });
  }
}

// --- Permissions page (users + groups) ---
if (document.getElementById('permissions-page')) {
  const permissionsRoot = document.getElementById('permissions-page');
  const userList = document.getElementById('permissions-user-list');
  const userSearch = document.getElementById('permissions-user-search');
  const createUserCard = document.getElementById('permissions-create-user-card');
  const usersCard = document.getElementById('permissions-users-card');
  const userItems = Array.from(document.querySelectorAll('[data-user-link][data-user-id]'));
  const userPanels = Array.from(document.querySelectorAll('[data-user-panel][data-user-id]'));
  const USER_STORAGE_KEY = 'tdeck_permissions_selectedUserId';
  const userSaveTimers = new Map();
  const userSaveInFlight = new Map();
  const userSaveQueued = new Map();
  const minPasswordLength = Math.max(4, Math.min(Number(permissionsRoot.getAttribute('data-min-password-length')) || 6, 128));

  function _permissionsExistingUsernames() {
    return new Set(userItems.map(item => String(item.getAttribute('data-user-username') || '').trim().toLowerCase()).filter(Boolean));
  }

  function _permissionsExistingUserEmails() {
    return new Set(userItems.map(item => String(item.getAttribute('data-user-email') || '').trim().toLowerCase()).filter(Boolean));
  }

  function _permissionsExistingGroupNames() {
    const names = Array.from(document.querySelectorAll('[data-group-option][data-group-name]')).map(el => String(el.getAttribute('data-group-name') || '').trim().toLowerCase()).filter(Boolean);
    return new Set(names);
  }

  function _permissionsShowFieldError(field, msg) {
    if (!field) return false;
    field.setCustomValidity(String(msg || 'Invalid value'));
    field.reportValidity();
    const clear = () => field.setCustomValidity('');
    field.addEventListener('input', clear, { once: true });
    field.addEventListener('change', clear, { once: true });
    return false;
  }

  try {
    const feedbackError = String(permissionsRoot.getAttribute('data-feedback-error') || '').trim();
    if (feedbackError) {
      const activePane = permissionsRoot.querySelector('.tab-pane.active') || permissionsRoot;
      const field = activePane.querySelector('input:not([type="hidden"]), button');
      _permissionsShowFieldError(field, feedbackError);
    }
  } catch (e) {}

  document.querySelectorAll('form[data-confirm]').forEach(form => {
    if (form.getAttribute('data-confirm-bound') === '1') return;
    form.setAttribute('data-confirm-bound', '1');
    form.addEventListener('submit', (e) => {
      const msg = String(form.getAttribute('data-confirm') || 'Are you sure?');
      if (!window.confirm(msg)) {
        e.preventDefault();
      }
    });
  });

  document.querySelectorAll('form').forEach(form => {
    const actionEl = form.querySelector('input[name="action"]');
    const action = actionEl ? String(actionEl.value || '') : '';
    if (!['create_user', 'create_group', 'reset_password'].includes(action)) return;
    form.addEventListener('submit', (e) => {
      if (action === 'create_user') {
        const usernameEl = form.querySelector('input[name="username"]');
        const fullNameEl = form.querySelector('input[name="full_name"]');
        const emailEl = form.querySelector('input[name="email"]');
        const passwordEl = form.querySelector('input[name="password"]');
        const username = String((usernameEl || {}).value || '').trim();
        const fullName = String((fullNameEl || {}).value || '').trim();
        const email = String((emailEl || {}).value || '').trim();
        const password = String((passwordEl || {}).value || '');
        if (username && _permissionsExistingUsernames().has(username.toLowerCase())) {
          e.preventDefault();
          return _permissionsShowFieldError(usernameEl, `The user "${username}" already exists. Use a different username.`);
        }
        if (!fullName) {
          e.preventDefault();
          return _permissionsShowFieldError(fullNameEl, 'Please fill out this field.');
        }
        if (email && _permissionsExistingUserEmails().has(email.toLowerCase())) {
          e.preventDefault();
          return _permissionsShowFieldError(emailEl, `The email "${email}" is already being used. Use a different email address.`);
        }
        if (password && password.length < minPasswordLength) {
          e.preventDefault();
          return _permissionsShowFieldError(passwordEl, `Password must be at least ${minPasswordLength} characters.`);
        }
      }
      if (action === 'create_group') {
        const groupEl = form.querySelector('input[name="group_name"]');
        const groupName = String((groupEl || {}).value || '').trim();
        if (groupName && _permissionsExistingGroupNames().has(groupName.toLowerCase())) {
          e.preventDefault();
          return _permissionsShowFieldError(groupEl, `The group "${groupName}" already exists. Use a different group name.`);
        }
      }
      if (action === 'reset_password') {
        const passwordEl = form.querySelector('input[name="new_password"]');
        const password = String((passwordEl || {}).value || '');
        if (password && password.length < minPasswordLength) {
          e.preventDefault();
          return _permissionsShowFieldError(passwordEl, `Password must be at least ${minPasswordLength} characters.`);
        }
      }
      return true;
    });
  });

  function _syncUsersListHeight() {
    if (!createUserCard || !usersCard || !userList) return;
    const createRect = createUserCard.getBoundingClientRect();
    const cardBody = usersCard.querySelector('.card-body');
    if (!createRect || !cardBody) return;
    if (createRect.height <= 0 || usersCard.getBoundingClientRect().height <= 0) return;
    userList.style.maxHeight = '';
    const bodyRect = cardBody.getBoundingClientRect();
    const listRect = userList.getBoundingClientRect();
    if (bodyRect.height <= 0 || listRect.height <= 0) return;
    const nonListHeight = Math.max(0, bodyRect.height - listRect.height);
    const maxListHeight = Math.max(140, Math.floor(createRect.height - nonListHeight));
    userList.style.maxHeight = `${maxListHeight}px`;
    userList.style.overflowY = 'auto';
  }

  function _scheduleUsersListHeightSync() {
    window.requestAnimationFrame(() => {
      _syncUsersListHeight();
      setTimeout(_syncUsersListHeight, 0);
    });
  }

  _syncUsersListHeight();
  window.addEventListener('resize', _scheduleUsersListHeightSync);
  setTimeout(_syncUsersListHeight, 0);

  function _showPermissionsTab(name) {
    const target = name === 'groups' ? '#permissions-groups' : '#permissions-users';
    const btn = document.querySelector(`[data-bs-target="${target}"]`);
    if (!btn) return;
    try {
      if (window.bootstrap && window.bootstrap.Tab) {
        window.bootstrap.Tab.getOrCreateInstance(btn).show();
      } else {
        btn.click();
      }
    } catch (e) {
      try { btn.click(); } catch (ignored) {}
    }
  }

  function _permissionsTabNameFromButton(btn) {
    const target = btn ? String(btn.getAttribute('data-bs-target') || '') : '';
    return target === '#permissions-groups' ? 'groups' : 'users';
  }

  function _setPermissionsUrlTab(name) {
    const tabName = name === 'groups' ? 'groups' : 'users';
    try {
      const url = new URL(window.location.href);
      url.searchParams.set('tab', tabName);
      const desiredHash = tabName === 'groups' ? '#groups' : '#users';
      if (!url.hash || url.hash === '#groups' || url.hash === '#users' || url.hash.startsWith('#role-') || url.hash.startsWith('#user-')) {
        url.hash = desiredHash;
      }
      window.history.replaceState(null, '', url.toString());
    } catch (e) {}
  }

  function _syncPermissionsHash() {
    let tabParam = '';
    try {
      tabParam = String(new URLSearchParams(window.location.search || '').get('tab') || '').toLowerCase();
    } catch (e) {
      tabParam = '';
    }
    if (tabParam === 'groups') {
      _showPermissionsTab('groups');
      return;
    }
    if (tabParam === 'users') {
      _showPermissionsTab('users');
      return;
    }
    const h = String(window.location.hash || '').replace(/^#/, '');
    if (h === 'groups' || h.startsWith('role-')) _showPermissionsTab('groups');
    if (h === 'users' || h.startsWith('user-')) _showPermissionsTab('users');
  }

  _syncPermissionsHash();
  window.addEventListener('hashchange', _syncPermissionsHash);
  document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tabBtn => {
    tabBtn.addEventListener('shown.bs.tab', (e) => {
      _setPermissionsUrlTab(_permissionsTabNameFromButton(e.target));
      _scheduleUsersListHeightSync();
    });
    tabBtn.addEventListener('click', () => {
      _setPermissionsUrlTab(_permissionsTabNameFromButton(tabBtn));
      _scheduleUsersListHeightSync();
    });
  });

  function _userPanelById(userId) {
    const id = String(userId || '').trim();
    return userPanels.find(p => String(p.getAttribute('data-user-id')) === id) || null;
  }

  function _userSetMessage(panel, kind, msg) {
    if (!panel) return;
    const el = panel.querySelector(kind === 'error' ? '[data-user-error]' : '[data-user-saved]');
    if (!el) return;
    if (!msg) {
      _uiClearAutoHide(el);
      el.textContent = kind === 'error' ? '' : 'Saved';
      el.classList.add('d-none');
      return;
    }
    el.classList.remove('d-none');
    if (kind === 'error') el.textContent = String(msg);
    _uiScheduleAutoHide(el, () => el.classList.add('d-none'));
  }

  function _selectUser(userId, { persist = true, updateHash = true } = {}) {
    const id = String(userId || '').trim();
    if (!id) return;
    userItems.forEach(item => item.classList.toggle('active', String(item.getAttribute('data-user-id')) === id));
    userPanels.forEach(panel => panel.classList.toggle('d-none', String(panel.getAttribute('data-user-id')) !== id));
    if (persist) {
      try { window.localStorage.setItem(USER_STORAGE_KEY, id); } catch (e) {}
    }
    if (updateHash) {
      try { window.location.hash = `user-${id}`; } catch (e) {}
    }
  }

  function _readSelectedUserId() {
    try {
      const h = String(window.location.hash || '');
      const m = h.match(/^#user-(\d+)$/);
      if (m) return String(m[1]);
    } catch (e) {}
    try {
      const stored = window.localStorage.getItem(USER_STORAGE_KEY);
      if (stored) return String(stored);
    } catch (e) {}
    return userItems[0] ? String(userItems[0].getAttribute('data-user-id')) : null;
  }

  const initialUserId = _readSelectedUserId();
  if (initialUserId && userPanels.length) _selectUser(initialUserId, { persist: true, updateHash: false });

  if (userList) {
    userList.addEventListener('click', (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('[data-user-select][data-user-id]') : null;
      if (!btn) return;
      _selectUser(btn.getAttribute('data-user-id'), { persist: true, updateHash: true });
    });
  }

  if (userSearch) {
    userSearch.addEventListener('input', () => {
      const q = String(userSearch.value || '').trim().toLowerCase();
      userItems.forEach(item => {
        const name = String(item.getAttribute('data-user-name') || '').toLowerCase();
        item.classList.toggle('d-none', !!q && !name.includes(q));
      });
    });
  }

  document.querySelectorAll('[data-group-search]').forEach(search => {
    search.addEventListener('input', () => {
      const q = String(search.value || '').trim().toLowerCase();
      const scope = search.closest('form') || search.closest('[data-user-panel]') || document;
      scope.querySelectorAll('[data-group-option][data-group-name]').forEach(option => {
        const name = String(option.getAttribute('data-group-name') || '').toLowerCase();
        option.classList.toggle('d-none', !!q && !name.includes(q));
      });
    });
  });

  function _userReadPayload(panel) {
    const form = panel ? panel.querySelector('form[data-user-form]') : null;
    if (!form) return null;
    return {
      is_active: !!(form.querySelector('input[name="is_active"]') || {}).checked,
      group_ids: Array.from(form.querySelectorAll('input[name="group_ids"]:checked')).map(cb => String(cb.value)),
    };
  }

  async function _userSaveNow(userId) {
    const id = String(userId || '').trim();
    const panel = _userPanelById(id);
    const payload = _userReadPayload(panel);
    if (!id || !panel || !payload) return false;
    if (userSaveInFlight.get(id)) {
      userSaveQueued.set(id, true);
      return true;
    }
    userSaveInFlight.set(id, true);
    _userSetMessage(panel, 'error', '');
    _userSetMessage(panel, 'saved', '');
    try {
      const res = await fetch(`/api/admin/users/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : 'Save failed');
      _userSetMessage(panel, 'saved', 'Saved');
      return true;
    } catch (e) {
      _userSetMessage(panel, 'error', String(e.message || e));
      return false;
    } finally {
      userSaveInFlight.set(id, false);
      if (userSaveQueued.get(id)) {
        userSaveQueued.set(id, false);
        _userScheduleSave(id, { delayMs: 250 });
      }
    }
  }

  function _userScheduleSave(userId, { delayMs = 250 } = {}) {
    const id = String(userId || '').trim();
    if (!id) return;
    const prior = userSaveTimers.get(id);
    if (prior) clearTimeout(prior);
    userSaveTimers.set(id, setTimeout(() => {
      userSaveTimers.delete(id);
      _userSaveNow(id);
    }, Math.max(0, Number(delayMs) || 0)));
  }

  userPanels.forEach(panel => {
    const form = panel.querySelector('form[data-user-form]');
    if (!form) return;
    const userId = String(form.getAttribute('data-user-id') || '').trim();
    form.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => _userScheduleSave(userId, { delayMs: 0 }));
    });
  });
}

// --- Admin user detail page ---
if (document.getElementById('admin-user-detail-page')) {
  const root = document.getElementById('admin-user-detail-page');
  const userId = String(root.getAttribute('data-user-id') || '').trim();
  const minPasswordLength = Math.max(4, Math.min(Number(root.getAttribute('data-min-password-length')) || 6, 128));
  const accessForm = root.querySelector('[data-user-detail-access-form]');
  const accessError = root.querySelector('[data-user-detail-access-error]');
  const passwordError = root.querySelector('[data-user-detail-password-error]');
  let accessSaveTimer = null;
  let accessSaveInFlight = false;
  let accessSaveQueued = false;

  function _detailShowFieldError(field, msg) {
    if (!field) return false;
    field.setCustomValidity(String(msg || 'Invalid value'));
    field.reportValidity();
    if (passwordError) {
      passwordError.textContent = String(msg || 'Invalid value');
      passwordError.classList.remove('d-none');
    }
    const clear = () => field.setCustomValidity('');
    field.addEventListener('input', clear, { once: true });
    field.addEventListener('change', clear, { once: true });
    return false;
  }

  document.querySelectorAll('[data-group-search]').forEach(search => {
    search.addEventListener('input', () => {
      const q = String(search.value || '').trim().toLowerCase();
      const scope = search.closest('.card-body') || document;
      scope.querySelectorAll('[data-group-option][data-group-name]').forEach(option => {
        const name = String(option.getAttribute('data-group-name') || '').toLowerCase();
        option.classList.toggle('d-none', !!q && !name.includes(q));
      });
    });
  });

  function _detailSetAccessMessage(kind, msg) {
    const el = kind === 'error' ? accessError : null;
    if (!el) return;
    if (!msg) {
      _uiClearAutoHide(el);
      el.classList.add('d-none');
      if (kind !== 'error') el.textContent = 'Saved';
      return;
    }
    el.textContent = String(msg);
    el.classList.remove('d-none');
    _uiScheduleAutoHide(el, () => el.classList.add('d-none'));
  }

  function _detailReadAccessPayload() {
    if (!accessForm) return null;
    return {
      is_active: !!(accessForm.querySelector('input[name="is_active"]') || {}).checked,
      group_ids: Array.from(accessForm.querySelectorAll('input[name="group_ids"]:checked')).map(cb => String(cb.value)),
    };
  }

  async function _detailSaveAccessNow() {
    const payload = _detailReadAccessPayload();
    if (!userId || !payload) return false;
    if (accessSaveInFlight) {
      accessSaveQueued = true;
      return true;
    }
    accessSaveInFlight = true;
    _detailSetAccessMessage('error', '');
    _detailSetAccessMessage('saved', '');
    try {
      const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : 'Save failed');
      _detailSetAccessMessage('saved', 'Saved');
      return true;
    } catch (e) {
      _detailSetAccessMessage('error', String(e.message || e));
      return false;
    } finally {
      accessSaveInFlight = false;
      if (accessSaveQueued) {
        accessSaveQueued = false;
        _detailScheduleAccessSave(250);
      }
    }
  }

  function _detailScheduleAccessSave(delayMs) {
    if (accessSaveTimer) clearTimeout(accessSaveTimer);
    accessSaveTimer = setTimeout(() => {
      accessSaveTimer = null;
      _detailSaveAccessNow();
    }, Math.max(0, Number(delayMs) || 0));
  }

  if (accessForm) {
    accessForm.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => _detailScheduleAccessSave(0));
    });
  }

  document.querySelectorAll('form').forEach(form => {
    const actionEl = form.querySelector('input[name="action"]');
    const action = actionEl ? String(actionEl.value || '') : '';
    if (action !== 'reset_password') return;
    form.addEventListener('submit', (e) => {
      const generated = e.submitter && String(e.submitter.getAttribute('name') || '') === 'generate_password';
      if (generated) return true;
      if (passwordError) {
        passwordError.classList.add('d-none');
        passwordError.textContent = '';
      }
      const passwordEl = form.querySelector('input[name="new_password"]');
      const password = String((passwordEl || {}).value || '');
      if (password.length < minPasswordLength) {
        e.preventDefault();
        return _detailShowFieldError(passwordEl, `Password must be at least ${minPasswordLength} characters.`);
      }
      return true;
    });
  });
}

// --- Groups editor ---
if (document.getElementById('access-levels-page')) {
  const ROLE_STORAGE_KEY = 'tdeck_groups_selectedGroupId';
  const roleList = document.getElementById('access-levels-role-list');
  const roleItems = Array.from(document.querySelectorAll('[data-role-item][data-role-id]'));
  const rolePanels = Array.from(document.querySelectorAll('[data-role-panel][data-role-id]'));

  function _rolePanelById(roleId) {
    const id = String(roleId || '').trim();
    return rolePanels.find(p => String(p.getAttribute('data-role-id')) === id) || null;
  }

  function _roleSetError(panel, msg) {
    if (!panel) return;
    const el = panel.querySelector('[data-role-error]');
    if (!el) return;
    if (!msg) {
      _uiClearAutoHide(el);
      el.textContent = '';
      el.classList.add('d-none');
      return;
    }
    el.className = 'alert alert-danger';
    el.textContent = String(msg);
    el.classList.remove('d-none');
    _uiScheduleAutoHide(el, () => {
      el.textContent = '';
      el.classList.add('d-none');
    });
  }

  function _readSelectedRoleFromHash() {
    try {
      const h = String(window.location.hash || '');
      const m = h.match(/^#role-(\d+)$/);
      return m ? String(m[1]) : null;
    } catch (e) {
      return null;
    }
  }

  function _readSelectedRoleFromStorage() {
    try {
      const v = window.localStorage.getItem(ROLE_STORAGE_KEY);
      return v ? String(v) : null;
    } catch (e) {
      return null;
    }
  }

  function _setSelectedRole(roleId, { persist = true, updateHash = true } = {}) {
    const id = String(roleId || '').trim();
    if (!id) return;

    // mark list selection
    roleItems.forEach(item => {
      const match = String(item.getAttribute('data-role-id')) === id;
      item.classList.toggle('active', match);
    });

    // show one panel at a time
    rolePanels.forEach(panel => {
      const match = String(panel.getAttribute('data-role-id')) === id;
      panel.classList.toggle('d-none', !match);
    });

    if (persist) {
      try { window.localStorage.setItem(ROLE_STORAGE_KEY, id); } catch (e) {}
    }
    if (updateHash) {
      try { window.location.hash = `role-${id}`; } catch (e) {}
    }
  }

  // Init selection
  const initialId = _readSelectedRoleFromHash() || _readSelectedRoleFromStorage() || (roleItems[0] ? String(roleItems[0].getAttribute('data-role-id')) : null);
  if (initialId) _setSelectedRole(initialId, { persist: true, updateHash: false });

  // Clicking a role selects it
  if (roleList) {
    roleList.addEventListener('click', (e) => {
      const t = e.target;
      const btn = t && t.closest ? t.closest('[data-role-select][data-role-id]') : null;
      if (!btn) return;
      const id = btn.getAttribute('data-role-id');
      if (id) _setSelectedRole(id, { persist: true, updateHash: true });
    });
  }

  // If user navigates via hash
  window.addEventListener('hashchange', () => {
    const id = _readSelectedRoleFromHash();
    if (id) _setSelectedRole(id, { persist: true, updateHash: false });
  });

  // --- Auto-save per role ---
  const _saveTimers = new Map();
  const _saveInFlight = new Map();
  const _saveQueued = new Map();

  function _applyRoleFieldState(panel) {
    if (!panel) return;
    const form = panel.querySelector('form[data-role-form]');
    if (!form) return;
    const routingCb = form.querySelector('input[type="checkbox"][name="page_keys"][value="page:routing"]');
    const videohubCb = form.querySelector('input[type="checkbox"][name="page_keys"][value="page:videohub"]');
    const atemCb = form.querySelector('input[type="checkbox"][name="page_keys"][value="page:atem_audio"]');
    const outEl = form.querySelector('[data-role="vh-outputs"]');
    const inEl = form.querySelector('[data-role="vh-inputs"]');
    const presetsEl = form.querySelector('[data-role="vh-presets"]');
    const editPresetsEl = form.querySelector('[data-role="vh-edit-presets"]');
    const atemFields = Array.from(form.querySelectorAll('[data-role="atem-audio-field"]'));
    if (routingCb && outEl && inEl) {
      const routingEnabled = !!routingCb.checked;
      outEl.disabled = !routingEnabled;
      inEl.disabled = !routingEnabled;
    }
    if (videohubCb && (presetsEl || editPresetsEl)) {
      const videohubEnabled = !!videohubCb.checked;
      if (presetsEl) presetsEl.disabled = !videohubEnabled;
      if (editPresetsEl) editPresetsEl.disabled = !videohubEnabled;
    }
    if (atemCb && atemFields.length) {
      const atemEnabled = !!atemCb.checked;
      atemFields.forEach(el => {
        el.disabled = !atemEnabled;
      });
    }
  }

  function _roleReadPayload(panel) {
    const form = panel.querySelector('form[data-role-form]');
    if (!form) return null;
    const roleId = String(form.getAttribute('data-role-id') || '').trim();
    if (!roleId) return null;

    const pageKeys = Array.from(form.querySelectorAll('input[type="checkbox"][name="page_keys"]:checked')).map(cb => String(cb.value));
    const idleTimeoutEl = form.querySelector('input[name="auth_idle_timeout_minutes_override_role"]');
    const outEl = form.querySelector('input[name="videohub_allowed_outputs_role"]');
    const inEl = form.querySelector('input[name="videohub_allowed_inputs_role"]');
    const presetsEl = form.querySelector('input[name="videohub_allowed_presets_role"]');
    const canEditEl = form.querySelector('input[name="videohub_can_edit_presets_role"]');
    const companionClickSurfaceIds = Array.from(form.querySelectorAll('input[type="checkbox"][name="companion_click_surfaces_role"]:checked')).map(cb => String(cb.value || ''));
    const atemAudioSourceIds = Array.from(form.querySelectorAll('input[type="checkbox"][name="atem_allowed_audio_sources_role"]:checked')).map(cb => String(cb.value || ''));
    const atemCanSoloEl = form.querySelector('input[name="atem_can_solo_audio_role"]');
    const atemCanMonitorEl = form.querySelector('input[name="atem_can_monitor_audio_role"]');

    return {
      page_keys: pageKeys,
      auth_idle_timeout_minutes_override_role: idleTimeoutEl ? String(idleTimeoutEl.value || '') : '',
      videohub_allowed_outputs_role: outEl ? String(outEl.value || '') : '',
      videohub_allowed_inputs_role: inEl ? String(inEl.value || '') : '',
      videohub_allowed_presets_role: presetsEl ? String(presetsEl.value || '') : '',
      videohub_can_edit_presets_role: canEditEl ? !!canEditEl.checked : true,
      companion_click_surfaces_role: companionClickSurfaceIds,
      atem_allowed_audio_sources_role: atemAudioSourceIds,
      atem_can_solo_audio_role: atemCanSoloEl ? !!atemCanSoloEl.checked : false,
      atem_can_monitor_audio_role: atemCanMonitorEl ? !!atemCanMonitorEl.checked : false,
    };
  }

  async function _roleSaveNow(roleId) {
    const id = String(roleId || '').trim();
    const panel = _rolePanelById(id);
    if (!panel) return false;
    const payload = _roleReadPayload(panel);
    if (!payload) return false;

    if (_saveInFlight.get(id)) {
      _saveQueued.set(id, true);
      return true;
    }

    _saveInFlight.set(id, true);
    _roleSetError(panel, '');

    try {
      const res = await fetch(`/api/admin/groups/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data || !data.ok) {
        throw new Error((data && data.error) ? data.error : 'Save failed');
      }
      return true;
    } catch (e) {
      _roleSetError(panel, String(e.message || e));
      return false;
    } finally {
      _saveInFlight.set(id, false);
      if (_saveQueued.get(id)) {
        _saveQueued.set(id, false);
        // Small delay so we don't hammer the server.
        _roleScheduleSave(id, { delayMs: 250 });
      }
    }
  }

  function _roleScheduleSave(roleId, { delayMs = 500 } = {}) {
    const id = String(roleId || '').trim();
    if (!id) return;
    const prior = _saveTimers.get(id);
    if (prior) {
      clearTimeout(prior);
      _saveTimers.delete(id);
    }
    const t = setTimeout(() => {
      _saveTimers.delete(id);
      _roleSaveNow(id);
    }, Math.max(0, Number(delayMs) || 0));
    _saveTimers.set(id, t);
  }

  // Wire up each role panel
  rolePanels.forEach(panel => {
    _applyRoleFieldState(panel);

    const form = panel.querySelector('form[data-role-form]');
    if (!form) return;
    const roleId = String(form.getAttribute('data-role-id') || '').trim();
    if (!roleId) return;

    // Checkboxes save quickly
    form.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => {
        _applyRoleFieldState(panel);
        _roleScheduleSave(roleId, { delayMs: 0 });
      });
    });

    // Text inputs: debounce while typing, flush on blur
    form.querySelectorAll('input[type="text"], input:not([type])').forEach(inp => {
      inp.addEventListener('input', () => {
        _roleScheduleSave(roleId, { delayMs: 600 });
      });
      inp.addEventListener('change', () => {
        _roleScheduleSave(roleId, { delayMs: 0 });
      });
      inp.addEventListener('blur', () => {
        _roleScheduleSave(roleId, { delayMs: 0 });
      });
    });
  });
}

if (document.getElementById('companion-surfaces-config-page')) {
  const statusEl = document.getElementById('companion-surfaces-status');
  const surfacesList = document.getElementById('companion-surfaces-list');
  const displaysList = document.getElementById('companion-displays-list');
  const addSurfaceBtn = document.getElementById('companion-surface-add');
  let surfaceConfig = {surfaces: [], surface_controls: []};
  let saveTimer = null;

  function _csSetStatus(msg, type = 'info') {
    if (!statusEl) return;
    const text = String(msg || '').trim();
    if (!text) {
      statusEl.innerHTML = '';
      return;
    }
    statusEl.innerHTML = `<div class="alert alert-${type} py-2 mb-0">${_escapeHtml(text)}</div>`;
  }

  function _csNewSurface() {
    const idx = (surfaceConfig.surfaces || []).length + 1;
    return {id: `surface-${idx}`, label: `Surface ${idx}`, layout: '3x5'};
  }

  function _csSurfaceOptions(selectedId) {
    return (surfaceConfig.surfaces || []).map(s => {
      const id = String(s.id || '');
      const label = String(s.label || id);
      return `<option value="${_escapeHtml(id)}"${id === String(selectedId || '') ? ' selected' : ''}>${_escapeHtml(label)} (${_escapeHtml(id)})</option>`;
    }).join('');
  }

  function _csLayoutOptions(selectedLayout) {
    const layouts = [
      ['3x5', '3x5'],
      ['2x5', '2x5'],
      ['4x5', '4x5'],
      ['3x4', '3x4'],
      ['2x4', '2x4'],
      ['4x4', '4x4'],
      ['4x8', '4x8'],
    ];
    const selected = String(selectedLayout || '3x5');
    return layouts.map(([value, label]) => (
      `<option value="${_escapeHtml(value)}"${value === selected ? ' selected' : ''}>${_escapeHtml(label)}</option>`
    )).join('');
  }

  function _csScaleFromInput(value) {
    const raw = String(value ?? '').trim();
    const number = Number(raw || 1);
    if (!Number.isFinite(number) || number <= 0) {
      throw new Error('Size must be greater than zero.');
    }
    return `${Number.isInteger(number) ? number : Number(number.toFixed(3))}`;
  }

  function _csRender() {
    if (!surfacesList || !displaysList) return;
    const surfaces = surfaceConfig.surfaces || [];
    const displays = surfaceConfig.surface_controls || [];

    surfacesList.innerHTML = surfaces.length ? surfaces.map((surface, idx) => `
      <div class="companion-config-row" data-surface-idx="${idx}" data-surface-old-id="${_escapeHtml(surface.id || '')}">
        <div class="companion-surface-config-grid">
          <div>
            <label class="form-label small text-muted mb-1">ID</label>
            <input class="form-control form-control-sm" data-surface-field="id" value="${_escapeHtml(surface.id || '')}">
          </div>
          <div>
            <label class="form-label small text-muted mb-1">Label</label>
            <input class="form-control form-control-sm" data-surface-field="label" value="${_escapeHtml(surface.label || '')}">
          </div>
          <div>
            <label class="form-label small text-muted mb-1">Surface Size</label>
            <select class="form-select form-select-sm" data-surface-field="layout">${_csLayoutOptions(surface.layout)}</select>
          </div>
          <div class="companion-config-delete-cell">
            <button class="btn btn-sm btn-outline-danger" type="button" data-surface-delete="1" title="Delete">Delete</button>
          </div>
        </div>
      </div>
    `).join('') : '<div class="text-muted small">No surfaces configured.</div>';

    function _renderDisplayList(list) {
      return list.length ? list.map((display, idx) => `
      <div class="companion-config-row" data-display-idx="${idx}">
        <div class="row g-2 align-items-end">
          <div class="col-12 col-lg-5">
            <label class="form-label small text-muted mb-1">Display Label</label>
            <input class="form-control form-control-sm" data-display-field="label" value="${_escapeHtml(display.label || `Display ${idx + 1}`)}">
          </div>
          <div class="col-12 col-md-8 col-lg-5">
            <label class="form-label small text-muted mb-1">Surface</label>
            <select class="form-select form-select-sm" data-display-field="surface_id">${_csSurfaceOptions(display.surface_id || display.id)}</select>
          </div>
          <div class="col-12 col-md-4 col-lg-2">
            <label class="form-label small text-muted mb-1">Size</label>
            <input class="form-control form-control-sm" type="number" min="0.1" step="0.05" inputmode="decimal" data-display-field="size" value="${_escapeHtml(String(display.size || '1'))}">
          </div>
        </div>
      </div>
      `).join('') : '<div class="text-muted small">No displays configured.</div>';
    }

    displaysList.innerHTML = _renderDisplayList(displays);
  }

  function _csReadFromUi() {
    const surfaces = [];
    const idMap = new Map();
    if (surfacesList) {
      surfacesList.querySelectorAll('[data-surface-idx]').forEach(row => {
        const oldId = String(row.getAttribute('data-surface-old-id') || '').trim();
        const id = String((row.querySelector('[data-surface-field="id"]') || {}).value || '').trim();
        const label = String((row.querySelector('[data-surface-field="label"]') || {}).value || '').trim();
        const layout = String((row.querySelector('[data-surface-field="layout"]') || {}).value || '3x5').trim();
        if (id) {
          surfaces.push({id, label: label || id, layout: layout || '3x5'});
          if (oldId && oldId !== id) idMap.set(oldId, id);
        }
      });
    }

    const validIds = new Set(surfaces.map(s => s.id));
    function _readDisplayList(listEl) {
      const displays = [];
      if (!listEl) return displays;
      listEl.querySelectorAll('[data-display-idx]').forEach(row => {
        let surfaceId = String((row.querySelector('[data-display-field="surface_id"]') || {}).value || '').trim();
        if (!validIds.has(surfaceId) && idMap.has(surfaceId)) surfaceId = idMap.get(surfaceId);
        if (!surfaceId || !validIds.has(surfaceId)) return;
        const label = String((row.querySelector('[data-display-field="label"]') || {}).value || '').trim();
        const display = {
          surface_id: surfaceId,
          label: label || `Display ${displays.length + 1}`,
          size: _csScaleFromInput((row.querySelector('[data-display-field="size"]') || {}).value),
        };
        displays.push(display);
      });
      return displays;
    }
    return {
      surfaces,
      surface_controls: _readDisplayList(displaysList),
    };
  }

  function _csValidate(cfg) {
    const ids = new Set();
    for (const surface of cfg.surfaces || []) {
      const id = String(surface.id || '').trim();
      if (!id) throw new Error('Every surface needs an ID.');
      if (ids.has(id)) throw new Error(`Duplicate surface ID: ${id}`);
      if (!['2x5', '3x5', '4x5', '2x4', '3x4', '4x4', '4x8'].includes(String(surface.layout || ''))) {
        throw new Error(`Surface "${id}" needs a valid size.`);
      }
      ids.add(id);
    }
    for (const display of cfg.surface_controls || []) {
      const id = String(display.surface_id || '').trim();
      if (!ids.has(id)) throw new Error(`Display references unknown surface ID: ${id}`);
      if (!String(display.label || '').trim()) throw new Error('Every display needs a label.');
    }
  }

  async function _csSaveNow() {
    const cfg = _csReadFromUi();
    _csValidate(cfg);
    _csSetStatus('', 'info');
    const res = await fetch('/api/companion-surfaces-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) ? data.error : 'Save failed');
    }
    surfaceConfig = data.config || cfg;
    _csSetStatus('', 'info');
    return true;
  }

  function _csScheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      _csSaveNow().catch(e => _csSetStatus(String(e.message || e), 'danger'));
    }, 700);
  }

  async function _csLoad() {
    _csSetStatus('', 'info');
    try {
      const res = await fetch('/api/companion-surfaces-config?_ts=' + Date.now(), {cache: 'no-store'});
      const data = await res.json();
      if (!res.ok || !data || !Array.isArray(data.surfaces)) {
        throw new Error((data && data.error) ? data.error : 'Unable to load Companion surfaces.');
      }
      surfaceConfig = {
        surfaces: data.surfaces || [],
        surface_controls: data.surface_controls || [],
      };
      _csRender();
      _csSetStatus('', 'info');
    } catch (e) {
      _csSetStatus(String(e.message || e), 'danger');
    }
  }

  if (addSurfaceBtn) addSurfaceBtn.addEventListener('click', () => {
    surfaceConfig = _csReadFromUi();
    surfaceConfig.surfaces.push(_csNewSurface());
    _csRender();
    _csScheduleSave();
  });

  document.addEventListener('input', ev => {
    const target = ev.target;
    if (!target || !document.getElementById('companion-surfaces-config-page').contains(target)) return;
    _csScheduleSave();
  });

  document.addEventListener('change', ev => {
    const target = ev.target;
    if (!target || !document.getElementById('companion-surfaces-config-page').contains(target)) return;
    _csScheduleSave();
  });

  document.addEventListener('click', ev => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button') : null;
    if (!btn || !document.getElementById('companion-surfaces-config-page').contains(btn)) return;

    const surfaceRow = btn.closest('[data-surface-idx]');
    const displayRow = btn.closest('[data-display-idx]');
    surfaceConfig = _csReadFromUi();

    if (surfaceRow) {
      const idx = parseInt(surfaceRow.getAttribute('data-surface-idx') || '-1', 10);
      if (btn.hasAttribute('data-surface-delete')) {
        const removed = surfaceConfig.surfaces[idx];
        const removedId = removed ? String(removed.id || '') : '';
        if ((surfaceConfig.surface_controls || []).some(d => String(d.surface_id || '') === removedId)) {
          _csSetStatus(`Surface "${removedId}" is being used by a display slot. Change that slot first.`, 'danger');
          return;
        }
        surfaceConfig.surfaces.splice(idx, 1);
      } else {
        return;
      }
      _csRender();
      _csScheduleSave();
    } else if (displayRow) {
      return;
    }
  });

  _csLoad();
}

if (document.getElementById('foyer-audio-page')) {
  const root = document.getElementById('foyer-audio-page');
  const grid = document.getElementById('foyer-audio-grid');
  const monitorEl = document.getElementById('foyer-audio-monitor');
  const emptyEl = document.getElementById('foyer-audio-empty');
  const statusEl = document.getElementById('foyer-audio-status');
  let stateSources = [];
  let monitorState = {};
  let sourceSignature = '';
  const volumeState = new Map();
  const VOLUME_SEND_INTERVAL_MS = 60;
  const METER_REFRESH_MS = 350;

  function _foyerJsonAttr(name, fallback) {
    try {
      return JSON.parse(root.getAttribute(name) || JSON.stringify(fallback));
    } catch (e) {
      return fallback;
    }
  }

  const allowAll = !!_foyerJsonAttr('data-allow-all', false);
  const canSolo = !!_foyerJsonAttr('data-can-solo', false);
  const canMonitor = !!_foyerJsonAttr('data-can-monitor', false);
  const allowedIds = new Set((_foyerJsonAttr('data-allowed-source-ids', []) || []).map(v => String(v)));

  function _foyerSetStatus(text, type) {
    if (!statusEl) return;
    const msg = String(text || '').trim();
    if (!msg) {
      statusEl.innerHTML = '';
      return;
    }
    const cls = type === 'danger' ? 'danger' : (type === 'warning' ? 'warning' : 'info');
    statusEl.innerHTML = `<div class="alert alert-${cls} py-2 mb-0">${_escapeHtml(msg)}</div>`;
  }

  function _sourceVisible(source) {
    if (allowAll) return true;
    return allowedIds.has(String(source && source.id));
  }

  function _formatDb(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= -59.9) return '-inf';
    return `${n >= 0 ? '+' : ''}${n.toFixed(1)} dB`;
  }

  function _pctFromDb(db) {
    const n = Math.max(-60, Math.min(Number(db) || 0, 6));
    return Math.round(((n + 60) / 66) * 100);
  }

  function _meterPct(db) {
    const n = Math.max(-60, Math.min(Number(db), 6));
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.min(100, ((n + 60) / 66) * 100));
  }

  function _meterClass(db) {
    const n = Number(db);
    if (!Number.isFinite(n)) return '';
    if (n >= -6) return ' foyer-audio-meter-hot';
    if (n >= -18) return ' foyer-audio-meter-warm';
    return '';
  }

  function _sourcesSignature(sources) {
    return (sources || []).filter(_sourceVisible).map(s => `${String(s.id)}:${String(s.label || '')}:${String(!!s.muted)}:${String(s.mixOption || '')}`).join('|');
  }

  function _clampedVolume(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    return Math.max(-60, Math.min(n, 6));
  }

  function _renderMeterLane(id, side, db) {
    const pct = _meterPct(db);
    return `
      <div class="foyer-audio-meter-lane" aria-hidden="true">
        <span class="foyer-audio-meter-label">${side}</span>
        <span class="foyer-audio-meter-track">
          <span class="foyer-audio-meter-fill${_meterClass(db)}" data-meter-${side.toLowerCase()}="${_escapeHtml(id)}" style="width:${pct.toFixed(1)}%"></span>
        </span>
      </div>
    `;
  }

  function _renderSource(source) {
    const id = String(source.id || '');
    const volume = Number(source.volume || 0);
    const muted = !!source.muted;
    const isMaster = id === 'master';
    const soloActive = !!monitorState.solo && String(monitorState.soloSource || '') === id;
    const level = source.level || {};
    return `
      <section class="foyer-audio-strip" data-source-id="${_escapeHtml(id)}">
        <div class="foyer-audio-strip-head">
          <div class="foyer-audio-name">${_escapeHtml(source.label || id)}</div>
          <div class="foyer-audio-value" data-volume-readout="${_escapeHtml(id)}">${_escapeHtml(_formatDb(volume))}</div>
        </div>
        <div class="foyer-audio-meter" data-meter-source="${_escapeHtml(id)}">
          ${_renderMeterLane(id, 'L', level.left)}
          ${_renderMeterLane(id, 'R', level.right)}
        </div>
        <div class="foyer-audio-slider-row">
          <input class="form-range foyer-audio-slider" type="range" min="-60" max="6" step="0.1" value="${String(Math.max(-60, Math.min(volume, 6)))}" data-foyer-volume="${_escapeHtml(id)}" aria-label="${_escapeHtml(source.label || id)} volume">
          <div class="foyer-audio-percent" data-volume-percent="${_escapeHtml(id)}">${_pctFromDb(volume)}%</div>
        </div>
        <div class="foyer-audio-actions">
          ${isMaster ? '' : `<button class="btn ${muted ? 'btn-outline-secondary' : 'btn-primary'}" type="button" data-foyer-mute="${_escapeHtml(id)}" aria-pressed="${muted ? 'false' : 'true'}">On</button>`}
          ${(!isMaster && canSolo) ? `<button class="btn ${soloActive ? 'btn-warning' : 'btn-outline-secondary'}" type="button" data-foyer-solo="${_escapeHtml(id)}">${soloActive ? 'Solo' : 'Solo'}</button>` : ''}
        </div>
      </section>
    `;
  }

  function _renderMonitor() {
    if (!monitorEl) return;
    if (!canMonitor) {
      monitorEl.innerHTML = '';
      return;
    }
    const volume = Number(monitorState.volume || 0);
    const enabled = !!monitorState.enabled;
    const dim = !!monitorState.dim;
    monitorEl.innerHTML = `
      <section class="foyer-audio-monitor">
        <div class="foyer-audio-monitor-head">
          <div>
            <div class="foyer-audio-monitor-title">Monitor</div>
            <div class="foyer-audio-monitor-subtitle">Headphone output</div>
          </div>
          <div class="foyer-audio-value" data-monitor-volume-readout>${_escapeHtml(_formatDb(volume))}</div>
        </div>
        <div class="foyer-audio-monitor-controls">
          <button class="btn ${enabled ? 'btn-primary' : 'btn-outline-secondary'}" type="button" data-foyer-monitor-toggle="enabled" aria-pressed="${enabled ? 'true' : 'false'}">On</button>
          <button class="btn ${dim ? 'btn-warning' : 'btn-outline-secondary'}" type="button" data-foyer-monitor-toggle="dim" aria-pressed="${dim ? 'true' : 'false'}">Dim</button>
          <div class="foyer-audio-monitor-slider">
            <input class="form-range foyer-audio-slider" type="range" min="-60" max="6" step="0.1" value="${String(Math.max(-60, Math.min(volume, 6)))}" data-foyer-monitor-volume aria-label="Monitor volume">
            <div class="foyer-audio-percent" data-monitor-volume-percent>${_pctFromDb(volume)}%</div>
          </div>
        </div>
      </section>
    `;
  }

  function _render() {
    const visible = stateSources.filter(_sourceVisible);
    _renderMonitor();
    if (grid) grid.innerHTML = visible.map(_renderSource).join('');
    if (emptyEl) emptyEl.classList.toggle('d-none', visible.length > 0);
    sourceSignature = _sourcesSignature(stateSources);
  }

  function _updateMeters(sources) {
    for (const source of (sources || [])) {
      if (!_sourceVisible(source)) continue;
      const id = String(source.id || '');
      const level = source.level || {};
      const left = document.querySelector(`[data-meter-l="${CSS.escape(id)}"]`);
      const right = document.querySelector(`[data-meter-r="${CSS.escape(id)}"]`);
      if (left) {
        left.style.width = `${_meterPct(level.left).toFixed(1)}%`;
        left.className = `foyer-audio-meter-fill${_meterClass(level.left)}`;
      }
      if (right) {
        right.style.width = `${_meterPct(level.right).toFixed(1)}%`;
        right.className = `foyer-audio-meter-fill${_meterClass(level.right)}`;
      }
    }
  }

  function _syncSourceControls(sources) {
    for (const source of (sources || [])) {
      if (!_sourceVisible(source)) continue;
      const id = String(source.id || '');
      const volume = _clampedVolume(source.volume);
      const activeVolumeId = String(root.getAttribute('data-active-volume') || '');
      const slider = document.querySelector(`[data-foyer-volume="${CSS.escape(id)}"]`);
      if (slider && activeVolumeId !== id) slider.value = String(volume);
      if (activeVolumeId !== id) {
        const readout = document.querySelector(`[data-volume-readout="${CSS.escape(id)}"]`);
        const pct = document.querySelector(`[data-volume-percent="${CSS.escape(id)}"]`);
        if (readout) readout.textContent = _formatDb(volume);
        if (pct) pct.textContent = `${_pctFromDb(volume)}%`;
      }

      const muteBtn = document.querySelector(`[data-foyer-mute="${CSS.escape(id)}"]`);
      if (muteBtn) {
        const muted = !!source.muted;
        muteBtn.classList.toggle('btn-primary', !muted);
        muteBtn.classList.toggle('btn-outline-secondary', muted);
        muteBtn.setAttribute('aria-pressed', muted ? 'false' : 'true');
      }

      const soloBtn = document.querySelector(`[data-foyer-solo="${CSS.escape(id)}"]`);
      if (soloBtn) {
        const soloActive = !!monitorState.solo && String(monitorState.soloSource || '') === id;
        soloBtn.classList.toggle('btn-warning', soloActive);
        soloBtn.classList.toggle('btn-outline-secondary', !soloActive);
      }
    }
  }

  function _syncMonitorControls() {
    if (!canMonitor) return;
    const activeVolumeId = String(root.getAttribute('data-active-volume') || '');
    const volume = _clampedVolume(monitorState.volume);
    const slider = document.querySelector('[data-foyer-monitor-volume]');
    if (slider && activeVolumeId !== 'monitor') slider.value = String(volume);
    if (activeVolumeId !== 'monitor') {
      const readout = document.querySelector('[data-monitor-volume-readout]');
      const pct = document.querySelector('[data-monitor-volume-percent]');
      if (readout) readout.textContent = _formatDb(volume);
      if (pct) pct.textContent = `${_pctFromDb(volume)}%`;
    }

    const onBtn = document.querySelector('[data-foyer-monitor-toggle="enabled"]');
    if (onBtn) {
      const enabled = !!monitorState.enabled;
      onBtn.classList.toggle('btn-primary', enabled);
      onBtn.classList.toggle('btn-outline-secondary', !enabled);
      onBtn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    }

    const dimBtn = document.querySelector('[data-foyer-monitor-toggle="dim"]');
    if (dimBtn) {
      const dim = !!monitorState.dim;
      dimBtn.classList.toggle('btn-warning', dim);
      dimBtn.classList.toggle('btn-outline-secondary', !dim);
      dimBtn.setAttribute('aria-pressed', dim ? 'true' : 'false');
    }
  }

  async function _loadState(options) {
    const meterOnly = !!(options && options.meterOnly);
    try {
      const res = await fetch('/api/atem/audio/state?_ts=' + Date.now(), {cache: 'no-store'});
      const data = await res.json().catch(() => ({}));
      if (!data || !Array.isArray(data.sources)) throw new Error((data && data.error) || 'Could not load ATEM audio state');
      const nextSources = data.sources;
      monitorState = data.monitor || {};
      if (data.ok === false && data.error) {
        _foyerSetStatus(data.error, 'warning');
      } else if (data.metering && data.metering.active) {
        _foyerSetStatus('', 'info');
      } else if (!meterOnly && data.metering && data.metering.enabled === false && data.metering.unavailableReason) {
        _foyerSetStatus(`Audio metering unavailable: ${data.metering.unavailableReason}`, 'warning');
      } else if (!meterOnly && data.metering && data.metering.enabled && !data.metering.active) {
        const suffix = data.metering.salnPacketsSent ? 'waiting for AMLv packets from the ATEM' : 'waiting to request levels from the ATEM';
        _foyerSetStatus(`Audio metering connected, ${suffix}.`, 'warning');
      } else if (!meterOnly) {
        _foyerSetStatus('', 'info');
      }
      const nextSignature = _sourcesSignature(nextSources);
      stateSources = nextSources;
      if (!meterOnly && !root.hasAttribute('data-volume-dragging')) {
        _render();
      } else if (nextSignature !== sourceSignature && !root.hasAttribute('data-volume-dragging')) {
        _render();
      } else {
        _updateMeters(nextSources);
        _syncSourceControls(nextSources);
        _syncMonitorControls();
      }
    } catch (e) {
      _foyerSetStatus(e && e.message ? e.message : 'Could not load ATEM audio state', 'danger');
    }
  }

  function _updateLocalVolume(id, db) {
    const source = stateSources.find(s => String(s.id) === String(id));
    if (source) source.volume = db;
    const readout = document.querySelector(`[data-volume-readout="${CSS.escape(String(id))}"]`);
    const pct = document.querySelector(`[data-volume-percent="${CSS.escape(String(id))}"]`);
    if (readout) readout.textContent = _formatDb(db);
    if (pct) pct.textContent = `${_pctFromDb(db)}%`;
  }

  function _updateLocalMonitorVolume(db) {
    monitorState.volume = db;
    const readout = document.querySelector('[data-monitor-volume-readout]');
    const pct = document.querySelector('[data-monitor-volume-percent]');
    if (readout) readout.textContent = _formatDb(db);
    if (pct) pct.textContent = `${_pctFromDb(db)}%`;
  }

  function _volumeEntry(id) {
    const key = String(id);
    let entry = volumeState.get(key);
    if (!entry) {
      entry = {lastSent: 0, timer: null, inFlight: false, queued: null};
      volumeState.set(key, entry);
    }
    return entry;
  }

  async function _sendVolumeRequest(key, db) {
    const isMonitor = key === 'monitor';
    const res = await fetch(isMonitor ? '/api/atem/audio/monitor' : '/api/atem/audio/volume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(isMonitor ? {volume: db} : {source_id: key, db}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || (isMonitor ? 'Monitor volume change failed' : 'Volume change failed'));
  }

  async function _sendVolumeNow(id, db) {
    const key = String(id);
    const entry = _volumeEntry(key);
    if (entry.inFlight) {
      entry.queued = db;
      return;
    }
    entry.inFlight = true;
    entry.lastSent = Date.now();
    try {
      await _sendVolumeRequest(key, db);
    } catch (e) {
      _foyerSetStatus(e && e.message ? e.message : (key === 'monitor' ? 'Monitor volume change failed' : 'Volume change failed'), 'danger');
    } finally {
      entry.inFlight = false;
      if (entry.queued !== null) {
        const queued = entry.queued;
        entry.queued = null;
        _sendVolume(key, queued, {force: true});
      }
    }
  }

  function _sendVolume(id, db, opts) {
    const key = String(id);
    const entry = _volumeEntry(key);
    const force = !!(opts && opts.force);
    if (entry.timer) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    const elapsed = Date.now() - Number(entry.lastSent || 0);
    const wait = force ? 0 : Math.max(0, VOLUME_SEND_INTERVAL_MS - elapsed);
    entry.timer = setTimeout(() => {
      entry.timer = null;
      _sendVolumeNow(key, db);
    }, wait);
  }

  function _flushVolumeFromSlider(slider) {
    if (!slider) return;
    const id = String(slider.getAttribute('data-foyer-volume') || '');
    if (!id) return;
    const db = Number(slider.value);
    _updateLocalVolume(id, db);
    _sendVolume(id, db, {force: true});
  }

  function _flushMonitorVolumeFromSlider(slider) {
    if (!slider || !canMonitor) return;
    const db = Number(slider.value);
    _updateLocalMonitorVolume(db);
    _sendVolume('monitor', db, {force: true});
  }

  function _clearDraggingSoon() {
    setTimeout(() => {
      root.removeAttribute('data-volume-dragging');
      root.removeAttribute('data-active-volume');
    }, 250);
  }

  async function _postAction(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || 'Action failed');
    return data;
  }

  root.addEventListener('input', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    root.setAttribute('data-volume-dragging', '1');
    if (monitorSlider) {
      root.setAttribute('data-active-volume', 'monitor');
      const db = Number(monitorSlider.value);
      _updateLocalMonitorVolume(db);
      _sendVolume('monitor', db);
      return;
    }
    const id = String(slider.getAttribute('data-foyer-volume') || '');
    root.setAttribute('data-active-volume', id);
    const db = Number(slider.value);
    _updateLocalVolume(id, db);
    _sendVolume(id, db);
  });

  root.addEventListener('change', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    if (monitorSlider) _flushMonitorVolumeFromSlider(monitorSlider);
    else _flushVolumeFromSlider(slider);
    _clearDraggingSoon();
  });

  root.addEventListener('pointerup', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    if (monitorSlider) _flushMonitorVolumeFromSlider(monitorSlider);
    else _flushVolumeFromSlider(slider);
    _clearDraggingSoon();
  });

  root.addEventListener('touchend', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    if (monitorSlider) _flushMonitorVolumeFromSlider(monitorSlider);
    else _flushVolumeFromSlider(slider);
    _clearDraggingSoon();
  }, {passive: true});

  root.addEventListener('keydown', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)) return;
    setTimeout(() => {
      if (monitorSlider) _flushMonitorVolumeFromSlider(monitorSlider);
      else _flushVolumeFromSlider(slider);
    }, 0);
  });

  root.addEventListener('blur', (e) => {
    const slider = e.target && e.target.closest ? e.target.closest('[data-foyer-volume]') : null;
    const monitorSlider = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-volume]') : null;
    if (!slider && !monitorSlider) return;
    if (monitorSlider) _flushMonitorVolumeFromSlider(monitorSlider);
    else _flushVolumeFromSlider(slider);
    _clearDraggingSoon();
  }, true);

  root.addEventListener('click', async (e) => {
    const muteBtn = e.target && e.target.closest ? e.target.closest('[data-foyer-mute]') : null;
    const soloBtn = e.target && e.target.closest ? e.target.closest('[data-foyer-solo]') : null;
    const monitorToggle = e.target && e.target.closest ? e.target.closest('[data-foyer-monitor-toggle]') : null;
    try {
      if (muteBtn) {
        const id = String(muteBtn.getAttribute('data-foyer-mute') || '');
        const source = stateSources.find(s => String(s.id) === id);
        const muted = !(source && source.muted);
        await _postAction('/api/atem/audio/mute', {source_id: id, mix_option: muted ? 'off' : 'on'});
        if (source) {
          source.muted = muted;
          source.mixOption = muted ? 'off' : 'on';
        }
        _render();
      } else if (soloBtn) {
        const id = String(soloBtn.getAttribute('data-foyer-solo') || '');
        const enabled = !(monitorState.solo && String(monitorState.soloSource || '') === id);
        await _postAction('/api/atem/audio/solo', {source_id: id, enabled});
        monitorState = {...monitorState, solo: enabled, soloSource: enabled ? id : ''};
        _render();
      } else if (monitorToggle && canMonitor) {
        const field = String(monitorToggle.getAttribute('data-foyer-monitor-toggle') || '');
        if (!['enabled', 'dim'].includes(field)) return;
        const enabled = !monitorState[field];
        await _postAction('/api/atem/audio/monitor', {[field]: enabled});
        monitorState[field] = enabled;
        _renderMonitor();
      }
    } catch (err) {
      _foyerSetStatus(err && err.message ? err.message : 'Action failed', 'danger');
    }
  });

  _loadState();
  setInterval(() => _loadState({meterOnly: true}), METER_REFRESH_MS);
}

