(() => {
  const tree = document.getElementById('hisense-tree')
  const profileRows = document.getElementById('hisense-profiles')
  const alertBox = document.getElementById('hisense-alert')
  let config = {}

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char])

  const slugify = (value, fallback = '') => {
    const slug = String(value || '').trim().toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '')
    return slug || fallback
  }

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

  const collapseKey = (kind, id) => `tdeck:hisense:${kind}:${String(id || 'new')}:open`
  const storedOpen = (kind, id, defaultValue) => {
    try {
      const value = localStorage.getItem(collapseKey(kind, id))
      return value === null ? defaultValue : value === '1'
    } catch (_error) {
      return defaultValue
    }
  }
  const storeOpen = (kind, id, open) => {
    try { localStorage.setItem(collapseKey(kind, id), open ? '1' : '0') } catch (_error) {}
  }

  const orderButtons = (kind) => `
    <div class="btn-group btn-group-sm" role="group" aria-label="Reorder">
      <button type="button" class="btn btn-outline-secondary" data-action="${kind}-up" title="Move up">&uarr;</button>
      <button type="button" class="btn btn-outline-secondary" data-action="${kind}-down" title="Move down">&darr;</button>
    </div>`

  const currentProfiles = () => [...profileRows.querySelectorAll('.hisense-profile')].map((row, index) => ({
    id: row.querySelector('.profile-id').value.trim() || `profile-${index + 1}`,
    name: row.querySelector('.profile-name').value.trim() || `Profile ${index + 1}`,
    cert_path: row.querySelector('.profile-cert').value.trim(),
    key_path: row.querySelector('.profile-key').value.trim(),
    compatible_models: row.querySelector('.profile-models').value.trim(),
    enabled: row.querySelector('.profile-enabled').checked,
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
          <div class="d-flex gap-2">${orderButtons('profile')}<button type="button" class="btn btn-sm btn-outline-danger" data-action="profile-remove">Remove</button></div>
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

  const groupRecords = () => [...tree.querySelectorAll(':scope > .hisense-group')].map((row) => ({
    id: row.querySelector('.group-id').value.trim(),
    name: row.querySelector('.group-name').value.trim(),
  }))

  const groupOptions = (selected = '') => [
    `<option value="" ${selected ? '' : 'selected'}>Ungrouped</option>`,
    ...groupRecords().map((group) =>
      `<option value="${escapeHtml(group.id)}" ${group.id === selected ? 'selected' : ''}>${escapeHtml(group.name)}</option>`),
  ].join('')

  const uniqueId = (kind, name, currentRow = null) => {
    const selector = kind === 'group' ? '.group-id' : '.tv-id'
    const fallback = kind === 'group' ? 'group' : 'tv'
    const used = new Set(
      [...tree.querySelectorAll(selector)]
        .filter((input) => !currentRow || !currentRow.contains(input))
        .map((input) => input.value.trim())
        .filter(Boolean),
    )
    const base = slugify(name, fallback)
    let candidate = base
    let suffix = 2
    while (used.has(candidate)) candidate = `${base}-${suffix++}`
    return candidate
  }

  const tvStatusText = (status = {}) => {
    if (!status.connected) return `Offline${status.lastError ? ` · ${status.lastError}` : ''}`
    return `Online · ${status.power || 'unknown'} · Vol ${status.volume ?? '—'} · ${status.source || 'no source'}`
  }

  const tvDetectedText = (status = {}) => [
    status.model ? `Model ${status.model}` : '',
    status.protocolVersion ? `Protocol ${status.protocolVersion}` : 'Protocol not detected',
    status.authMethod ? `Auth ${status.authMethod}` : '',
    status.certificateProfile ? `Certificate ${status.certificateProfile}` : '',
  ].filter(Boolean).join(' · ')

  const tvCard = (tv = {}, status = {}, groupId = '', isNew = false) => {
    const id = tv.id || uniqueId('tv', tv.name || 'tv')
    const open = isNew ? true : storedOpen('tv', id, false)
    const connected = Boolean(status.connected)
    return `<div class="list-group-item hisense-tv" data-tv-id="${escapeHtml(id)}" data-id-auto="${isNew ? 'true' : 'false'}" data-saved="${isNew ? 'false' : 'true'}" style="padding-left:${groupId ? '44px' : '24px'}">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary tv-toggle" data-action="tv-toggle" title="Expand/collapse TV" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 text-truncate">
          <div class="fw-semibold text-truncate tv-title">${escapeHtml(tv.name || 'New TV')}</div>
          <div class="small ${connected ? 'text-success' : 'text-muted'} text-truncate tv-status">${escapeHtml(tvStatusText(status))}</div>
        </div>
        <select class="form-select form-select-sm tv-group-select" aria-label="TV group" style="max-width:210px">${groupOptions(groupId)}</select>
        ${orderButtons('tv')}
        <button type="button" class="btn btn-sm btn-outline-danger" data-action="tv-remove">Remove</button>
      </div>
      <div class="tv-details mt-3" ${open ? '' : 'style="display:none"'}>
        <div class="small text-muted mb-3 tv-detected">${escapeHtml(tvDetectedText(status))}</div>
        <div class="row g-3">
          <div class="col-md-4">
            <label class="form-label">TV name</label>
            <input class="form-control tv-name" value="${escapeHtml(tv.name || '')}" placeholder="Foyer TV">
            <div class="form-text">This is the name shown in TDeck and Companion.</div>
          </div>
          <div class="col-md-4">
            <label class="form-label">IP address</label>
            <input class="form-control tv-host" value="${escapeHtml(tv.host || '')}" placeholder="10.5.10.175">
          </div>
          <div class="col-md-4">
            <label class="form-label">MAC address</label>
            <input class="form-control tv-mac" value="${escapeHtml(tv.mac || '')}" placeholder="e4:8a:93:f1:da:22">
            <div class="form-text">Required for Wake-on-LAN power-on and newer VIDAA authentication.</div>
          </div>
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
          <div class="col-md-5">
            <label class="form-label">Certificate compatibility profile</label>
            <select class="form-select tv-profile" data-selected="${escapeHtml(tv.certificate_profile || 'auto')}">${profileOptions(tv.certificate_profile || 'auto')}</select>
          </div>
          <div class="col-md-3">
            <div class="form-check form-switch mt-4">
              <input class="form-check-input tv-enabled" type="checkbox" ${tv.enabled !== false ? 'checked' : ''}>
              <label class="form-check-label">Enabled</label>
            </div>
          </div>
        </div>
        <details class="mt-3">
          <summary class="small text-muted">Advanced identity</summary>
          <div class="row g-3 mt-1">
            <div class="col-md-6">
              <label class="form-label">Internal ID</label>
              <input class="form-control tv-id" value="${escapeHtml(id)}" placeholder="foyer-tv">
              <div class="form-text">Generated from the name for new TVs. Keep it unchanged after creating Companion buttons so those buttons retain their target.</div>
            </div>
          </div>
        </details>
        <div class="d-flex flex-wrap gap-2 mt-3">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-action="tv-reconnect">Reconnect / redetect</button>
          <button type="button" class="btn btn-sm btn-outline-warning" data-action="tv-pair">Request pairing PIN</button>
          <div class="input-group input-group-sm" style="max-width:220px">
            <input class="form-control tv-pin" maxlength="4" inputmode="numeric" placeholder="4-digit PIN">
            <button class="btn btn-outline-warning" type="button" data-action="tv-submit-pin">Submit PIN</button>
          </div>
          <button type="button" class="btn btn-sm btn-outline-primary" data-action="tv-test">Test volume +</button>
        </div>
      </div>
    </div>`
  }

  const groupCard = (group = {}, tvs = [], statuses = {}) => {
    const id = group.id || uniqueId('group', group.name || 'group')
    const open = storedOpen('group', id, true)
    return `<div class="hisense-group" data-group-id="${escapeHtml(id)}">
      <input type="hidden" class="group-id" value="${escapeHtml(id)}">
      <input type="hidden" class="group-name" value="${escapeHtml(group.name || id)}">
      <div class="list-group-item d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary group-toggle" data-action="group-toggle" title="Expand/collapse group" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 text-truncate">
          <div class="fw-semibold text-truncate group-title">${escapeHtml(group.name || id)}</div>
          <div class="small text-muted group-count">${tvs.length} TV${tvs.length === 1 ? '' : 's'}</div>
        </div>
        <div class="form-check form-switch mb-0">
          <input class="form-check-input group-enabled" type="checkbox" ${group.enabled !== false ? 'checked' : ''} aria-label="Enable group in Companion" title="Show group in Companion">
          <label class="form-check-label small">Companion</label>
        </div>
        ${orderButtons('group')}
        <button type="button" class="btn btn-sm btn-outline-secondary" data-action="group-rename">Rename</button>
        <button type="button" class="btn btn-sm btn-outline-danger" data-action="group-remove">Delete</button>
      </div>
      <div class="tree-tv-list list-group list-group-flush" data-group-id="${escapeHtml(id)}" ${open ? '' : 'style="display:none"'}>
        ${tvs.map((tv) => tvCard(tv, statuses[tv.id] || {}, id)).join('')}
      </div>
    </div>`
  }

  const rootCard = (tvs = [], statuses = {}) => {
    const open = storedOpen('root', 'ungrouped', true)
    return `<div class="hisense-root">
      <div class="list-group-item d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary root-toggle" data-action="root-toggle" title="Expand/collapse ungrouped TVs" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1">
          <div class="fw-semibold">Ungrouped TVs</div>
          <div class="small text-muted root-count">${tvs.length} TV${tvs.length === 1 ? '' : 's'}</div>
        </div>
      </div>
      <div class="tree-tv-list list-group list-group-flush" data-group-id="" ${open ? '' : 'style="display:none"'}>
        ${tvs.map((tv) => tvCard(tv, statuses[tv.id] || {}, '')).join('')}
      </div>
    </div>`
  }

  const directTvRows = (list) => [...list.children].filter((child) => child.classList.contains('hisense-tv'))

  const currentTvs = () => [...tree.querySelectorAll('.hisense-tv')].map((row, index) => ({
    id: row.querySelector('.tv-id').value.trim() || uniqueId('tv', row.querySelector('.tv-name').value || `tv-${index + 1}`, row),
    name: row.querySelector('.tv-name').value.trim() || `TV ${index + 1}`,
    host: row.querySelector('.tv-host').value.trim(),
    mac: row.querySelector('.tv-mac').value.trim(),
    enabled: row.querySelector('.tv-enabled').checked,
    auth_mode: row.querySelector('.tv-auth-mode').value,
    certificate_profile: row.querySelector('.tv-profile').value,
  }))

  const currentGroups = () => [...tree.querySelectorAll(':scope > .hisense-group')].map((row, index) => {
    const list = row.querySelector('.tree-tv-list')
    return {
      id: row.querySelector('.group-id').value.trim() || `group-${index + 1}`,
      name: row.querySelector('.group-name').value.trim() || `Group ${index + 1}`,
      enabled: row.querySelector('.group-enabled').checked,
      tv_ids: directTvRows(list).map((tvRow) => tvRow.querySelector('.tv-id').value.trim()),
    }
  })

  const parentGroupId = (tvRow) => tvRow.closest('.hisense-group')?.querySelector('.group-id')?.value.trim() || ''

  const refreshCounts = () => {
    const rootList = tree.querySelector('.hisense-root > .tree-tv-list')
    const rootCount = rootList ? directTvRows(rootList).length : 0
    const rootLabel = tree.querySelector('.root-count')
    if (rootLabel) rootLabel.textContent = `${rootCount} TV${rootCount === 1 ? '' : 's'}`
    for (const group of tree.querySelectorAll(':scope > .hisense-group')) {
      const count = directTvRows(group.querySelector('.tree-tv-list')).length
      group.querySelector('.group-count').textContent = `${count} TV${count === 1 ? '' : 's'}`
    }
  }

  const refreshGroupSelects = () => {
    for (const tvRow of tree.querySelectorAll('.hisense-tv')) {
      const selected = parentGroupId(tvRow)
      const select = tvRow.querySelector('.tv-group-select')
      select.innerHTML = groupOptions(selected)
      select.value = selected
    }
    refreshCounts()
  }

  const refreshProfileChoices = (renamedFrom = '', renamedTo = '') => {
    for (const select of tree.querySelectorAll('.tv-profile')) {
      let selected = select.value || select.dataset.selected || 'auto'
      if (renamedFrom && selected === renamedFrom) selected = renamedTo || 'auto'
      select.innerHTML = profileOptions(selected)
      if (![...select.options].some((option) => option.value === selected)) select.value = 'auto'
    }
  }

  const setTvOpen = (row, open) => {
    row.querySelector('.tv-details').style.display = open ? '' : 'none'
    row.querySelector('.tv-toggle').innerHTML = open ? '&#9662;' : '&#9656;'
    storeOpen('tv', row.querySelector('.tv-id').value.trim(), open)
  }

  const setGroupOpen = (row, open) => {
    row.querySelector('.tree-tv-list').style.display = open ? '' : 'none'
    row.querySelector('.group-toggle').innerHTML = open ? '&#9662;' : '&#9656;'
    storeOpen('group', row.querySelector('.group-id').value.trim(), open)
  }

  const setRootOpen = (open) => {
    const root = tree.querySelector('.hisense-root')
    root.querySelector('.tree-tv-list').style.display = open ? '' : 'none'
    root.querySelector('.root-toggle').innerHTML = open ? '&#9662;' : '&#9656;'
    storeOpen('root', 'ungrouped', open)
  }

  const moveSibling = (row, direction, selector) => {
    const siblings = [...row.parentElement.children].filter((item) => item.matches(selector))
    const index = siblings.indexOf(row)
    const other = siblings[index + direction]
    if (!other) return
    if (direction < 0) row.parentElement.insertBefore(row, other)
    else row.parentElement.insertBefore(other, row)
  }

  const renderTree = (tvs, groups, statuses) => {
    const tvById = new Map((tvs || []).map((tv) => [String(tv.id || ''), tv]))
    const assigned = new Set()
    const groupHtml = []
    for (const group of groups || []) {
      const members = []
      for (const tvId of group.tv_ids || []) {
        const id = String(tvId || '')
        const tv = tvById.get(id)
        if (tv && !assigned.has(id)) {
          assigned.add(id)
          members.push(tv)
        }
      }
      groupHtml.push(groupCard(group, members, statuses))
    }
    const ungrouped = (tvs || []).filter((tv) => !assigned.has(String(tv.id || '')))
    tree.innerHTML = rootCard(ungrouped, statuses) + groupHtml.join('')
    refreshGroupSelects()
    refreshProfileChoices()
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
      renderTree(config.hisense_tvs || [], config.hisense_tv_groups || [], statuses)
    } catch (error) {
      notify(error.message, 'danger')
    }
  }

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

  document.getElementById('hisense-refresh').addEventListener('click', load)
  document.getElementById('hisense-add-profile').addEventListener('click', () => {
    profileRows.insertAdjacentHTML('beforeend', profileCard({}, profileRows.children.length))
    refreshProfileChoices()
  })
  document.getElementById('hisense-add').addEventListener('click', () => {
    const rootList = tree.querySelector('.hisense-root > .tree-tv-list')
    rootList.insertAdjacentHTML('beforeend', tvCard({}, {}, '', true))
    setRootOpen(true)
    refreshGroupSelects()
    refreshProfileChoices()
    const row = directTvRows(rootList).at(-1)
    row?.querySelector('.tv-name')?.focus()
  })
  document.getElementById('hisense-add-group').addEventListener('click', () => {
    const name = window.prompt('Group name?')
    if (!name?.trim()) return
    const id = uniqueId('group', name)
    tree.insertAdjacentHTML('beforeend', groupCard({ id, name: name.trim(), enabled: true }, []))
    storeOpen('group', id, true)
    refreshGroupSelects()
  })
  document.getElementById('hisense-expand-all').addEventListener('click', () => {
    setRootOpen(true)
    for (const group of tree.querySelectorAll(':scope > .hisense-group')) setGroupOpen(group, true)
    for (const tv of tree.querySelectorAll('.hisense-tv')) setTvOpen(tv, true)
  })
  document.getElementById('hisense-collapse-all').addEventListener('click', () => {
    setRootOpen(false)
    for (const group of tree.querySelectorAll(':scope > .hisense-group')) setGroupOpen(group, false)
    for (const tv of tree.querySelectorAll('.hisense-tv')) setTvOpen(tv, false)
  })

  profileRows.addEventListener('input', (event) => {
    const row = event.target.closest('.hisense-profile')
    if (!row) return
    if (event.target.classList.contains('profile-name')) {
      row.querySelector('.profile-title').textContent = event.target.value || 'Certificate profile'
    }
    if (event.target.classList.contains('profile-id')) {
      const previousId = row.dataset.profileId
      const nextId = event.target.value.trim()
      refreshProfileChoices(previousId, nextId)
      if (nextId) row.dataset.profileId = nextId
    } else refreshProfileChoices()
  })

  profileRows.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]')
    const row = event.target.closest('.hisense-profile')
    if (!button || !row) return
    const action = button.dataset.action
    if (action === 'profile-remove') {
      if (profileRows.children.length <= 1) return notify('At least one certificate profile is required.', 'warning')
      row.remove()
      refreshProfileChoices()
    } else if (action === 'profile-up') moveSibling(row, -1, '.hisense-profile')
    else if (action === 'profile-down') moveSibling(row, 1, '.hisense-profile')
  })

  tree.addEventListener('change', (event) => {
    if (!event.target.classList.contains('tv-group-select')) return
    const tvRow = event.target.closest('.hisense-tv')
    const groupId = event.target.value
    const destination = groupId
      ? [...tree.querySelectorAll(':scope > .hisense-group')].find((group) => group.querySelector('.group-id').value === groupId)?.querySelector('.tree-tv-list')
      : tree.querySelector('.hisense-root > .tree-tv-list')
    if (!destination) return
    destination.appendChild(tvRow)
    tvRow.style.paddingLeft = groupId ? '44px' : '24px'
    if (groupId) setGroupOpen(destination.closest('.hisense-group'), true)
    else setRootOpen(true)
    refreshGroupSelects()
  })

  tree.addEventListener('input', (event) => {
    const tvRow = event.target.closest('.hisense-tv')
    if (!tvRow) return
    if (event.target.classList.contains('tv-name')) {
      const name = event.target.value.trim()
      tvRow.querySelector('.tv-title').textContent = name || 'New TV'
      if (tvRow.dataset.idAuto === 'true') {
        const id = uniqueId('tv', name || 'tv', tvRow)
        tvRow.querySelector('.tv-id').value = id
        tvRow.dataset.tvId = id
      }
    } else if (event.target.classList.contains('tv-id')) {
      const normalized = slugify(event.target.value, 'tv')
      event.target.value = uniqueId('tv', normalized, tvRow)
      tvRow.dataset.tvId = event.target.value
      tvRow.dataset.idAuto = 'false'
    }
  })

  tree.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]')
    if (!button) return
    const action = button.dataset.action
    const tvRow = button.closest('.hisense-tv')
    const groupRow = button.closest('.hisense-group')

    if (action === 'root-toggle') {
      const list = tree.querySelector('.hisense-root > .tree-tv-list')
      return setRootOpen(list.style.display === 'none')
    }
    if (groupRow) {
      if (action === 'group-toggle') {
        return setGroupOpen(groupRow, groupRow.querySelector('.tree-tv-list').style.display === 'none')
      }
      if (action === 'group-up') {
        moveSibling(groupRow, -1, '.hisense-group')
        return refreshGroupSelects()
      }
      if (action === 'group-down') {
        moveSibling(groupRow, 1, '.hisense-group')
        return refreshGroupSelects()
      }
      if (action === 'group-rename') {
        const nameInput = groupRow.querySelector('.group-name')
        const name = window.prompt('Rename group:', nameInput.value)
        if (name === null || !name.trim()) return
        nameInput.value = name.trim()
        groupRow.querySelector('.group-title').textContent = name.trim()
        return refreshGroupSelects()
      }
      if (action === 'group-remove') {
        if (!window.confirm('Delete this group? Its TVs will move to Ungrouped.')) return
        const rootList = tree.querySelector('.hisense-root > .tree-tv-list')
        for (const row of directTvRows(groupRow.querySelector('.tree-tv-list'))) {
          row.style.paddingLeft = '24px'
          rootList.appendChild(row)
        }
        groupRow.remove()
        setRootOpen(true)
        return refreshGroupSelects()
      }
    }
    if (!tvRow) return
    if (action === 'tv-toggle') {
      return setTvOpen(tvRow, tvRow.querySelector('.tv-details').style.display === 'none')
    }
    if (action === 'tv-up') {
      moveSibling(tvRow, -1, '.hisense-tv')
      return refreshCounts()
    }
    if (action === 'tv-down') {
      moveSibling(tvRow, 1, '.hisense-tv')
      return refreshCounts()
    }
    if (action === 'tv-remove') {
      tvRow.remove()
      return refreshCounts()
    }

    const id = tvRow.querySelector('.tv-id').value.trim()
    if (tvRow.dataset.saved !== 'true') {
      return notify('Save the new TV before testing, reconnecting, or pairing it.', 'warning')
    }
    try {
      if (action === 'tv-reconnect') {
        await request(`/api/tvs/${encodeURIComponent(id)}/reconnect`, { method: 'POST', body: '{}' })
      } else if (action === 'tv-pair') {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/request`, { method: 'POST', body: '{}' })
        notify('Pairing request sent. If the TV reports that the app is no longer compatible, select a newer certificate profile, save, reconnect, and retry.', 'warning')
        return
      } else if (action === 'tv-submit-pin') {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/submit`, {
          method: 'POST',
          body: JSON.stringify({ pin: tvRow.querySelector('.tv-pin').value.trim() }),
        })
      } else if (action === 'tv-test') {
        await request(`/api/tvs/${encodeURIComponent(id)}/volume`, {
          method: 'POST',
          body: JSON.stringify({ action: 'up' }),
        })
      } else return
      notify('TV command accepted.')
      await load()
    } catch (error) {
      notify(error.message, 'danger')
    }
  })

  load()
})()
