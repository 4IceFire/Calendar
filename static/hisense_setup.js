(() => {
  const rows = document.getElementById('hisense-tvs')
  const alertBox = document.getElementById('hisense-alert')
  let config = {}

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char])

  const notify = (message, kind = 'success') => {
    alertBox.innerHTML = `<div class="alert alert-${kind}" role="alert">${escapeHtml(message)}</div>`
  }

  const request = async (path, options = {}) => {
    const response = await fetch(path, { headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, ...options })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `${response.status} ${response.statusText}`)
    return payload
  }

  const tvCard = (tv = {}, status = {}) => {
    const connected = Boolean(status.connected)
    const state = connected ? `Online · ${status.power || 'unknown'} · Vol ${status.volume ?? '—'} · ${status.source || 'no source'}` : `Offline${status.lastError ? ` · ${status.lastError}` : ''}`
    return `<div class="card mb-3 hisense-tv" data-tv-id="${escapeHtml(tv.id || '')}">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-start mb-3">
          <div><strong class="tv-title">${escapeHtml(tv.name || 'New TV')}</strong><div class="small ${connected ? 'text-success' : 'text-muted'} tv-status">${escapeHtml(state)}</div></div>
          <button type="button" class="btn btn-sm btn-outline-danger tv-remove">Remove</button>
        </div>
        <div class="row g-3">
          <div class="col-md-3"><label class="form-label">ID</label><input class="form-control tv-id" value="${escapeHtml(tv.id || '')}" placeholder="auditorium"></div>
          <div class="col-md-3"><label class="form-label">Name</label><input class="form-control tv-name" value="${escapeHtml(tv.name || '')}" placeholder="Auditorium TV"></div>
          <div class="col-md-3"><label class="form-label">IP address</label><input class="form-control tv-host" value="${escapeHtml(tv.host || '')}" placeholder="10.5.10.140"></div>
          <div class="col-md-3"><label class="form-label">MAC address</label><input class="form-control tv-mac" value="${escapeHtml(tv.mac || '')}" placeholder="a0:62:fb:84:ed:28"></div>
          <div class="col-md-3"><div class="form-check form-switch mt-4"><input class="form-check-input tv-enabled" type="checkbox" ${tv.enabled !== false ? 'checked' : ''}><label class="form-check-label">Enabled</label></div></div>
        </div>
        <div class="d-flex flex-wrap gap-2 mt-3">
          <button type="button" class="btn btn-sm btn-outline-secondary tv-reconnect">Reconnect</button>
          <button type="button" class="btn btn-sm btn-outline-warning tv-pair">Request pairing PIN</button>
          <div class="input-group input-group-sm" style="max-width: 220px"><input class="form-control tv-pin" maxlength="4" inputmode="numeric" placeholder="4-digit PIN"><button class="btn btn-outline-warning tv-submit-pin" type="button">Submit PIN</button></div>
          <button type="button" class="btn btn-sm btn-outline-primary tv-test">Test volume +</button>
        </div>
      </div>
    </div>`
  }

  const currentTvs = () => [...rows.querySelectorAll('.hisense-tv')].map((row, index) => ({
    id: row.querySelector('.tv-id').value.trim() || `tv-${index + 1}`,
    name: row.querySelector('.tv-name').value.trim() || `TV ${index + 1}`,
    host: row.querySelector('.tv-host').value.trim(),
    mac: row.querySelector('.tv-mac').value.trim(),
    enabled: row.querySelector('.tv-enabled').checked,
  }))

  const load = async () => {
    try {
      const [settings, state] = await Promise.all([request('/api/hisense/config'), request('/api/tvs')])
      config = settings.config || {}
      document.getElementById('hisense-enabled').checked = Boolean(config.hisense_enabled)
      document.getElementById('hisense-cert').value = config.hisense_cert_path || 'hisense_certs/vidaa_client.pem'
      document.getElementById('hisense-key').value = config.hisense_key_path || 'hisense_certs/vidaa_client.key'
      document.getElementById('hisense-poll').value = config.hisense_poll_interval || 10
      document.getElementById('hisense-reconnect').value = config.hisense_reconnect_interval || 15
      const statuses = Object.fromEntries((state.tvs || []).map((tv) => [tv.id, tv]))
      rows.innerHTML = (config.hisense_tvs || []).map((tv) => tvCard(tv, statuses[tv.id] || {})).join('')
      if (!rows.children.length) rows.insertAdjacentHTML('beforeend', tvCard())
    } catch (error) { notify(error.message, 'danger') }
  }

  document.getElementById('hisense-add').addEventListener('click', () => rows.insertAdjacentHTML('beforeend', tvCard()))
  document.getElementById('hisense-refresh').addEventListener('click', load)
  document.getElementById('hisense-save').addEventListener('click', async () => {
    try {
      const payload = {
        hisense_enabled: document.getElementById('hisense-enabled').checked,
        hisense_cert_path: document.getElementById('hisense-cert').value.trim(),
        hisense_key_path: document.getElementById('hisense-key').value.trim(),
        hisense_poll_interval: Number(document.getElementById('hisense-poll').value || 10),
        hisense_reconnect_interval: Number(document.getElementById('hisense-reconnect').value || 15),
        hisense_tvs: currentTvs(),
      }
      await request('/api/hisense/config', { method: 'PUT', body: JSON.stringify(payload) })
      notify('TV configuration saved. The native service has restarted.')
      await load()
    } catch (error) { notify(error.message, 'danger') }
  })

  rows.addEventListener('click', async (event) => {
    const button = event.target.closest('button')
    const row = event.target.closest('.hisense-tv')
    if (!button || !row) return
    if (button.classList.contains('tv-remove')) { row.remove(); return }
    const id = row.querySelector('.tv-id').value.trim()
    if (!id) { notify('Save the TV with an ID before testing or pairing.', 'warning'); return }
    try {
      if (button.classList.contains('tv-reconnect')) await request(`/api/tvs/${encodeURIComponent(id)}/reconnect`, { method: 'POST', body: '{}' })
      else if (button.classList.contains('tv-pair')) await request(`/api/tvs/${encodeURIComponent(id)}/pair/request`, { method: 'POST', body: '{}' })
      else if (button.classList.contains('tv-submit-pin')) await request(`/api/tvs/${encodeURIComponent(id)}/pair/submit`, { method: 'POST', body: JSON.stringify({ pin: row.querySelector('.tv-pin').value.trim() }) })
      else if (button.classList.contains('tv-test')) await request(`/api/tvs/${encodeURIComponent(id)}/volume`, { method: 'POST', body: JSON.stringify({ action: 'up' }) })
      else return
      notify('TV command accepted.')
      setTimeout(load, 700)
    } catch (error) { notify(error.message, 'danger') }
  })

  load()
})()
