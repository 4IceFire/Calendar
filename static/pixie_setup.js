(() => {
  const root = document.getElementById('pixie-setup')
  if (!root) return

  const alertBox = document.getElementById('pixie-setup-alert')
  const tree = document.getElementById('pixie-device-tree')
  const sceneList = document.getElementById('pixie-scene-list')
  const unsaved = document.getElementById('pixie-unsaved')
  const saveButton = document.getElementById('pixie-save')
  const csrfToken = String(root.dataset.csrfToken || '')
  let config = { pixie_auditoriums: [], pixie_devices: [], pixie_scenes: [] }
  let status = {}
  let dirty = false

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char])

  const slugify = (value, fallback = 'auditorium') => {
    const result = String(value || '').trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
    return result || fallback
  }

  const notify = (message, kind = 'success') => {
    alertBox.innerHTML = message ? `<div class="alert alert-${kind}" role="alert">${escapeHtml(message)}</div>` : ''
  }

  const request = async (path, options = {}) => {
    const response = await fetch(path, {
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken } : {}),
        ...(options.headers || {}),
      },
      ...options,
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `${response.status} ${response.statusText}`)
    return payload
  }

  const markDirty = () => {
    dirty = true
    unsaved.textContent = 'Unsaved changes'
    unsaved.classList.remove('text-muted')
    unsaved.classList.add('text-warning')
  }

  const markSaved = () => {
    dirty = false
    unsaved.textContent = 'No unsaved changes'
    unsaved.classList.remove('text-warning')
    unsaved.classList.add('text-muted')
  }

  const collapseKey = (kind, id) => `tdeck:pixie:setup:${kind}:${id}:open`
  const isOpen = (kind, id, fallback = true) => {
    try {
      const value = localStorage.getItem(collapseKey(kind, id))
      return value === null ? fallback : value === '1'
    } catch (_error) { return fallback }
  }
  const setOpen = (kind, id, open) => {
    try { localStorage.setItem(collapseKey(kind, id), open ? '1' : '0') } catch (_error) {}
  }

  const deviceById = (id) => (config.pixie_devices || []).find((device) => device.id === id)
  const auditoriumById = (id) => (config.pixie_auditoriums || []).find((auditorium) => auditorium.id === id)
  const assignedDeviceIds = () => new Set((config.pixie_auditoriums || []).flatMap((auditorium) => auditorium.device_ids || []))
  const ungroupedDevices = () => {
    const assigned = assignedDeviceIds()
    return (config.pixie_devices || []).filter((device) => !assigned.has(device.id))
  }

  const orderButtons = (kind, id) => `<div class="btn-group btn-group-sm pixie-order-buttons" role="group" aria-label="Reorder">
    <button class="btn btn-outline-secondary" type="button" data-action="${kind}-up" data-id="${escapeHtml(id)}" title="Move up">&uarr;</button>
    <button class="btn btn-outline-secondary" type="button" data-action="${kind}-down" data-id="${escapeHtml(id)}" title="Move down">&darr;</button>
  </div>`

  const auditoriumOptions = (selected) => [
    `<option value="" ${selected ? '' : 'selected'}>Ungrouped</option>`,
    ...(config.pixie_auditoriums || []).map((auditorium) =>
      `<option value="${escapeHtml(auditorium.id)}" ${selected === auditorium.id ? 'selected' : ''}>${escapeHtml(auditorium.name)}</option>`),
  ].join('')

  const deviceRow = (device, auditoriumId = '') => {
    const open = isOpen('device', device.id, false)
    const stateText = device.missing
      ? 'Missing from inventory'
      : (device.online === true ? 'Online' : (device.online === false ? 'Offline' : 'Status unavailable'))
    const stateClass = device.missing || device.online === false
      ? 'text-warning'
      : (device.online === true ? 'text-success' : 'text-muted')
    return `<div class="list-group-item pixie-setup-device" data-device-id="${escapeHtml(device.id)}">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <button class="btn btn-sm btn-outline-secondary" type="button" data-action="device-toggle" data-id="${escapeHtml(device.id)}" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 text-truncate" style="min-width:150px">
          <div class="fw-semibold text-truncate" data-device-title="${escapeHtml(device.id)}">${escapeHtml(device.name)}</div>
          <div class="pixie-setup-original">Pixie: ${escapeHtml(device.original_name)} · <span class="${stateClass}">${escapeHtml(stateText)}</span></div>
        </div>
        <select class="form-select form-select-sm" data-device-group="${escapeHtml(device.id)}" aria-label="Device auditorium" style="max-width:210px">${auditoriumOptions(auditoriumId)}</select>
        ${orderButtons('device', device.id)}
      </div>
      <div class="mt-3" data-device-details="${escapeHtml(device.id)}" ${open ? '' : 'style="display:none"'}>
        <div class="row g-3">
          <div class="col-md-4">
            <label class="form-label">TDeck display name</label>
            <input class="form-control" data-device-name="${escapeHtml(device.id)}" value="${escapeHtml(device.name)}">
          </div>
          <div class="col-md-4">
            <label class="form-label">Original Pixie name</label>
            <input class="form-control" value="${escapeHtml(device.original_name)}" readonly>
          </div>
          <div class="col-md-4">
            <label class="form-label">Control type</label>
            <select class="form-select" data-device-control-type="${escapeHtml(device.id)}">
              <option value="automatic" ${device.control_type === 'automatic' ? 'selected' : ''}>Automatic (${escapeHtml(device.detected_kind || 'unknown')})</option>
              <option value="dimmer" ${device.control_type === 'dimmer' ? 'selected' : ''}>Dimmable</option>
              <option value="on_off" ${device.control_type === 'on_off' ? 'selected' : ''}>On/Off</option>
            </select>
          </div>
        </div>
        <div class="small text-muted mt-2">ID: <code>${escapeHtml(device.id)}</code>${device.model ? ` · Model: ${escapeHtml(device.model)}` : ''}</div>
      </div>
    </div>`
  }

  const auditoriumGroup = (auditorium) => {
    const open = isOpen('auditorium', auditorium.id, true)
    const devices = (auditorium.device_ids || []).map(deviceById).filter(Boolean)
    return `<div data-auditorium-id="${escapeHtml(auditorium.id)}">
      <div class="list-group-item d-flex flex-wrap align-items-center gap-2">
        <button class="btn btn-sm btn-outline-secondary" type="button" data-action="auditorium-toggle" data-id="${escapeHtml(auditorium.id)}" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <input class="form-control form-control-sm fw-semibold flex-grow-1" data-auditorium-name="${escapeHtml(auditorium.id)}" value="${escapeHtml(auditorium.name)}" aria-label="Auditorium name" style="min-width:180px">
        <span class="small text-muted">${devices.length} device${devices.length === 1 ? '' : 's'}</span>
        ${orderButtons('auditorium', auditorium.id)}
        <button class="btn btn-sm btn-outline-danger" type="button" data-action="auditorium-delete" data-id="${escapeHtml(auditorium.id)}">Delete</button>
      </div>
      <div class="list-group list-group-flush" data-auditorium-devices="${escapeHtml(auditorium.id)}" ${open ? '' : 'style="display:none"'}>
        ${devices.map((device) => deviceRow(device, auditorium.id)).join('') || '<div class="list-group-item text-muted small ps-5">No devices assigned.</div>'}
      </div>
    </div>`
  }

  const ungroupedGroup = () => {
    const open = isOpen('root', 'ungrouped', true)
    const devices = ungroupedDevices()
    return `<div data-ungrouped-root="1">
      <div class="list-group-item d-flex align-items-center gap-2">
        <button class="btn btn-sm btn-outline-secondary" type="button" data-action="root-toggle" data-id="ungrouped" style="width:32px">${open ? '&#9662;' : '&#9656;'}</button>
        <div class="flex-grow-1 fw-semibold">Ungrouped devices</div>
        <span class="small text-muted">${devices.length} device${devices.length === 1 ? '' : 's'}</span>
      </div>
      <div class="list-group list-group-flush" data-ungrouped-devices="1" ${open ? '' : 'style="display:none"'}>
        ${devices.map((device) => deviceRow(device, '')).join('') || '<div class="list-group-item text-muted small ps-5">No ungrouped devices.</div>'}
      </div>
    </div>`
  }

  const renderTree = () => {
    tree.innerHTML = (config.pixie_auditoriums || []).map(auditoriumGroup).join('') + ungroupedGroup()
  }

  const renderScenes = () => {
    sceneList.innerHTML = (config.pixie_scenes || []).map((scene) => `
      <div class="list-group-item" data-scene-id="${escapeHtml(scene.id)}">
        <div class="row g-2 align-items-center">
          <div class="col-12 col-md-4">
            <label class="form-label small">TDeck display name</label>
            <input class="form-control form-control-sm" data-scene-name="${escapeHtml(scene.id)}" value="${escapeHtml(scene.name)}">
          </div>
          <div class="col-12 col-md-4">
            <label class="form-label small">Original Pixie name</label>
            <div class="form-control form-control-sm bg-body-tertiary text-muted">${escapeHtml(scene.original_name)}</div>
          </div>
          <div class="col-6 col-md-2">
            <div class="form-check form-switch mt-md-4">
              <input class="form-check-input" type="checkbox" data-scene-enabled="${escapeHtml(scene.id)}" ${scene.enabled ? 'checked' : ''}>
              <label class="form-check-label">Enabled</label>
            </div>
          </div>
          <div class="col-6 col-md-2 text-end mt-md-4">${orderButtons('scene', scene.id)}</div>
        </div>
        <div class="small text-muted mt-1">ID: <code>${escapeHtml(scene.id)}</code>${scene.missing ? ' · Missing from inventory' : ''}</div>
      </div>`).join('') || '<div class="text-muted small">No scenes have been discovered.</div>'
  }

  const renderConnection = () => {
    document.getElementById('pixie-network-mode').value = config.pixie_network_mode || 'disabled'
    document.getElementById('pixie-gateway-host').value = config.pixie_gateway_host || ''
    document.getElementById('pixie-username').value = config.pixie_username || ''
    document.getElementById('pixie-password').placeholder = config.pixie_password_configured
      ? 'Saved — leave blank to keep it'
      : 'Enter Pixie account password'
    setHomeOptions([{ id: config.pixie_home_id || '', name: config.pixie_home_name || config.pixie_home_id || 'Select a Home' }], config.pixie_home_id || '')
    document.getElementById('pixie-net-id').textContent = config.pixie_net_id || '—'
    document.getElementById('pixie-mesh-net').textContent = config.pixie_mesh_net || '—'
    document.getElementById('pixie-mesh-net-2').textContent = config.pixie_mesh_net_2 || '—'
    const badge = document.getElementById('pixie-connection-badge')
    const connected = !!status.connected
    const mode = String(status.mode || config.pixie_network_mode || 'disabled')
    const label = mode === 'disabled' ? 'Disabled' : mode === 'observe' && connected ? 'Observe only' : connected ? 'Connected' : 'Offline'
    badge.textContent = label
    badge.className = `badge ${connected ? 'text-bg-success' : mode === 'disabled' ? 'text-bg-secondary' : 'text-bg-danger'}`
    const inventoryTime = status.lastInventoryAt ? new Date(Number(status.lastInventoryAt) * 1000).toLocaleString() : 'never'
    document.getElementById('pixie-diagnostics').textContent = [
      `Gateway: ${status.gatewayHost || config.pixie_gateway_host || 'not selected'}`,
      `Inventory: ${status.deviceCount || 0} devices, ${status.sceneCount || 0} scenes (last refresh ${inventoryTime})`,
      `Control session: ${status.controlReady ? 'ready' : 'not ready'}`,
      status.lastError ? `Last error: ${status.lastError}` : '',
    ].filter(Boolean).join(' · ')
  }

  const setHomeOptions = (homes, selectedId = '') => {
    const select = document.getElementById('pixie-home-id')
    const cleaned = (homes || []).filter((home, index, list) => home.id || (index === 0 && !list.some((item) => item.id)))
    select.innerHTML = cleaned.map((home) => `<option value="${escapeHtml(home.id)}" ${String(home.id) === String(selectedId) ? 'selected' : ''}>${escapeHtml(home.name || home.id || 'Select a Home')}</option>`).join('')
    if (!select.options.length) select.innerHTML = '<option value="">Load homes to choose</option>'
  }

  const loadConfig = async ({ refresh = false } = {}) => {
    const result = await request(`/api/pixie/config${refresh ? '?refresh=1' : ''}`)
    config = result.config || config
    status = result.status || {}
    renderConnection()
    renderTree()
    renderScenes()
    markSaved()
    return result
  }

  const moveInList = (list, id, direction, key = 'id') => {
    const index = list.findIndex((item) => String(item[key]) === String(id))
    const next = index + direction
    if (index < 0 || next < 0 || next >= list.length) return false
    ;[list[index], list[next]] = [list[next], list[index]]
    return true
  }

  const moveDevice = (deviceId, direction) => {
    const group = (config.pixie_auditoriums || []).find((item) => (item.device_ids || []).includes(deviceId))
    if (group) {
      const index = group.device_ids.indexOf(deviceId)
      const next = index + direction
      if (next < 0 || next >= group.device_ids.length) return false
      ;[group.device_ids[index], group.device_ids[next]] = [group.device_ids[next], group.device_ids[index]]
      return true
    }
    const ungrouped = ungroupedDevices()
    const index = ungrouped.findIndex((item) => item.id === deviceId)
    const other = ungrouped[index + direction]
    if (!other) return false
    const a = config.pixie_devices.findIndex((item) => item.id === deviceId)
    const b = config.pixie_devices.findIndex((item) => item.id === other.id)
    ;[config.pixie_devices[a], config.pixie_devices[b]] = [config.pixie_devices[b], config.pixie_devices[a]]
    return true
  }

  const moveDeviceToAuditorium = (deviceId, targetId) => {
    ;(config.pixie_auditoriums || []).forEach((auditorium) => {
      auditorium.device_ids = (auditorium.device_ids || []).filter((id) => id !== deviceId)
    })
    const target = auditoriumById(targetId)
    if (target) target.device_ids.push(deviceId)
  }

  tree.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]')
    if (!button) return
    const action = button.dataset.action
    const id = String(button.dataset.id || '')
    if (action === 'auditorium-toggle') {
      const next = !isOpen('auditorium', id, true)
      setOpen('auditorium', id, next)
      renderTree()
      return
    }
    if (action === 'root-toggle') {
      const next = !isOpen('root', 'ungrouped', true)
      setOpen('root', 'ungrouped', next)
      renderTree()
      return
    }
    if (action === 'device-toggle') {
      const next = !isOpen('device', id, false)
      setOpen('device', id, next)
      renderTree()
      return
    }
    if (action === 'auditorium-delete') {
      const auditorium = auditoriumById(id)
      if (!auditorium || !window.confirm(`Delete ${auditorium.name}? Its devices will move to Ungrouped.`)) return
      config.pixie_auditoriums = config.pixie_auditoriums.filter((item) => item.id !== id)
      markDirty(); renderTree(); return
    }
    if (action === 'auditorium-up' || action === 'auditorium-down') {
      if (moveInList(config.pixie_auditoriums, id, action.endsWith('up') ? -1 : 1)) { markDirty(); renderTree() }
      return
    }
    if (action === 'device-up' || action === 'device-down') {
      if (moveDevice(id, action.endsWith('up') ? -1 : 1)) { markDirty(); renderTree() }
    }
  })

  tree.addEventListener('input', (event) => {
    const auditoriumInput = event.target.closest('[data-auditorium-name]')
    if (auditoriumInput) {
      const auditorium = auditoriumById(auditoriumInput.dataset.auditoriumName)
      if (auditorium) auditorium.name = auditoriumInput.value
      markDirty(); return
    }
    const deviceInput = event.target.closest('[data-device-name]')
    if (deviceInput) {
      const device = deviceById(deviceInput.dataset.deviceName)
      if (device) {
        device.name = deviceInput.value
        const title = tree.querySelector(`[data-device-title="${CSS.escape(device.id)}"]`)
        if (title) title.textContent = device.name
      }
      markDirty()
    }
  })

  tree.addEventListener('change', (event) => {
    const groupSelect = event.target.closest('[data-device-group]')
    if (groupSelect) {
      moveDeviceToAuditorium(groupSelect.dataset.deviceGroup, groupSelect.value)
      markDirty(); renderTree(); return
    }
    const typeSelect = event.target.closest('[data-device-control-type]')
    if (typeSelect) {
      const device = deviceById(typeSelect.dataset.deviceControlType)
      if (device) device.control_type = typeSelect.value
      markDirty()
    }
  })

  sceneList.addEventListener('input', (event) => {
    const input = event.target.closest('[data-scene-name]')
    if (!input) return
    const scene = config.pixie_scenes.find((item) => item.id === input.dataset.sceneName)
    if (scene) scene.name = input.value
    markDirty()
  })

  sceneList.addEventListener('change', (event) => {
    const input = event.target.closest('[data-scene-enabled]')
    if (!input) return
    const scene = config.pixie_scenes.find((item) => item.id === input.dataset.sceneEnabled)
    if (scene) scene.enabled = !!input.checked
    markDirty()
  })

  sceneList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action^="scene-"]')
    if (!button) return
    const direction = button.dataset.action.endsWith('up') ? -1 : 1
    if (moveInList(config.pixie_scenes, button.dataset.id, direction)) { markDirty(); renderScenes() }
  })

  document.getElementById('pixie-add-auditorium').addEventListener('click', () => {
    const name = window.prompt('Auditorium name')
    if (!name || !name.trim()) return
    const used = new Set(config.pixie_auditoriums.map((item) => item.id))
    const base = slugify(name)
    let id = base
    let suffix = 2
    while (used.has(id)) id = `${base}-${suffix++}`
    config.pixie_auditoriums.push({ id, name: name.trim(), device_ids: [] })
    setOpen('auditorium', id, true)
    markDirty(); renderTree()
  })

  document.getElementById('pixie-expand-all').addEventListener('click', () => {
    config.pixie_auditoriums.forEach((item) => setOpen('auditorium', item.id, true))
    config.pixie_devices.forEach((item) => setOpen('device', item.id, true))
    setOpen('root', 'ungrouped', true)
    renderTree()
  })

  document.getElementById('pixie-collapse-all').addEventListener('click', () => {
    config.pixie_auditoriums.forEach((item) => setOpen('auditorium', item.id, false))
    config.pixie_devices.forEach((item) => setOpen('device', item.id, false))
    setOpen('root', 'ungrouped', false)
    renderTree()
  })

  document.getElementById('pixie-discover').addEventListener('click', async (event) => {
    const button = event.currentTarget
    button.disabled = true
    notify('Listening for a Pixie Gateway…', 'info')
    try {
      const result = await request('/api/pixie/discover', { method: 'POST', body: JSON.stringify({}) })
      if (result.gateways.length === 1) {
        document.getElementById('pixie-gateway-host').value = result.gateways[0].host
        config.pixie_gateway_host = result.gateways[0].host
        markDirty(); notify(`Found Gateway at ${result.gateways[0].host}`)
      } else if (!result.gateways.length) {
        notify('No Pixie Gateway advertisement was received.', 'warning')
      } else {
        notify(`Multiple Gateways were found: ${result.gateways.map((item) => item.host).join(', ')}. Enter the required address.`, 'warning')
      }
    } catch (error) { notify(error.message || String(error), 'danger') }
    finally { button.disabled = false }
  })

  document.getElementById('pixie-load-homes').addEventListener('click', async (event) => {
    const button = event.currentTarget
    button.disabled = true
    try {
      const result = await request('/api/pixie/homes', {
        method: 'POST',
        body: JSON.stringify({
          username: document.getElementById('pixie-username').value,
          password: document.getElementById('pixie-password').value,
        }),
      })
      const selected = config.pixie_home_id || result.currentHomeId || (result.homes[0] || {}).id || ''
      setHomeOptions(result.homes, selected)
      notify(`Loaded ${result.homes.length} Pixie Home${result.homes.length === 1 ? '' : 's'}.`)
    } catch (error) { notify(error.message || String(error), 'danger') }
    finally { button.disabled = false }
  })

  document.getElementById('pixie-refresh-inventory').addEventListener('click', async (event) => {
    const button = event.currentTarget
    button.disabled = true
    try {
      await loadConfig({ refresh: true })
      notify('Pixie inventory refreshed.')
    } catch (error) { notify(error.message || String(error), 'danger') }
    finally { button.disabled = false }
  })

  ;['pixie-network-mode', 'pixie-gateway-host', 'pixie-username', 'pixie-password', 'pixie-home-id'].forEach((id) => {
    document.getElementById(id).addEventListener('change', markDirty)
    if (id !== 'pixie-network-mode' && id !== 'pixie-home-id') document.getElementById(id).addEventListener('input', markDirty)
  })

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true
    notify('Saving configuration and restarting the Pixie service…', 'info')
    try {
      const result = await request('/api/pixie/config', {
        method: 'PUT',
        body: JSON.stringify({
          pixie_network_mode: document.getElementById('pixie-network-mode').value,
          pixie_gateway_host: document.getElementById('pixie-gateway-host').value,
          pixie_username: document.getElementById('pixie-username').value,
          pixie_password: document.getElementById('pixie-password').value,
          pixie_home_id: document.getElementById('pixie-home-id').value,
          pixie_auditoriums: config.pixie_auditoriums,
          pixie_devices: config.pixie_devices,
          pixie_scenes: config.pixie_scenes,
        }),
      })
      document.getElementById('pixie-password').value = ''
      markSaved()
      await loadConfig()
      if (result.status && result.status.lastError) {
        notify(`Configuration saved, but Pixie did not reconnect: ${result.status.lastError}`, 'warning')
      } else {
        notify('Pixie configuration saved and service restarted.')
      }
    } catch (error) { notify(error.message || String(error), 'danger') }
    finally { saveButton.disabled = false }
  })

  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return
    event.preventDefault()
    event.returnValue = ''
  })

  loadConfig().catch((error) => {
    notify(error.message || String(error), 'danger')
    tree.textContent = 'Could not load Pixie configuration.'
  })
})()
