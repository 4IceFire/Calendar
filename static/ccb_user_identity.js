(function () {
  const card = document.getElementById('ccb-user-identity-card');
  if (!card) return;
  const userId = Number(card.dataset.userId || 0);
  const statusEl = document.getElementById('ccb-user-identity-status');

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }
  function status(message, kind = 'info') {
    statusEl.innerHTML = message ? `<div class="alert alert-${kind} py-2 mb-0">${escapeHtml(message)}</div>` : '';
  }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }
  async function link(person, vocalist) {
    await api('/api/ccb/identities/link', { method: 'POST', body: JSON.stringify({
      external_id: String(person.id), user_id: userId, display_name: person.name || '', email: person.email || '', is_vocalist: Boolean(vocalist), raw: person,
    }) });
    window.location.reload();
  }

  const searchButton = document.getElementById('ccb-user-search-button');
  if (searchButton) searchButton.addEventListener('click', async () => {
    const query = document.getElementById('ccb-user-search').value.trim();
    if (query.length < 2) return status('Enter at least two characters.', 'warning');
    searchButton.disabled = true;
    try {
      const payload = await api(`/api/ccb/individuals?query=${encodeURIComponent(query)}`);
      const results = document.getElementById('ccb-user-search-results');
      const people = payload.individuals || [];
      results.innerHTML = people.length ? people.map((person, index) => `<button type="button" class="list-group-item list-group-item-action" data-result-index="${index}">
        <div class="fw-semibold">${escapeHtml(person.name || `CCB ${person.id}`)}</div><div class="small text-muted">${escapeHtml(person.email || 'No email')} · CCB ${escapeHtml(person.id)}</div>
      </button>`).join('') : '<div class="text-muted small">No CCB individuals matched.</div>';
      results.querySelectorAll('[data-result-index]').forEach((button) => button.addEventListener('click', async () => {
        const person = people[Number(button.dataset.resultIndex)];
        if (!window.confirm(`Link ${person.name || person.id} to this TDeck user?`)) return;
        try { await link(person, false); } catch (error) { status(error.message, 'danger'); }
      }));
    } catch (error) { status(error.message, 'danger'); } finally { searchButton.disabled = false; }
  });

  const saveButton = document.getElementById('ccb-user-save-identity');
  if (saveButton) saveButton.addEventListener('click', async () => {
    saveButton.disabled = true;
    try {
      await link({ id: card.dataset.externalId, name: saveButton.dataset.name || '', email: saveButton.dataset.email || '' }, document.getElementById('ccb-user-vocalist').checked);
    } catch (error) { status(error.message, 'danger'); saveButton.disabled = false; }
  });

  const unlinkButton = document.getElementById('ccb-user-unlink');
  if (unlinkButton) unlinkButton.addEventListener('click', async () => {
    if (!window.confirm('Unlink this CCB identity? Manual TDeck groups will not be changed.')) return;
    unlinkButton.disabled = true;
    try {
      await api(`/api/ccb/identities/${encodeURIComponent(card.dataset.externalId)}`, { method: 'DELETE' });
      window.location.reload();
    } catch (error) { status(error.message, 'danger'); unlinkButton.disabled = false; }
  });
})();
