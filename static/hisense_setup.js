(() => {
  const tree = document.getElementById('hisense-tree')
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

  const friendlyError = (message) => {
    const text = String(message || '')
    if (/paired device UUID is required/i.test(text)) {
      return 'Phone pairing required. Open Pair or repair for this TV.'
    }
    if (/certificate profile.+files were not found|client certificate\/key not found|support files are missing/i.test(text)) {
      return 'TDeck TV support files need to be installed.'
    }
    if (/connection timed out or authentication was rejected/i.test(text)) {
      return 'The TV did not accept the connection. It may need pairing or repair.'
    }
    return text
  }

  const tvStatusText = (status = {}) => {
    if (!status.connected) {
      return status.lastError ? `Offline - ${friendlyError(status.lastError)}` : 'Offline'
    }
    return `Online - ${status.power || 'unknown'} - Vol ${status.volume ?? '-'} - ${status.source || 'no source'}`
  }

  const tvStatusClass = (status = {}) => {
    if (status.connected) return 'text-success'
    return status.lastError ? 'text-danger' : 'text-muted'
  }

  const healthSummary = (total, online, errors) => {
    if (!total) return {
      text: '0 TVs',
      className: 'text-muted',
    }
    return {
      text: `${online}/${total} online${errors ? ` - ${errors} error${errors === 1 ? '' : 's'}` : ''}`,
      className: errors
        ? 'text-danger'
        : (online === total ? 'text-success' : (online ? 'text-warning' : 'text-muted')),
    }
  }

  const healthFromTvs = (tvs = [], statuses = {}) => {
    const states = tvs.map((tv) => statuses[tv.id] || {})
    return healthSummary(
      tvs.length,
      states.filter((status) => status.connected).length,
      states.filter((status) => status.lastError).length,
    )
  }

  const applyHealth = (label, rows) => {
    if (!label) return
    const summary = healthSummary(
      rows.length,
      rows.filter((row) => row.dataset.connected === 'true').length,
      rows.filter((row) => row.dataset.hasError === 'true').length,
    )
    label.textContent = summary.text
    label.classList.remove('text-muted', 'text-success', 'text-warning', 'text-danger')
    label.classList.add(summary.className)
  }

  const tvCard = (tv = {}, status = {}, groupId = '', isNew = false) => {
    const id = tv.id || uniqueId('tv', tv.name || 'tv')
    const open = isNew ? true : storedOpen('tv', id, false)
    const requiresUuid = /paired device UUID is required/i.test(String(status.lastError || ''))
    const pairingOpen = storedOpen('pair', id, requiresUuid)
    return `<div class="list-group-item hisense-tv"
      data-tv-id="${escapeHtml(id)}"
      data-id-auto="${isNew ? 'true' : 'false'}"
      data-saved="${isNew ? 'false' : 'true'}"
      data-connected="${status.connected ? 'true' : 'false'}"
      data-has-error="${status.lastError ? 'true' : 'false'}"
      data-requires-uuid="${requiresUuid ? 'true' : 'false'}"
      style="padding-left:${groupId ? '44px' : '24px'}">
      <input type="hidden" class="tv-id" value="${escapeHtml(id)}">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary tv-toggle" data-action="tv-toggle" title="Expand/collapse TV" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 text-truncate">
          <div class="fw-semibold text-truncate tv-title">${escapeHtml(tv.name || 'New TV')}</div>
          <div class="small ${tvStatusClass(status)} text-truncate tv-status">${escapeHtml(tvStatusText(status))}</div>
        </div>
        <select class="form-select form-select-sm tv-group-select" aria-label="TV group" style="max-width:210px">${groupOptions(groupId)}</select>
        ${orderButtons('tv')}
        <button type="button" class="btn btn-sm btn-outline-danger" data-action="tv-remove">Remove</button>
      </div>
      <div class="tv-details mt-3" ${open ? '' : 'style="display:none"'}>
        ${status.model ? `<div class="small text-muted mb-3">Detected model: ${escapeHtml(status.model)}</div>` : ''}
        <div class="row g-3">
          <div class="col-md-4">
            <label class="form-label">TV name</label>
            <input class="form-control tv-name" value="${escapeHtml(tv.name || '')}" placeholder="Foyer TV">
            <div class="form-text">The name shown in TDeck and Companion.</div>
          </div>
          <div class="col-md-4">
            <label class="form-label">IP address</label>
            <input class="form-control tv-host" value="${escapeHtml(tv.host || '')}" placeholder="10.5.10.175">
          </div>
          <div class="col-md-4">
            <label class="form-label">TV MAC address</label>
            <input class="form-control tv-mac" value="${escapeHtml(tv.mac || '')}" placeholder="e4:8a:93:f1:da:22">
            <div class="form-text">Used for Wake-on-LAN power-on.</div>
          </div>
        </div>
        <div class="d-flex flex-wrap gap-2 mt-3">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-action="tv-reconnect">Reconnect</button>
          <button type="button" class="btn btn-sm btn-outline-warning" data-action="tv-pairing-toggle">${pairingOpen ? 'Hide pairing' : 'Pair or repair'}</button>
          <button type="button" class="btn btn-sm btn-outline-primary" data-action="tv-test">Test volume +</button>
        </div>
        <div class="tv-pairing mt-3 p-3 rounded border border-warning-subtle" ${pairingOpen ? '' : 'style="display:none"'}>
          <div class="small text-muted mb-3">
            Most older TVs only need the PIN buttons below. For a newer TV, first pair the
            official VIDAA phone app, then enter that phone's case-sensitive Wi-Fi/Bluetooth
            MAC or UUID here. This is not the TV MAC address.
          </div>
          <div class="row g-3 align-items-end">
            <div class="col-lg-5">
              <label class="form-label">Paired phone UUID <span class="text-muted">(newer TVs only)</span></label>
              <input class="form-control tv-uuid" value="${escapeHtml(tv.uuid || '')}" placeholder="AA:BB:CC:DD:EE:FF">
            </div>
            <div class="col-lg-7">
              <div class="d-flex flex-wrap gap-2">
                <button type="button" class="btn btn-sm btn-outline-warning" data-action="tv-pair">Request pairing PIN</button>
                <div class="input-group input-group-sm" style="max-width:220px">
                  <input class="form-control tv-pin" maxlength="4" inputmode="numeric" placeholder="4-digit PIN">
                  <button class="btn btn-outline-warning" type="button" data-action="tv-submit-pin">Submit PIN</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>`
  }

  const groupCard = (group = {}, tvs = [], statuses = {}) => {
    const id = group.id || uniqueId('group', group.name || 'group')
    const open = storedOpen('group', id, true)
    const health = healthFromTvs(tvs, statuses)
    return `<div class="hisense-group" data-group-id="${escapeHtml(id)}">
      <input type="hidden" class="group-id" value="${escapeHtml(id)}">
      <input type="hidden" class="group-name" value="${escapeHtml(group.name || id)}">
      <div class="list-group-item d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary group-toggle" data-action="group-toggle" title="Expand/collapse group" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 text-truncate">
          <div class="fw-semibold text-truncate group-title">${escapeHtml(group.name || id)}</div>
          <div class="small ${health.className} group-health">${escapeHtml(health.text)}</div>
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
    const health = healthFromTvs(tvs, statuses)
    return `<div class="hisense-root">
      <div class="list-group-item d-flex flex-wrap align-items-center gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary root-toggle" data-action="root-toggle" title="Expand/collapse ungrouped TVs" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1">
          <div class="fw-semibold">Ungrouped TVs</div>
          <div class="small ${health.className} root-health">${escapeHtml(health.text)}</div>
        </div>
      </div>
      <div class="tree-tv-list list-group list-group-flush" data-group-id="" ${open ? '' : 'style="display:none"'}>
        ${tvs.map((tv) => tvCard(tv, statuses[tv.id] || {}, '')).join('')}
      </div>
    </div>`
  }

  const directTvRows = (list) => [...list.children].filter((child) => child.classList.contains('hisense-tv'))

  const currentTvs = () => [...tree.querySelectorAll('.hisense-tv')].map((row, index) => ({
    id: row.querySelector('.tv-id').value.trim()
      || uniqueId('tv', row.querySelector('.tv-name').value || `tv-${index + 1}`, row),
    name: row.querySelector('.tv-name').value.trim() || `TV ${index + 1}`,
    host: row.querySelector('.tv-host').value.trim(),
    mac: row.querySelector('.tv-mac').value.trim(),
    uuid: row.querySelector('.tv-uuid').value.trim(),
  }))

  const currentGroups = () => [...tree.querySelectorAll(':scope > .hisense-group')].map((row, index) => {
    const list = row.querySelector('.tree-tv-list')
    return {
      id: row.querySelector('.group-id').value.trim() || `group-${index + 1}`,
      name: row.querySelector('.group-name').value.trim() || `Group ${index + 1}`,
      tv_ids: directTvRows(list).map((tvRow) => tvRow.querySelector('.tv-id').value.trim()),
    }
  })

  const parentGroupId = (tvRow) =>
    tvRow.closest('.hisense-group')?.querySelector('.group-id')?.value.trim() || ''

  const refreshHealthSummaries = () => {
    const rootList = tree.querySelector('.hisense-root > .tree-tv-list')
    applyHealth(
      tree.querySelector('.root-health'),
      rootList ? directTvRows(rootList) : [],
    )
    for (const group of tree.querySelectorAll(':scope > .hisense-group')) {
      applyHealth(
        group.querySelector('.group-health'),
        directTvRows(group.querySelector('.tree-tv-list')),
      )
    }
  }

  const refreshGroupSelects = () => {
    for (const tvRow of tree.querySelectorAll('.hisense-tv')) {
      const selected = parentGroupId(tvRow)
      const select = tvRow.querySelector('.tv-group-select')
      select.innerHTML = groupOptions(selected)
      select.value = selected
    }
    refreshHealthSummaries()
  }

  const setTvOpen = (row, open) => {
    row.querySelector('.tv-details').style.display = open ? '' : 'none'
    row.querySelector('.tv-toggle').innerHTML = open ? '&#9662;' : '&#9656;'
    storeOpen('tv', row.querySelector('.tv-id').value.trim(), open)
  }

  const setPairingOpen = (row, open) => {
    row.querySelector('.tv-pairing').style.display = open ? '' : 'none'
    row.querySelector('[data-action="tv-pairing-toggle"]').textContent = open ? 'Hide pairing' : 'Pair or repair'
    storeOpen('pair', row.querySelector('.tv-id').value.trim(), open)
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
  }

  const load = async () => {
    try {
      const [settings, state] = await Promise.all([
        request('/api/hisense/config'),
        request('/api/tvs'),
      ])
      config = settings.config || {}
      const statuses = Object.fromEntries((state.tvs || []).map((tv) => [tv.id, tv]))
      renderTree(config.hisense_tvs || [], config.hisense_tv_groups || [], statuses)
    } catch (error) {
      notify(friendlyError(error.message), 'danger')
    }
  }

  const saveConfiguration = async ({ reload = true, announce = true } = {}) => {
    const payload = {
      hisense_tvs: currentTvs(),
      hisense_tv_groups: currentGroups(),
    }
    const result = await request('/api/hisense/config', {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
    for (const row of tree.querySelectorAll('.hisense-tv')) row.dataset.saved = 'true'
    if (announce) notify('TV configuration saved. Connections have restarted.')
    if (reload) await load()
    return result
  }

  document.getElementById('hisense-save').addEventListener('click', async () => {
    try {
      await saveConfiguration()
    } catch (error) {
      notify(friendlyError(error.message), 'danger')
    }
  })

  document.getElementById('hisense-refresh').addEventListener('click', load)
  document.getElementById('hisense-add').addEventListener('click', () => {
    const rootList = tree.querySelector('.hisense-root > .tree-tv-list')
    rootList.insertAdjacentHTML('beforeend', tvCard({}, {}, '', true))
    setRootOpen(true)
    refreshGroupSelects()
    const row = directTvRows(rootList).at(-1)
    row?.querySelector('.tv-name')?.focus()
  })
  document.getElementById('hisense-add-group').addEventListener('click', () => {
    const name = window.prompt('Group name?')
    if (!name?.trim()) return
    const id = uniqueId('group', name)
    tree.insertAdjacentHTML('beforeend', groupCard({ id, name: name.trim() }, []))
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

  tree.addEventListener('change', (event) => {
    if (!event.target.classList.contains('tv-group-select')) return
    const tvRow = event.target.closest('.hisense-tv')
    const groupId = event.target.value
    const destination = groupId
      ? [...tree.querySelectorAll(':scope > .hisense-group')]
        .find((group) => group.querySelector('.group-id').value === groupId)
        ?.querySelector('.tree-tv-list')
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
    if (action === 'tv-pairing-toggle') {
      return setPairingOpen(tvRow, tvRow.querySelector('.tv-pairing').style.display === 'none')
    }
    if (action === 'tv-up') {
      moveSibling(tvRow, -1, '.hisense-tv')
      return refreshHealthSummaries()
    }
    if (action === 'tv-down') {
      moveSibling(tvRow, 1, '.hisense-tv')
      return refreshHealthSummaries()
    }
    if (action === 'tv-remove') {
      tvRow.remove()
      return refreshHealthSummaries()
    }

    const id = tvRow.querySelector('.tv-id').value.trim()
    const pin = tvRow.querySelector('.tv-pin').value.trim()
    try {
      if (
        action === 'tv-pair'
        && tvRow.dataset.requiresUuid === 'true'
        && !tvRow.querySelector('.tv-uuid').value.trim()
      ) {
        throw new Error(
          'Pair this TV with the official VIDAA phone app first, then enter the phone UUID in Pair or repair.',
        )
      }
      await saveConfiguration({ reload: false, announce: false })
      if (action === 'tv-reconnect') {
        await request(`/api/tvs/${encodeURIComponent(id)}/reconnect`, {
          method: 'POST',
          body: '{}',
        })
        notify('Reconnect requested.')
      } else if (action === 'tv-pair') {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/request`, {
          method: 'POST',
          body: '{}',
        })
        notify('Pairing request sent. Enter the four-digit PIN shown by the TV.')
      } else if (action === 'tv-submit-pin') {
        await request(`/api/tvs/${encodeURIComponent(id)}/pair/submit`, {
          method: 'POST',
          body: JSON.stringify({ pin }),
        })
        notify('Pairing PIN submitted.')
      } else if (action === 'tv-test') {
        await request(`/api/tvs/${encodeURIComponent(id)}/volume`, {
          method: 'POST',
          body: JSON.stringify({ action: 'up' }),
        })
        notify('Volume test sent.')
      } else {
        return
      }
      await load()
    } catch (error) {
      notify(friendlyError(error.message), 'danger')
    }
  })

  load()
})()
