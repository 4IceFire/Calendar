// Poll companion status endpoint and update indicator
async function updateCompanion() {
  try {
    const res = await fetch('/api/companion_status');
    if (!res.ok) throw new Error('fetch failed');
    const data = await res.json();
    const dot = document.getElementById('companion-dot');
    const label = document.getElementById('companion-label');
    if (data.connected) {
      dot.classList.remove('bg-danger');
      dot.classList.add('bg-success');
      label.textContent = 'Companion: Online';
    } else {
      dot.classList.remove('bg-success');
      dot.classList.add('bg-danger');
      label.textContent = 'Companion: Offline';
    }
  } catch (e) {
    const dot = document.getElementById('companion-dot');
    const label = document.getElementById('companion-label');
    dot.classList.remove('bg-success');
    dot.classList.add('bg-danger');
    label.textContent = 'Companion: Unknown';
  }
}

// Poll ProPresenter status endpoint and update indicator
async function updateProPresenter() {
  try {
    const res = await fetch('/api/propresenter_status');
    if (!res.ok) throw new Error('fetch failed');
    const data = await res.json();
    const dot = document.getElementById('propresenter-dot');
    const label = document.getElementById('propresenter-label');
    if (data.connected) {
      dot.classList.remove('bg-danger');
      dot.classList.add('bg-success');
      label.textContent = 'ProPresenter: Online';
    } else {
      dot.classList.remove('bg-success');
      dot.classList.add('bg-danger');
      label.textContent = 'ProPresenter: Offline';
    }
  } catch (e) {
    const dot = document.getElementById('propresenter-dot');
    const label = document.getElementById('propresenter-label');
    dot.classList.remove('bg-success');
    dot.classList.add('bg-danger');
    label.textContent = 'ProPresenter: Unknown';
  }
}

// initial check
updateCompanion();
updateProPresenter();
// refresh every 10s for more responsive UI
setInterval(updateCompanion, 10000);
setInterval(updateProPresenter, 10000);

// --- Config page ---
function _configSetStatus(msg, kind) {
  const el = document.getElementById('config-status');
  if (!el) return;
  let cls;
  if (kind === 'error') cls = 'alert alert-danger';
  else if (kind === 'warn') cls = 'alert alert-warning';
  else cls = 'alert alert-success';

  el.className = cls;

  // allow html for link/warning UX
  el.innerHTML = msg;
}

function _configClearStatus() {
  const el = document.getElementById('config-status');
  if (!el) return;
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
    input.type = 'text';
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
  const container = document.getElementById('config-groups');
  if (!container) return;
  container.innerHTML = '';

  const groups = [
    {
      title: 'Web UI',
      keys: ['webserver_port', 'server_port', 'poll_interval', 'debug', 'dark_mode'],
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
      title: 'Scheduling',
      keys: ['EVENTS_FILE'],
    },
  ];

  const used = new Set();

  for (const g of groups) {
    const presentKeys = (g.keys || []).filter(k => Object.prototype.hasOwnProperty.call(cfg, k));
    if (!presentKeys.length) continue;
    for (const k of presentKeys) used.add(k);

    const card = document.createElement('div');
    card.className = 'card mb-3';
    const body = document.createElement('div');
    body.className = 'card-body';
    const h = document.createElement('h2');
    h.className = 'h5';
    h.textContent = g.title;

    body.appendChild(h);
    for (const k of presentKeys) {
      body.appendChild(_renderConfigField(k, cfg[k]));
    }
    card.appendChild(body);
    container.appendChild(card);
  }

  // Everything else (sorted) so we truly show all variables.
  const otherKeys = Object.keys(cfg || {}).filter(k => !used.has(k)).sort();
  if (otherKeys.length) {
    const card = document.createElement('div');
    card.className = 'card mb-3';
    const body = document.createElement('div');
    body.className = 'card-body';
    const h = document.createElement('h2');
    h.className = 'h5';
    h.textContent = 'Other';
    body.appendChild(h);
    for (const k of otherKeys) {
      body.appendChild(_renderConfigField(k, cfg[k]));
    }
    card.appendChild(body);
    container.appendChild(card);
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

  (async () => {
    try {
      const res = await fetch('/api/config');
      if (!res.ok) throw new Error('Failed to load config');
      const cfg = await res.json();
      _configOriginal = cfg || {};
      _renderConfigGroups(_configOriginal);
    } catch (e) {
      _configSetStatus(String(e.message || e), 'error');
    }
  })();

  const saveBtn = document.getElementById('config-save');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      _configClearStatus();
      saveBtn.disabled = true;
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

        // Apply theme immediately (no reload required)
        if (Object.prototype.hasOwnProperty.call(_configOriginal, 'dark_mode')) {
          const theme = _configOriginal.dark_mode ? 'dark' : 'light';
          document.documentElement.setAttribute('data-bs-theme', theme);
        }

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
      } catch (e) {
        _configSetStatus(String(e.message || e), 'error');
      } finally {
        saveBtn.disabled = false;
      }
    });
  }
}

// --- Console page ---
function _consoleSetStatus(msg, kind) {
  const el = document.getElementById('console-status');
  if (!el) return;
  if (!msg) {
    el.className = '';
    el.textContent = '';
    return;
  }
  const cls = kind === 'error' ? 'alert alert-danger' : 'alert alert-success';
  el.className = cls;
  el.textContent = msg;
}

