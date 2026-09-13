function initializeRoutingPresetsConfig() {
  const root = document.getElementById('routing-presets-config');
  if (!root) return;
  const el = id => document.getElementById(id);
  const canEdit = root.dataset.canEdit === 'true';
  let presets = [], images = [], outputs = [], selected = null, busy = false, sequence = 0;
  function node(tag, className, copy) { const result = document.createElement(tag); if (className) result.className = className; if (copy) result.textContent = copy; return result; }
  function message(copy, error) { el('preset-config-message').textContent = copy; el('preset-config-message').className = copy ? 'alert alert-' + (error ? 'danger' : 'success') : ''; }
  function controls() { el('preset-fields').disabled = busy || !canEdit; el('preset-new').disabled = busy || !canEdit; el('preset-config-refresh').disabled = busy; root.querySelectorAll('[data-preset-select]').forEach(button => { button.disabled = busy; }); }
  async function request(path, options) {
    const response = await fetch(path, Object.assign({cache: 'no-store', credentials: 'same-origin'}, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.message || data.error || 'The request failed. Refresh and try again.');
    return data;
  }
  function preview() {
    const image = images.find(item => item.id === el('preset-image').value);
    el('preset-config-preview').classList.toggle('d-none', !image);
    if (image) el('preset-config-image').src = image.thumbnail_url;
  }
  function list() {
    el('preset-config-list').textContent = '';
    presets.forEach(item => {
      const button = node('button', 'list-group-item list-group-item-action', item.name + (item.enabled ? '' : ' (disabled)'));
      button.type = 'button'; button.dataset.presetSelect = item.id;
      button.classList.toggle('active', !!selected && selected.id === item.id);
      button.addEventListener('click', () => edit(item)); el('preset-config-list').appendChild(button);
    });
    if (!presets.length) el('preset-config-list').appendChild(node('p', 'text-muted', 'No presets yet. Create one for your team.'));
  }
  function addAction(action) {
    action = action || {label: '', method: 'POST', path: '', body: null};
    const row = node('div', 'border rounded p-3'); row.dataset.presetAction = 'true';
    sequence += 1;
    const fields = [
      ['label', 'Action description', 'input'], ['method', 'Method', 'select'],
      ['path', 'TDeck API path', 'input'], ['body', 'JSON body (optional)', 'textarea'],
    ];
    fields.forEach(([key, labelText, tag]) => {
      const input = node(tag, tag === 'select' ? 'form-select mb-2' : 'form-control mb-2');
      input.id = 'preset-action-' + sequence + '-' + key; input.dataset.actionField = key;
      const label = node('label', 'form-label', labelText); label.htmlFor = input.id;
      if (key === 'method') ['POST', 'GET', 'PUT', 'PATCH', 'DELETE'].forEach(method => input.appendChild(new Option(method, method)));
      input.value = key === 'body' ? (action.body === null ? '' : JSON.stringify(action.body, null, 2)) : action[key];
      if (key === 'path') input.placeholder = '/api/timers/apply';
      if (key === 'label') { input.placeholder = 'For example, turn on foyer TVs'; input.maxLength = 120; }
      if (key === 'body') { input.rows = 3; input.spellcheck = false; input.placeholder = '{"preset": 1}'; }
      else input.required = true;
      row.appendChild(label); row.appendChild(input);
    });
    const buttons = node('div', 'd-flex gap-2');
    [['Move up', () => { if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling); }],
     ['Move down', () => { if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row); }],
     ['Remove action', () => row.remove()]].forEach(([label, click]) => { const button = node('button', 'btn btn-sm btn-outline-secondary', label); button.type = 'button'; button.addEventListener('click', click); buttons.appendChild(button); });
    row.appendChild(buttons); el('preset-actions').appendChild(row);
  }
  function edit(item) {
    selected = item || null; const value = item || {name: '', description: '', media_id: '', output: null, enabled: true, actions: []};
    el('preset-editor-title').textContent = item ? 'Edit preset' : 'New preset';
    el('preset-name').value = value.name; el('preset-description').value = value.description;
    el('preset-enabled').checked = value.enabled; el('preset-image').value = value.media_id;
    el('preset-output').value = value.output === null ? '' : String(value.output);
    if (value.output && !el('preset-output').value) { el('preset-output').appendChild(new Option('Output ' + value.output + ' (not currently listed)', value.output)); el('preset-output').value = String(value.output); }
    el('preset-actions').textContent = ''; value.actions.forEach(addAction); el('preset-actions-panel').open = !!value.actions.length;
    el('preset-delete').classList.toggle('d-none', !selected); preview(); list(); controls();
  }
  async function load(identity, initial) {
    busy = true; controls();
    try {
      const data = await request('/api/config/routing-presets'); presets = data.presets; images = data.images; outputs = data.outputs;
      el('preset-image').textContent = ''; el('preset-image').appendChild(new Option('Choose an image', ''));
      images.forEach(item => el('preset-image').appendChild(new Option(item.name, item.id)));
      el('preset-output').textContent = ''; el('preset-output').appendChild(new Option('Ask the user to choose', ''));
      outputs.forEach(item => el('preset-output').appendChild(new Option(item.number + ': ' + (item.label || 'Output ' + item.number), item.number)));
      edit(presets.find(item => item.id === identity));
      if (initial) {
        const imageId = new URLSearchParams(window.location.search).get('image');
        const image = images.find(item => item.id === imageId);
        if (image) { el('preset-image').value = image.id; el('preset-name').value = image.name; preview(); }
      }
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }
  el('preset-editor').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !canEdit) return;
    try {
      const actions = Array.from(el('preset-actions').children).map(row => {
        const get = key => row.querySelector('[data-action-field="' + key + '"]').value;
        let body = null;
        try { if (get('body').trim()) body = JSON.parse(get('body')); } catch (_) { throw new Error('Enter valid JSON for “' + get('label') + '”.'); }
        return {label: get('label'), method: get('method'), path: get('path'), body: body};
      });
      const body = {name: el('preset-name').value, description: el('preset-description').value,
        media_id: el('preset-image').value, output: Number(el('preset-output').value) || null,
        enabled: el('preset-enabled').checked, actions: actions};
      if (selected) body.revision = selected.revision;
      busy = true; controls();
      const result = await request('/api/config/routing-presets' + (selected ? '/' + selected.id : ''),
        {method: selected ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      message('Preset saved. Assign it to the appropriate groups in Permissions.'); await load(result.preset.id);
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  el('preset-delete').addEventListener('click', async () => {
    if (busy || !canEdit || !selected || !window.confirm('Delete “' + selected.name + '”?')) return;
    busy = true; controls();
    try { await request('/api/config/routing-presets/' + selected.id, {method: 'DELETE', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: selected.revision})}); message('Preset deleted.'); await load(); }
    catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  el('preset-new').addEventListener('click', () => { message(''); edit(); el('preset-name').focus(); });
  el('preset-config-refresh').addEventListener('click', () => load(selected && selected.id));
  el('preset-add-action').addEventListener('click', () => { if (el('preset-actions').children.length < 20) addAction(); });
  el('preset-image').addEventListener('change', preview);
  load(null, true);
}
initializeRoutingPresetsConfig();
