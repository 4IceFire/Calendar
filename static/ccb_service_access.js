(function () {
  const root = document.getElementById('ccb-service-access-page');
  if (!root) return;

  const byId = (id) => document.getElementById(id);
  let services = [];
  let state = null;
  let busy = false;

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }

  function status(message, kind = 'info') {
    byId('ccb-service-status').innerHTML = message ? `<div class="alert alert-${kind} py-2">${escapeHtml(message)}</div>` : '';
  }

  function setBusy(value) {
    busy = Boolean(value);
    root.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  async function api(path, options = {}, acceptPartial = false) {
    const response = await fetch(path, {
      ...options,
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || (!acceptPartial && payload.ok === false)) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function serviceLabel(service) {
    const when = formatDateTime(service.start);
    return `${when ? `${when} — ` : ''}${service.name || `Service ${service.id}`}${service.schedule_name && service.schedule_name !== service.name ? ` (${service.schedule_name})` : ''}`;
  }

  function formatDateTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }).format(date);
  }

  function formatTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(date);
  }

  function formatDuration(seconds) {
    let remaining = Math.max(0, Number(seconds || 0));
    const minutes = Math.floor(remaining / 60);
    remaining = Math.floor(remaining % 60);
    return `${minutes}:${String(remaining).padStart(2, '0')}`;
  }

  function selectedServiceId() {
    return Number(byId('ccb-service-select').value || 0);
  }

  function sourceBody() {
    return {
      roster: Number(byId('ccb-roster-source').value || selectedServiceId()),
      runsheet: Number(byId('ccb-runsheet-source').value || selectedServiceId()),
    };
  }

  function renderServiceChoices(preserve = true) {
    const select = byId('ccb-service-select');
    const previous = preserve ? Number(select.value || 0) : 0;
    select.innerHTML = '<option value="">Select a service</option>' + services.map((service) =>
      `<option value="${service.event_id}">${escapeHtml(serviceLabel(service))}</option>`
    ).join('');
    const active = services.find((service) => service.active);
    const target = services.some((service) => service.event_id === previous) ? previous : (active?.event_id || services[0]?.event_id || 0);
    if (target) select.value = String(target);
    renderSourceChoices();
  }

  function renderSourceChoices() {
    const options = services.map((service) => `<option value="${service.event_id}">${escapeHtml(serviceLabel(service))}</option>`).join('');
    const selected = selectedServiceId();
    for (const id of ['ccb-roster-source', 'ccb-runsheet-source']) {
      const select = byId(id);
      const old = Number(select.value || 0);
      select.innerHTML = options;
      select.value = String(services.some((service) => service.event_id === old) ? old : selected);
    }
  }

  async function loadServices(refresh = false) {
    setBusy(true);
    try {
      const payload = await api(refresh ? '/api/ccb/services/refresh' : '/api/ccb/services/upcoming', { method: refresh ? 'POST' : 'GET' });
      services = payload.services || [];
      renderServiceChoices();
      if (!services.length) {
        status('No cached upcoming services. Check the selected scheduling categories in Config > CCB.', 'warning');
        clearState();
      } else {
        await loadState();
        if (refresh) status(`Refreshed ${services.length} upcoming services.`, 'success');
      }
    } catch (error) {
      status(error.message, 'danger');
    } finally { setBusy(false); }
  }

  function clearState() {
    state = null;
    byId('ccb-role-groups').innerHTML = '<div class="text-muted small">Select a service and pull roles/users.</div>';
    byId('ccb-position-list').innerHTML = '<div class="text-muted small">No roster has been pulled.</div>';
    byId('ccb-unmatched-list').innerHTML = '<div class="text-muted small">No roster has been pulled.</div>';
    byId('ccb-runsheet-rows').innerHTML = '<tr><td colspan="4" class="text-muted">No runsheet has been pulled.</td></tr>';
  }

  async function loadState() {
    const eventId = selectedServiceId();
    if (!eventId) return clearState();
    const payload = await api(`/api/ccb/services/${eventId}/assignments`);
    state = payload.state;
    const sources = state.sources || {};
    byId('ccb-roster-source').value = String(sources.roster || eventId);
    byId('ccb-runsheet-source').value = String(sources.runsheet || eventId);
    renderState();
    await loadRunsheet();
  }

  function renderState() {
    renderActive();
    renderRoles();
    renderPositions();
    renderUnmatched();
    const rosterBranch = state?.branches?.roster;
    const runsheetBranch = state?.branches?.runsheet;
    const errors = [rosterBranch, runsheetBranch].filter((branch) => branch?.status === 'failure' && branch?.error).map((branch) => branch.error);
    if (errors.length) status(`Last pull warning: ${errors.join(' | ')}`, 'warning');
  }

  function renderActive() {
    const active = state?.active_service;
    if (!active) {
      byId('ccb-active-service').innerHTML = '<span class="badge text-bg-secondary">No active service access</span> Practice fallback groups are enabled.';
      return;
    }
    byId('ccb-active-service').innerHTML = `<span class="badge text-bg-success">Active</span> ${escapeHtml(active.service_name || `Service ${active.selected_event_id}`)} · applied ${escapeHtml(active.applied_at || '')}`;
  }

  function allocationIds(role) {
    return new Set((role.allocations || []).map((allocation) => String(allocation.ccb_individual_id)));
  }

  function candidatePeople(role) {
    const allocated = allocationIds(role);
    const pool = String(role.allocation_pool || '');
    const showAll = byId('ccb-show-all-vocalists').checked;
    return (state?.people || []).filter((person) => {
      const id = String(person.id);
      if (allocated.has(id)) return true;
      if (String(person.status || '').toUpperCase() === 'DECLINED') return true;
      if (pool) {
        if (!(person.pools || []).includes(pool)) return false;
        if (!showAll && ['ELIGIBLE', 'MANUAL'].includes(String(person.status || '').toUpperCase())) return false;
        return true;
      }
      return !['ELIGIBLE', 'MANUAL'].includes(String(person.status || '').toUpperCase());
    });
  }

  function renderRoles() {
    const container = byId('ccb-role-groups');
    if (!state?.roles?.length) {
      container.innerHTML = '<div class="text-muted small">No TDeck roles are configured.</div>';
      return;
    }
    container.innerHTML = ['Singers', 'Band', 'Production'].map((category) => {
      const roles = state.roles.filter((role) => role.display_category === category);
      return `<section class="mb-4"><h3 class="h6 text-uppercase text-muted">${category}</h3><div class="row g-3">${roles.map((role) => {
        const selected = allocationIds(role);
        const candidates = candidatePeople(role);
        const groups = (role.groups || []).map((group) => `<span class="badge text-bg-secondary">${escapeHtml(group.name)}</span>`).join(' ');
        return `<div class="col-12 col-lg-6"><div class="border rounded p-3 h-100">
          <div class="d-flex justify-content-between align-items-start gap-2 mb-2"><div class="fw-semibold">${escapeHtml(role.name)}</div><div>${groups || '<span class="badge text-bg-warning">No group mapping</span>'}</div></div>
          <div class="ccb-role-candidates" data-role-key="${escapeHtml(role.role_key)}">
            ${candidates.length ? candidates.map((person) => {
              const declined = String(person.status || '').toUpperCase() === 'DECLINED';
              return `<label class="form-check border rounded px-2 py-1 mb-1 ${declined ? 'text-muted' : ''}">
                <input class="form-check-input ms-0 me-2" type="checkbox" value="${person.id}" ${selected.has(String(person.id)) && !declined ? 'checked' : ''} ${declined ? 'disabled' : ''}>
                <span>${escapeHtml(person.name)}</span>
                <span class="badge ${statusBadge(person.status)} ms-1">${escapeHtml(person.status || '')}</span>
                ${person.linked_user ? `<span class="badge text-bg-success ms-1">${escapeHtml(person.linked_user.username)}</span>` : '<span class="badge text-bg-warning ms-1">Unlinked</span>'}
              </label>`;
            }).join('') : '<div class="small text-muted">No eligible people in the pulled roster.</div>'}
          </div>
        </div></div>`;
      }).join('')}</div></section>`;
    }).join('');
  }

  function statusBadge(value) {
    const status = String(value || '').toUpperCase();
    if (status === 'DECLINED') return 'text-bg-danger';
    if (status === 'ACCEPTED' || status === 'CHECKED_IN') return 'text-bg-success';
    if (status === 'PENDING') return 'text-bg-warning';
    return 'text-bg-secondary';
  }

  function renderPositions() {
    const container = byId('ccb-position-list');
    const positions = state?.positions || [];
    if (!positions.length) {
      container.innerHTML = '<div class="text-muted small">No roster has been pulled.</div>';
      return;
    }
    container.innerHTML = positions.map((position) => `<div class="col-12 col-lg-6"><div class="border rounded p-2 h-100">
      <div class="fw-semibold">${escapeHtml(position.position_name)}</div>
      <div class="small text-muted mb-1">${escapeHtml(position.team_name || '')}</div>
      ${(position.assignments || []).length ? position.assignments.map((assignment) => `<div class="d-flex justify-content-between gap-2 small"><span>${escapeHtml(assignment.name)}</span><span class="badge ${statusBadge(assignment.status)}">${escapeHtml(assignment.status)}</span></div>`).join('') : '<div class="small text-muted">Unassigned</div>'}
    </div></div>`).join('');
  }

  function renderUnmatched() {
    const container = byId('ccb-unmatched-list');
    const unmatched = state?.unmatched_people || [];
    const users = (state?.users || []).filter((user) => Number(user.is_active) && !Number(user.is_locked));
    if (!unmatched.length) {
      container.innerHTML = '<div class="alert alert-success py-2 mb-0">Every pulled CCB person is linked.</div>';
      return;
    }
    container.innerHTML = unmatched.map((person) => {
      const suggestedId = person.suggested_user?.id;
      return `<div class="border rounded p-2 mb-2" data-unmatched-id="${person.id}">
        <div class="fw-semibold">${escapeHtml(person.name)}</div>
        <div class="small text-muted mb-2">${escapeHtml(person.email || 'No email')} · CCB ${person.id}</div>
        <select class="form-select form-select-sm mb-2" data-link-user>
          <option value="">Choose TDeck user</option>
          ${users.map((user) => `<option value="${user.id}" ${Number(user.id) === Number(suggestedId) ? 'selected' : ''}>${escapeHtml(user.full_name || user.username)}${user.email ? ` — ${escapeHtml(user.email)}` : ''}</option>`).join('')}
        </select>
        <div class="d-flex justify-content-between align-items-center gap-2">
          <label class="form-check small"><input class="form-check-input" type="checkbox" data-link-vocalist ${(person.pools || []).includes('vocals') ? 'checked' : ''}> Vocalist</label>
          <button class="btn btn-sm btn-outline-primary" type="button" data-link-person>Link${suggestedId ? ' Suggested' : ''}</button>
        </div>
      </div>`;
    }).join('');
    container.querySelectorAll('[data-link-person]').forEach((button) => button.addEventListener('click', async () => {
      const card = button.closest('[data-unmatched-id]');
      const person = unmatched.find((item) => String(item.id) === String(card.dataset.unmatchedId));
      const userId = Number(card.querySelector('[data-link-user]').value || 0);
      if (!userId) return status('Choose a TDeck user to link.', 'warning');
      setBusy(true);
      try {
        await api('/api/ccb/identities/link', { method: 'POST', body: JSON.stringify({
          external_id: String(person.id), user_id: userId, display_name: person.name, email: person.email,
          is_vocalist: card.querySelector('[data-link-vocalist]').checked, raw: person.raw || {},
        }) });
        await loadState();
        status(`${person.name} is now linked.`, 'success');
      } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
    }));
  }

  function readAllocations() {
    const allocations = {};
    root.querySelectorAll('[data-role-key]').forEach((container) => {
      allocations[container.dataset.roleKey] = Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map((input) => Number(input.value));
    });
    return allocations;
  }

  async function saveAssignments(showMessage = true) {
    const eventId = selectedServiceId();
    if (!eventId) throw new Error('Select a service first.');
    const payload = await api(`/api/ccb/services/${eventId}/assignments`, { method: 'PUT', body: JSON.stringify({ allocations: readAllocations() }) });
    state = payload.state;
    renderState();
    if (showMessage) status('Reviewed assignments saved.', 'success');
  }

  async function pull(branches) {
    const eventId = selectedServiceId();
    if (!eventId) return status('Select a service first.', 'warning');
    setBusy(true);
    status('Pulling data from CCB...', 'info');
    try {
      const payload = await api(`/api/ccb/services/${eventId}/pull`, { method: 'POST', body: JSON.stringify({ branches, sources: sourceBody() }) }, true);
      state = payload.state;
      renderState();
      await loadRunsheet();
      const failed = Object.values(payload.workflow?.branches || {}).filter((branch) => !branch.ok);
      status(failed.length ? `Pull completed with warnings: ${failed.map((branch) => branch.error).join(' | ')}` : 'CCB data pulled successfully. No access was changed.', failed.length ? 'warning' : 'success');
    } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  }

  async function loadRunsheet() {
    const eventId = selectedServiceId();
    if (!eventId) return;
    try {
      const payload = await api(`/api/ccb/services/${eventId}/runsheet`);
      const items = payload.items || [];
      byId('ccb-runsheet-count').textContent = `${items.length} item${items.length === 1 ? '' : 's'}`;
      byId('ccb-runsheet-rows').innerHTML = items.length ? items.map((item) => `<tr class="${item.item_type === 'SECTION_HEADER' ? 'table-secondary fw-semibold' : ''}">
        <td>${escapeHtml(formatTime(item.starts_at))}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(formatDuration(item.duration_seconds))}</td><td>${escapeHtml(formatTime(item.ends_at))}</td>
      </tr>`).join('') : '<tr><td colspan="4" class="text-muted">No runsheet has been pulled.</td></tr>';
    } catch (error) {
      byId('ccb-runsheet-rows').innerHTML = `<tr><td colspan="4" class="text-danger">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  async function applyReviewed() {
    const eventId = selectedServiceId();
    if (!eventId) return status('Select a service first.', 'warning');
    setBusy(true);
    try {
      await saveAssignments(false);
      const result = await api(`/api/ccb/services/${eventId}/apply`, { method: 'POST' });
      await loadServices(false);
      status(`Applied ${result.membership_count} role/group assignment${result.membership_count === 1 ? '' : 's'} for ${result.user_count} user${result.user_count === 1 ? '' : 's'}.${result.warnings?.length ? ` ${result.warnings.join(' ')}` : ''}`, result.warnings?.length ? 'warning' : 'success');
    } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  }

  async function pullAndApply(allBranches) {
    const eventId = selectedServiceId();
    if (!eventId) return status('Select a service first.', 'warning');
    setBusy(true);
    status(allBranches ? 'Running full CCB service setup...' : 'Refreshing the roster and applying service access...', 'info');
    try {
      await saveAssignments(false);
      const result = await api(`/api/ccb/services/${eventId}/pull-and-apply`, { method: 'POST', body: JSON.stringify({ branches: allBranches ? ['roster', 'runsheet'] : ['roster'], sources: sourceBody() }) });
      await loadServices(false);
      const warnings = result.apply?.warnings || [];
      status(`${allBranches ? 'Service setup' : 'Roster pull and apply'} completed. ${result.apply?.membership_count || 0} group assignments are active.${warnings.length ? ` ${warnings.join(' ')}` : ''}`, warnings.length || result.pull?.workflow?.ok === false ? 'warning' : 'success');
    } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  }

  byId('ccb-service-select').addEventListener('change', async () => {
    renderSourceChoices();
    setBusy(true);
    try { await loadState(); status('', 'info'); } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  });
  byId('ccb-refresh-services').addEventListener('click', () => loadServices(true));
  root.querySelectorAll('[data-ccb-pull]').forEach((button) => button.addEventListener('click', () => {
    const branch = button.dataset.ccbPull;
    pull(branch === 'all' ? ['roster', 'runsheet'] : [branch]);
  }));
  byId('ccb-show-all-vocalists').addEventListener('change', renderRoles);
  byId('ccb-save-assignments').addEventListener('click', async () => {
    setBusy(true); try { await saveAssignments(true); } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  });
  byId('ccb-apply').addEventListener('click', applyReviewed);
  byId('ccb-pull-apply').addEventListener('click', () => pullAndApply(false));
  byId('ccb-run-setup').addEventListener('click', () => pullAndApply(true));
  byId('ccb-clear').addEventListener('click', async () => {
    if (!window.confirm('Clear all CCB-assigned service access and restore broad practice fallback groups? Manual groups will not be changed.')) return;
    setBusy(true);
    try {
      const result = await api('/api/ccb/access/clear', { method: 'POST' });
      await loadServices(false);
      status(`CCB service access cleared. ${result.cleared_memberships || 0} managed assignments were removed; manual groups are unchanged.`, 'success');
    } catch (error) { status(error.message, 'danger'); } finally { setBusy(false); }
  });

  loadServices(false);
})();