if (document.getElementById('console-page')) {
  const logEl = document.getElementById('console-log');
  const cmdEl = document.getElementById('console-command');
  const runBtn = document.getElementById('console-run');
  let since = 0;
  let polling = false;

  function _appendToLog(text) {
    if (!logEl || text == null) return;
    logEl.textContent += String(text);
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function pollConsole() {
    if (polling) return;
    polling = true;
    try {
      const res = await fetch(`/api/console/logs?since=${encodeURIComponent(String(since))}`);
      if (!res.ok) throw new Error('Failed to load console logs');
      const data = await res.json();
      if (!data || !data.ok) throw new Error(data && data.error ? data.error : 'Failed to load console logs');
      const lines = data.lines || [];
      if (lines.length) {
        const rendered = lines.map((ln) => {
          // Backward-compatible with older string-only API.
          if (typeof ln === 'string') return ln;
          const ts = String(ln.ts || '').trim();
          const text = String(ln.text || '');
          // Prefix every captured line with date/time.
          return ts ? `${ts} ${text}` : text;
        }).join('');
        _appendToLog(rendered);
      }
      since = Number(data.next || since) || since;
    } catch (e) {
      // Don't spam the UI; just show the latest error.
      _consoleSetStatus(String(e.message || e), 'error');
    } finally {
      polling = false;
    }
  }

  async function runCommand() {
    const cmd = String((cmdEl && cmdEl.value) || '').trim();
    if (!cmd) return;
    _consoleSetStatus('', 'ok');
    if (runBtn) runBtn.disabled = true;
    try {
      const res = await fetch('/api/console/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: cmd}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || 'Command failed');
      }
      // The server also appends output to the live log buffer; polling will show it.
      _consoleSetStatus(`Exit code: ${data.exit_code}`, 'ok');
      if (cmdEl) cmdEl.value = '';
      // Immediately poll once so output appears quickly.
      await pollConsole();
    } catch (e) {
      _consoleSetStatus(String(e.message || e), 'error');
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  }

  // initial load + poll
  pollConsole();
  setInterval(pollConsole, 1000);

  if (runBtn) {
    runBtn.addEventListener('click', runCommand);
  }
  if (cmdEl) {
    cmdEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runCommand();
      }
    });
  }
}

// --- Timers page ---
let _timersButtonTemplates = [];

function _timersSetStatus(msg, kind) {
  const el = document.getElementById('timers-status');
  if (!el) return;
  const cls = kind === 'error' ? 'alert alert-danger' : 'alert alert-success';
  el.className = cls;
  el.textContent = msg;
}

function _timersClearStatus() {
  const el = document.getElementById('timers-status');
  if (!el) return;
  el.className = '';
  el.textContent = '';
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

function _timersRenderPresets(presets) {
  const body = document.getElementById('timers-presets-body');
  if (!body) return;

  body.innerHTML = '';
  (presets || []).forEach((t, idx) => {
    const tr = document.createElement('tr');
    tr.dataset.index = String(idx);

    const presetObj = (t && typeof t === 'object') ? t : {time: String(t || ''), name: ''};

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
    input.value = String(presetObj.time || '').trim() || '00:00';
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

  const res = await fetch('/api/timers');
  if (!res.ok) throw new Error('Failed to load timers');
  const data = await res.json();

  _timersRenderPresets(data.timer_presets || []);
}

async function _timersSave() {
  _timersClearStatus();

  const presets = _timersReadPresetsFromUI();
  const payload = {
    timer_presets: presets,
  };

  const res = await fetch('/api/timers', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || 'Save failed');
  }

  _timersSetStatus('Saved.', 'ok');
  _timersRenderPresets(data.timer_presets || presets);
}

if (document.getElementById('timers-page')) {
  // Initial load
  _timersLoad().catch(e => _timersSetStatus(String(e.message || e), 'error'));

  // Save
  const saveBtn = document.getElementById('timers-save');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      _timersSave().catch(e => _timersSetStatus(String(e.message || e), 'error'));
    });
  }

  // Add
  const addBtn = document.getElementById('timers-add');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      const presets = _timersReadPresetsFromUI();
      presets.push({time: '00:00', name: ''});
      _timersRenderPresets(presets);
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
        return;
      }

      // Preset-row actions
      const action = btn.dataset.action;
      if (!action || !['delete', 'up', 'down'].includes(action)) return;
      const tr = btn.closest('tr');
      if (!tr) return;
      const idx = Number(tr.dataset.index);
      if (!Number.isFinite(idx)) return;
      const presets = _timersReadPresetsFromUI();

      if (action === 'delete') {
        presets.splice(idx, 1);
      } else if (action === 'up' && idx > 0) {
        const tmp = presets[idx - 1];
        presets[idx - 1] = presets[idx];
        presets[idx] = tmp;
      } else if (action === 'down' && idx < presets.length - 1) {
        const tmp = presets[idx + 1];
        presets[idx + 1] = presets[idx];
        presets[idx] = tmp;
      }

      _timersRenderPresets(presets);
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
    });
  }
}
