(function () {
  const root = document.getElementById('ccb-config-page');
  if (!root) return;

  let model = { credentials: {}, settings: {}, categories: [], roles: [], positions: [], connection: {} };
  const byId = (id) => document.getElementById(id);
  const statusEl = byId('ccb-config-status');

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }

  function status(message, kind = 'info') {
    if (!statusEl) return;
    statusEl.innerHTML = message ? `<div class="alert alert-${kind} py-2">${escapeHtml(message)}</div>` : '';
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function populate() {
    byId('ccb-client-id').value = model.credentials?.client_id || '';
    byId('ccb-client-secret').value = '';
    byId('ccb-subdomain').value = model.credentials?.subdomain || '';
    byId('ccb-companion-token').value = '';
    byId('ccb-redirect-uri').value = model.settings?.oauth_redirect_uri || '';
    byId('ccb-scopes').value = model.settings?.oauth_scopes || '';
    byId('ccb-upcoming-days').value = Number(model.settings?.upcoming_days || 45);
    byId('ccb-past-days').value = Number(model.settings?.past_days ?? 1);
    byId('ccb-default-callback').textContent = model.default_callback_url || '';
    const badge = byId('ccb-connection-badge');
    if (model.connection?.connected) {
      badge.className = 'badge text-bg-success';
      badge.textContent = 'Connected';
    } else if (model.connection?.configured) {
      badge.className = 'badge text-bg-warning';
      badge.textContent = 'Configured, authorization needed';
    } else {
      badge.className = 'badge text-bg-secondary';
      badge.textContent = 'Not configured';
    }
    renderCategories();
    renderRoles();
    renderPositions();
  }

  function renderCategories() {
    const list = byId('ccb-category-list');
    const enabled = new Set((model.settings?.enabled_category_ids || []).map(String));
    if (!model.categories?.length) {
      list.innerHTML = '<div class="col-12 text-muted small">Connect CCB, then refresh the category list.</div>';
      return;
    }
    list.innerHTML = model.categories.map((category) => `
      <div class="col-12 col-md-6 col-xl-4">
        <div class="form-check border rounded p-2 ps-5 h-100">
          <input class="form-check-input" type="checkbox" data-ccb-category value="${escapeHtml(category.id)}" id="ccb-category-${escapeHtml(category.id)}" ${enabled.has(String(category.id)) ? 'checked' : ''}>
          <label class="form-check-label" for="ccb-category-${escapeHtml(category.id)}">${escapeHtml(category.name)}${category.status ? ` <span class="text-muted">(${escapeHtml(category.status)})</span>` : ''}</label>
        </div>
      </div>`).join('');
  }

  function renderRoles() {
    const rows = byId('ccb-role-rows');
    rows.innerHTML = (model.roles || []).map((role, index) => `
      <tr data-role-index="${index}" data-role-key="${escapeHtml(role.role_key || '')}">
        <td><input class="form-control form-control-sm" data-field="name" value="${escapeHtml(role.name || '')}"></td>
        <td><select class="form-select form-select-sm" data-field="display_category">
          ${['Singers', 'Band', 'Production'].map((value) => `<option ${role.display_category === value ? 'selected' : ''}>${value}</option>`).join('')}
        </select></td>
        <td><input class="form-control form-control-sm" data-field="allocation_pool" value="${escapeHtml(role.allocation_pool || '')}" placeholder="blank or vocals"></td>
        <td><input class="form-control form-control-sm" data-field="sort_order" type="number" value="${Number(role.sort_order || ((index + 1) * 10))}"></td>
        <td class="text-center"><input class="form-check-input" data-field="is_active" type="checkbox" ${role.is_active !== false && Number(role.is_active) !== 0 ? 'checked' : ''}></td>
      </tr>`).join('');
  }

  function roleChoices(selected) {
    return (model.roles || []).filter((role) => role.is_active !== false && Number(role.is_active) !== 0).map((role) =>
      `<option value="${escapeHtml(role.role_key)}" ${String(selected || '') === String(role.role_key) ? 'selected' : ''}>${escapeHtml(role.name)}</option>`
    ).join('');
  }

  function renderPositions() {
    const rows = byId('ccb-position-rows');
    rows.innerHTML = (model.positions || []).map((position, index) => {
      const mode = position.mapping_mode || 'display';
      let mapping = '<span class="text-muted small">No authorization mapping</span>';
      if (mode === 'direct') mapping = `<select class="form-select form-select-sm" data-position-field="tdeck_role_key"><option value="">Choose role</option>${roleChoices(position.tdeck_role_key)}</select>`;
      if (mode === 'pool') mapping = `<input class="form-control form-control-sm" data-position-field="pool_name" value="${escapeHtml(position.pool_name || 'vocals')}" placeholder="Pool name">`;
      return `<tr data-position-index="${index}">
        <td><input class="form-control form-control-sm" data-position-field="name" value="${escapeHtml(position.name || '')}"></td>
        <td><select class="form-select form-select-sm" data-position-field="mapping_mode">
          <option value="direct" ${mode === 'direct' ? 'selected' : ''}>Direct role</option>
          <option value="pool" ${mode === 'pool' ? 'selected' : ''}>Candidate pool</option>
          <option value="display" ${mode === 'display' ? 'selected' : ''}>Display only</option>
        </select></td>
        <td>${mapping}</td>
        <td class="small text-muted">${escapeHtml(position.last_seen_at || 'Not seen yet')}</td>
      </tr>`;
    }).join('');
    rows.querySelectorAll('select[data-position-field="mapping_mode"]').forEach((select) => select.addEventListener('change', () => {
      readPositions();
      renderPositions();
    }));
  }

  function readRoles() {
    model.roles = Array.from(byId('ccb-role-rows').querySelectorAll('tr[data-role-index]')).map((row) => ({
      role_key: row.dataset.roleKey || '',
      name: row.querySelector('[data-field="name"]').value.trim(),
      display_category: row.querySelector('[data-field="display_category"]').value,
      allocation_pool: row.querySelector('[data-field="allocation_pool"]').value.trim(),
      sort_order: Number(row.querySelector('[data-field="sort_order"]').value || 0),
      is_active: row.querySelector('[data-field="is_active"]').checked,
    }));
  }

  function readPositions() {
    model.positions = Array.from(byId('ccb-position-rows').querySelectorAll('tr[data-position-index]')).map((row, index) => {
      const original = model.positions[Number(row.dataset.positionIndex || index)] || {};
      return {
        ...original,
        name: row.querySelector('[data-position-field="name"]')?.value.trim() || '',
        mapping_mode: row.querySelector('[data-position-field="mapping_mode"]')?.value || 'display',
        tdeck_role_key: row.querySelector('[data-position-field="tdeck_role_key"]')?.value || '',
        pool_name: row.querySelector('[data-position-field="pool_name"]')?.value.trim() || '',
      };
    });
  }

  function requestBody() {
    readRoles();
    readPositions();
    return {
      credentials: {
        client_id: byId('ccb-client-id').value.trim(),
        client_secret: byId('ccb-client-secret').value,
        subdomain: byId('ccb-subdomain').value.trim(),
        companion_api_token: byId('ccb-companion-token').value,
      },
      settings: {
        oauth_redirect_uri: byId('ccb-redirect-uri').value.trim(),
        oauth_scopes: byId('ccb-scopes').value.trim(),
        upcoming_days: Number(byId('ccb-upcoming-days').value || 45),
        past_days: Number(byId('ccb-past-days').value || 1),
        enabled_category_ids: Array.from(document.querySelectorAll('[data-ccb-category]:checked')).map((el) => Number(el.value)),
      },
      roles: model.roles,
      positions: model.positions,
    };
  }

  async function save(showMessage = true) {
    const payload = await api('/api/ccb/config', { method: 'PUT', body: JSON.stringify(requestBody()) });
    model = payload;
    populate();
    if (showMessage) status('CCB configuration saved.', 'success');
  }

  byId('ccb-config-save').addEventListener('click', () => save().catch((error) => status(error.message, 'danger')));
  byId('ccb-connect').addEventListener('click', async () => {
    try {
      await save(false);
      window.location.href = root.dataset.connectUrl;
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('ccb-test').addEventListener('click', async () => {
    try {
      await save(false);
      const result = await api('/api/ccb/config/test', { method: 'POST' });
      status(`CCB connection succeeded. ${result.category_count} scheduling categories are available.`, 'success');
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('ccb-refresh-categories').addEventListener('click', async () => {
    try {
      const result = await api('/api/ccb/config/categories/refresh', { method: 'POST' });
      model.categories = result.categories || [];
      renderCategories();
      status('Scheduling categories refreshed. Select the categories to use, then save.', 'success');
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('ccb-generate-token').addEventListener('click', async () => {
    try {
      const result = await api('/api/ccb/config/companion-token', { method: 'POST' });
      byId('ccb-companion-token').type = 'text';
      byId('ccb-companion-token').value = result.token || '';
      status('New Companion token generated. Copy it into Companion now; saving will preserve it.', 'warning');
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('ccb-add-role').addEventListener('click', () => {
    readRoles();
    model.roles.push({ role_key: '', name: 'New Role', display_category: 'Production', allocation_pool: '', sort_order: (model.roles.length + 1) * 10, is_active: true });
    renderRoles();
  });
  byId('ccb-add-position').addEventListener('click', () => {
    readPositions();
    model.positions.push({ name: 'New CCB Position', mapping_mode: 'display', tdeck_role_key: '', pool_name: '' });
    renderPositions();
  });

  api('/api/ccb/config').then((payload) => {
    model = payload;
    populate();
    const params = new URLSearchParams(window.location.search);
    if (params.get('connected')) status('CCB authorization completed successfully.', 'success');
    if (params.get('error')) status(params.get('error'), 'danger');
  }).catch((error) => status(error.message, 'danger'));
})();
