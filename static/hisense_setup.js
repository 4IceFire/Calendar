(() => {
  const tvRows = document.getElementById('hisense-tvs')
  const profileRows = document.getElementById('hisense-profiles')
  const groupRows = document.getElementById('hisense-groups')
  const alertBox = document.getElementById('hisense-alert')
  let config = {}

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char])

  const notify = (message, kind = 'success') => {
    alertBox.innerHTML = `<div class="alert alert-${kind}" role="alert">${escapeHtml(message)}</div>`
  }

  const request = async (path, options = {}) => {
    const response = await fetch(path, {
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      ...options,
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `${response.status} ${response.statusText}`)
    }
    return payload
  }

  const move = (element, direction) => {
    if (direction < 0 && element.previousElementSibling) {
      element.parentElement.insertBefore(element, element.previousElementSibling)
    } else if (direction > 0 && element.nextElementSibling) {
      element.parentElement.insertBefore(element.nextElementSibling, element)
    }
  }

  const orderButtons = (prefix) => `
    <div class="btn-group btn-group-sm" role="group" aria-label="Reorder">
      <button type="button" class="btn btn-outline-secondary ${prefix}-up" title="Move up">↑</button>
      <button type="button" class="btn btn-outline-secondary ${prefix}-down" title="Move down">↓</button>
    </div>`

  const currentProfiles = () => [...profileRows.querySelectorAll('.hisense-profile')].map((row, index) => ({
    id: row.querySelector('.profile-id').value.trim() || `profile-${index + 1}`,
    name: row.querySelector('.profile-name').value.trim() || `Profile ${index + 1}`,
    cert_path: row.querySelector('.profile-cert').value.trim(),
    key_path: row.querySelector('.profile-key').value.trim(),
    compatible_models: row.querySelector('.profile-models').value.trim(),
    enabled: row.querySelector('.profile-enabled').checked,
  }))

  const currentTvs = () => [...tvRows.querySelectorAll('.hisense-tv')].map((row, index) => ({
    id: row.querySelector('.tv-id').value.trim() || `tv-${index + 1}`,
    name: row.querySelector('.tv-name').value.trim() || `TV ${index + 1}`,
    host: row.querySelector('.tv-host').value.trim(),
    mac: row.querySelector('.tv-mac').value.trim(),
    enabled: row.querySelector('.tv-enabled').checked,
    auth_mode: row.querySelector('.tv-auth-mode').value,
    certificate_profile: row.querySelector('.tv-profile').value,
  }))

  const profileOptions = (selected = 'auto') => {
    const profiles = currentProfiles()
    return [{ id: 'auto', name: 'Automatic (try enabled profiles in order)' }, ...profiles]
      .map((profile) => `<option value="${escapeHtml(profile.id)}" ${profile.id === selected ? 'selected' : ''}>${escapeHtml(profile.name)}</option>`)
      .join('')
  }

  const profileCard = (profile = {}, index = 0) => `
    <div class="card mb-3 hisense-profile" data-profile-id="${escapeHtml(profile.id || '')}">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-start mb-3">
          <div>
            <strong class="profile-title">${escapeHtml(profile.name || `Profile ${index + 1}`)}</strong>
            <div class="small text-muted">Automatic selection tries enabled profiles from top to bottom.</div>
          </div>
          <div class="d-flex gap-2">${orderButtons('profile')}<button type="button" class="btn btn-sm btn-outline-danger profile-remove">Remove</button></div>
        </div>
        <div class="row g-3">
          <div class="col-md-3"><label class="form-label">ID</label><input class="form-control profile-id" value="${escapeHtml(profile.id || '')}" placeholder="current-vidaa"></div>
          <div class="col-md-3"><label class="form-label">Name</label><input class="form-control profile-name" value="${escapeHtml(profile.name || '')}" placeholder="Current VIDAA certificate"></div>
          <div class="col-md-3"><label class="form-label">Client certificate path</label><input class="form-control profile-cert" value="${escapeHtml(profile.cert_path || '')}" placeholder="hisense_certs/current.pem"></div>
          <div class="col-md-3"><label class="form-label">Client key path</label><input class="form-control profile-key" value="${escapeHtml(profile.key_path || '')}" placeholder="hisense_certs/current.key"></div>
          <div class="col-md-9"><label class="form-label">Compatible models / firmware</label><input class="form-control profile-models" value="${escapeHtml(profile.compatible_models || '')}" placeholder="55A7G, 65A7G or VIDAA 9 / Q0109"></div>
          <div class="col-md-3"><div class="form-check form-switch mt-4"><input class="form-check-input profile-enabled" type="checkbox" ${profile.enabled !== false ? 'checked' : ''}><label class="form-check-label">Enabled for automatic selection</label></div></div>
        </div>
      </div>
    </div>`

  const tvCard = (tv = {}, status = {}, index = 0) => {
    const connected = Boolean(status.connected)
    const state = connected
      ? `Online · ${status.power || 'unknown'} · Vol ${status.volume ?? '—'} · ${status.source || 'no source'}`
      : `Offline${status.lastError ? ` · ${status.lastError}` : ''}`
    const detected = [
      status.model ? `Model ${status.model}` : '',
      status.protocolVersion ? `Protocol ${status.protocolVersion}` : 'Protocol not detected',
      status.authMethod ? `Auth ${status.authMethod}` : '',
      status.certificateProfile ? `Certificate ${status.certificateProfile}` : '',
    ].filter(Boolean).join(' · ')
    return `<div class="card mb-3 hisense-tv" data-tv-id="${escapeHtml(tv.id || '')}">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-start mb-3">
          <div>
            <strong class="tv-title">${escapeHtml(tv.name || `TV ${index + 1}`)}</strong>
            <div class="small ${connected ? 'text-success' : 'text-muted'} tv-status">${escapeHtml(state)}</div>
            <div class="small text-muted tv-detected">${escapeHtml(detected)}</div>
          </div>
          <div class="d-flex gap-2">${orderButtons('tv')}<button type="button" class="btn btn-sm btn-outline-danger tv-remove">Remove</button></div>
        </div>
        <div class="row g-3">
          <div class="col-md-3"><label class="form-label">ID</label><input class="form-control tv-id" value="${escapeHtml(tv.id || '')}" placeholder="auditorium"></div>
          <div class="col-md-3"><label class="form-label">Name</label><input class="form-control tv-name" value="${escapeHtml(tv.name || '')}" placeholder="Auditorium TV"></div>
          <div class="col-md-3"><label class="form-label">IP address</label><input class="form-control tv-host" value="${escapeHtml(tv.host || '')}" placeholder="10.5.10.140"></div>
          <div class="col-md-3"><label class="form-label">MAC address</label><input class="form-control tv-mac" value="${escapeHtml(tv.mac || '')}" placeholder="a0:62:fb:84:ed:28"></div>
          <div class="col-md-4">
            <label class="form-label">Protocol / authentication</label>
            <select class="form-select tv-auth-mode">
              <option value="auto" ${(tv.auth_mode || 'auto') === 'auto' ? 'selected' : ''}>Automatic (recommended)</option>
              <option value="static-legacy" ${tv.auth_mode === 'static-legacy' ? 'selected' : ''}>Static legacy (A7G fallback)</option>
              <option value="dynamic-legacy" ${tv.auth_mode === 'dynamic-legacy' ? 'selected' : ''}>Dynamic legacy (&lt;3000)</option>
              <option value="dynamic-middle" ${tv.auth_mode === 'dynamic-middle' ? 'selected' : ''}>Dynamic middle (3000–3289)</option>
              <option value="dynamic-modern" ${tv.auth_mode === 'dynamic-modern' ? 'selected' : ''}>Dynamic modern (3290+)</option>
            </select>
          </div>
          <div class="col-md-5"><label class="form-label">Certificate compatibility profile</label><select class="form-select tv-profile" data-selected="${escapeHtml(tv.certificate_profile || 'auto')}">${profileOptions(tv.certificate_profile || 'auto')}</select></div>
          <div class="col-md-3"><div class="form-check form-switch mt-4"><input class="form-check-input tv-enabled" type="checkbox" ${tv.enabled !== false ? 'checked' : ''}><label class="form-check-label">Enabled</label></div></div>
        </div>
        <div class="d-flex flex-wrap gap-2 mt-3">
          <button type="button" class="btn btn-sm btn-outline-secondary tv-reconnect">Reconnect / redetect</button>
          <button type="button" class="btn btn-sm btn-outline-warning tv-pair">Request pairing PIN</button>
          <div class="input-group input-group-sm" style="max-width: 220px"><input class="form-control tv-pin" maxlength="4" inputmode="numeric" placeholder="4-digit PIN"><button class="btn btn-outline-warning tv-submit-pin" type="button">Submit PIN</button></div>
          <button type="button" class="btn btn-sm btn-outline-primary tv-test">Test volume +</button>
        </div>
      </div>
    </div>`
  }

  const tvOptions = (selected = '') => currentTvs()
    .map((tv) => `<option value="${escapeHtml(tv.id)}" ${tv.id === selected ? 'selected' : ''}>${escapeHtml(tv.name)} (${escapeHtml(tv.id)})</option>`)
    .join('')

  const memberRow = (tvId) => {
    const tv = currentTvs().find((item) => item.id === tvId)
    return `<div class="list-group-item d-flex justify-content-between align-items-center group-member" data-tv-id="${escapeHtml(tvId)}">
      <span class="group-member-label">${escapeHtml(tv ? `${tv.name} (${tv.id})` : tvId)}</span>
      <div class="d-flex gap-2">${orderButtons('member')}<button type="button" class="btn btn-sm btn-outline-danger member-remove">Remove</button></div>
    </div>`
  }

  const groupCard = (group = {}, index = 0) => `
    <div class="card mb-3 hisense-group">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-start mb-3">
          <strong class="group-title">${escapeHtml(group.name || `Group ${index + 1}`)}</strong>
          <div class="d-flex gap-2">${orderButtons('group')}<button type="button" class="btn btn-sm btn-outline-danger group-remove">Remove</button></div>
        </div>
        <div class="row g-3">
          <div class="col-md-4"><label class="form-label">ID</label><input class="form-control group-id" value="${escapeHtml(group.id || '')}" placeholder="foyer"></div>
          <div class="col-md-5"><label class="form-label">Name</label><input class="form-control group-name" value="${escapeHtml(group.name || '')}" placeholder="Foyer TVs"></div>
          <div class="col-md-3"><div class="form-check form-switch mt-4"><input class="form-check-input group-enabled" type="checkbox" ${group.enabled !== false ? 'checked' : ''}><label class="form-check-label">Enabled in Companion</label></div></div>
        </div>
        <label class="form-label mt-3">TVs in control order</label>
        <div class="list-group group-members mb-3">${(group.tv_ids || []).map(memberRow).join('')}</div>
        <div class="input-group input-group-sm" style="max-width: 520px">
          <select class="form-select group-add-tv">${tvOptions()}</select>
          <button type="button" class="btn btn-outline-primary member-add">Add TV to group</button>
        </div>
      </div>
    </div>`

  const currentGroups = () => [...groupRows.querySelectorAll('.hisense-group')].map((row, index) => ({
    id: row.querySelector('.group-id').value.trim() || `group-${index + 1}`,
    name: row.querySelector('.group-name').value.trim() || `Group ${index + 1}`,
    enabled: row.querySelector('.group-enabled').checked,
    tv_ids: [...row.querySelectorAll('.group-member')].map((member) => member.dataset.tvId),
  }))

  const refreshProfileChoices = (renamedFrom = '', renamedTo = '') => {
    for (const select of tvRows.querySelectorAll('.tv-profile')) {
      let selected = select.value || select.dataset.selected || 'auto'
      if (renamedFrom && selected === renamedFrom) selected = renamedTo || 'auto'
      select.innerHTML = profileOptions(selected)
      if (![...select.options].some((option) => option.value === selected)) select.value = 'auto'
    }
  }

  const refreshGroupTvChoices = () => {
    const tvs = currentTvs()
    for (const group of groupRows.querySelectorAll('.hisense-group')) {
      const select = group.querySelector('.group-add-tv')
      const selected = select.value
      select.innerHTML = tvOptions(selected)
      for (const member of group.querySelectorAll('.group-member')) {
        const tv = tvs.find((item) => item.id === member.dataset.tvId)
        member.querySelector('.group-member-label').textContent = tv ? `${tv.name} (${tv.id})` : member.dataset.tvId
      }
    }
  }

  const load = async () => {
    try {
      const [settings, state] = await Promise.all([request('/api/hisense/config'), request('/api/tvs')])
      config = settings.config || {}
      document.getElementById('hisense-enabled').checked = Boolean(config.hisense_enabled)
      document.getElementById('hisense-poll').value = config.hisense_poll_interval || 10
      document.getElementById('hisense-reconnect').value = config.hisense_reconnect_interval || 15
      document.getElementById('hisense-compatible-models').value = config.hisense_compatible_models || ''
      profileRows.innerHTML = (config.hisense_certificate_profiles || []).map(profileCard).join('')
      if (!profileRows.children.length) profileRows.insertAdjacentHTML('beforeend', profileCard({}, 0))
      const statuses = Object.fromEntries((state.tvs || []).map((tv) => [tv.id, tv]))
      tvRows.innerHTML = (config.hisense_tvs || []).map((tv, index) => tvCard(tv, statuses[tv.id] || {}, index)).join('')
      if (!tvRows.children.length) tvRows.insertAdjacentHTML('beforeend', tvCard({}, {}, 0))
      groupRows.innerHTML = (config.hisense_tv_groups || []).map(groupCard).join('')
      refreshProfileChoices()
      refreshGroupTvChoices()
    } catch (error) {
      notify(error.message, 'danger')
    }
  }

  document.getElementById('hisense-add-profile').addEventListener('click', () => {
    profileRows.insertAdjacentHTML('beforeend', profileCard({}, profileRows.children.length))
    refreshProfileChoices()
  })
  document.getElementById('hisense-add').addEventListener('click', () => {
    tvRows.insertAdjacentHTML('beforeend', tvCard({}, {}, tvRows.children.length))
    refreshGroupTvChoices()
  })
  document.getElementById('hisense-add-group').addEventListener('click', () => {
    groupRows.insertAdjacentHTML('beforeend', groupCard({}, groupRows.children.length))
  })
  document.getElementById('hisense-refresh').addEventListener('click', load)

  document.getElementById('hisense-save').addEventListener('click', async () => {
    try {
      const payload = {
        hisense_enabled: document.getElementById('hisense-enabled').checked,
        hisense_poll_interval: Number(document.getElementById('hisense-poll').value || 10),
        hisense_reconnect_interval: Number(document.getElementById('hisense-reconnect').value || 15),
        hisense_compatible_models: document.getElementById('hisense-compatible-models').value.trim(),
        hisense_certificate_profiles: currentProfiles(),
        hisense_tvs: currentTvs(),
        hisense_tv_groups: currentGroups(),
      }
      await request('/api/hisense/config', { method: 'PUT', body: JSON.stringify(payload) })
      notify('TV configuration saved. Protocol detection and TV connections have restarted.')
      await load()
    } catch (error) {
      notify(error.message, 'danger')
    }
  })

  profileRows.addEventListener('input', (event) => {
    const row = event.target.closest('.hisense-profile')
    if (row && event.target.classList.contains('profile-name')) row.querySelector('.profile-title').textContent = event.target.value || 'Certificate profile'
    if (row && event.target.classList.contains('profile-id')) {
      const previousId = row.dataset.profileId
      const nextId = event.target.value.trim()
      refreshProfileChoices(previousId, nextId)
      if (nextId) row.dataset.profileId = nextId
    } else refreshProfileChoices()
  })
  profileRows.addEventListener('click', (event) => {
    const button = event.target.closest('button')
    const row = event.target.closest('.hisense-profile')
    if (!button || !row) return
    if (button.classList.contains('profile-remove')) {
      if (profileRows.children.length <= 1) return notify('At least one certificate profile is required.', 'warning')
      row.remove()
      refreshProfileChoices()
    } else if (button.classList.contains('profile-up')) move(row, -1)
    else if (button.classList.contains('profile-down')) move(row, 1)
  })

  tvRows.addEventListener('input', (event) => {
    const row = event.target.closest('.hisense-tv')
    if (row && event.target.classList.contains('tv-name')) row.querySelector('.tv-title').textContent = event.target.value || 'TV'
    if (row && event.target.classList.contains('tv-id')) {
      const previousId = row.dataset.tvId
      const nextId = event.target.value.trim()
      if (previousId && nextId && previousId !== nextId) {
        for (const member of groupRows.querySelectorAll('.group-member')) {
          if (member.dataset.tvId === previousId) member.dataset.tvId = nextId
        }
      }
      if (nextId) row.dataset.tvId = nextId
    }
    refreshGroupTvChoices()
  })
  tvRows.addEventListener('click', async (event) => {
    const button = event.target.closest('button')
    const row = event.target.closest('.hisense-tv')
    if (!button || !row) return
    if (button.classList.contains('tv-remove')) {
      const id = row.querySelector('.tv-id').value.trim()
      row.remove()
      for (const member of groupRows.querySelectorAll(`.group-member[data-tv-id="${CSS.escape(id)}"]`)) member.remove()
      refreshGroupTvChoices()
      return
    }
    if (button.classList.contains('tv-up')) return move(row, -1)
    if (button.classList.contains('tv-down')) return move(row, 1)
    const id = row.querySelector('.tv-id').value.trim()
    if (!id) return notify('Save the TV with an ID before testing or pairing.', 'warning')
    try {
      if (button.classList.contains('tv-reconnect')) {
        await request(`/api/tvs/${encodeURIComponent(id)}/reconnect`, { method: 'POST', body: '{}' })
      } else if (button.classList.contains('tv-pair')) {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/request`, { method: 'POST', body: '{}' })
        notify('Pairing request sent. If the TV reports that the app is no longer compatible, select a newer certificate profile, save, reconnect, and retry.', 'warning')
        return
      } else if (button.classList.contains('tv-submit-pin')) {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/submit`, {
          method: 'POST',
          body: JSON.stringify({ pin: row.querySelector('.tv-pin').value.trim() }),
        })
      } else if (button.classList.contains('tv-test')) {
        await request(`/api/tvs/${encodeURIComponent(id)}/volume`, { method: 'POST', body: JSON.stringify({ action: 'up' }) })
      } else return
      notify('TV command accepted.')
      await load()
    } catch (error) {
      notify(error.message, 'danger')
    }
  })

  groupRows.addEventListener('input', (event) => {
    const row = event.target.closest('.hisense-group')
    if (row && event.target.classList.contains('group-name')) row.querySelector('.group-title').textContent = event.target.value || 'TV group'
  })
  groupRows.addEventListener('click', (event) => {
    const button = event.target.closest('button')
    const group = event.target.closest('.hisense-group')
    if (!button || !group) return
    if (button.classList.contains('group-remove')) group.remove()
    else if (button.classList.contains('group-up')) move(group, -1)
    else if (button.classList.contains('group-down')) move(group, 1)
    else if (button.classList.contains('member-add')) {
      const select = group.querySelector('.group-add-tv')
      const tvId = select.value
      if (!tvId) return notify('Add and name a TV before adding it to a group.', 'warning')
      if ([...group.querySelectorAll('.group-member')].some((member) => member.dataset.tvId === tvId)) {
        return notify('That TV is already in this group.', 'warning')
      }
      group.querySelector('.group-members').insertAdjacentHTML('beforeend', memberRow(tvId))
    } else {
      const member = button.closest('.group-member')
      if (!member) return
      if (button.classList.contains('member-remove')) member.remove()
      else if (button.classList.contains('member-up')) move(member, -1)
      else if (button.classList.contains('member-down')) move(member, 1)
    }
  })

  load()
})()
