(() => {
  const root = document.getElementById('pixie-controls')
  if (!root) return

  const alertBox = document.getElementById('pixie-controls-alert')
  const chooser = document.getElementById('pixie-auditorium-chooser')
  const auditoriumTiles = document.getElementById('pixie-auditorium-tiles')
  const controls = document.getElementById('pixie-auditorium-controls')
  const deviceTiles = document.getElementById('pixie-device-tiles')
  const sceneTiles = document.getElementById('pixie-scene-tiles')
  const noAccess = document.getElementById('pixie-no-access')
  const backButton = document.getElementById('pixie-auditorium-back')
  const auditoriumTitle = document.getElementById('pixie-auditorium-title')
  const auditoriumStatus = document.getElementById('pixie-auditorium-status')
  const selectionPanel = document.getElementById('pixie-selection-controls')
  const selectionCount = document.getElementById('pixie-selection-count')
  const selectionHelp = document.getElementById('pixie-selection-help')
  const faderControls = document.getElementById('pixie-fader-controls')
  const switchControls = document.getElementById('pixie-switch-controls')
  const levelRange = document.getElementById('pixie-level')
  const levelOutput = document.getElementById('pixie-level-output')
  const csrfToken = String(root.dataset.csrfToken || '')

  let payload = { auditoriums: [], devices: [], scenes: [] }
  let activeTab = 'devices'
  let activeAuditoriumId = ''
  let selectedIds = new Set()
  let selectionOrder = []
  let dragging = false
  let pollTimer = null
  let stateRequestVersion = 0
  let stateAbortController = null
  let sliderTimer = null
  let pendingLevel = null
  let sliderWriteInFlight = false
  let sliderWriteQueued = false
  let sliderFinalRequested = false
  let sliderDrainWaiters = []
  // A status request may have started before a level command. Keep that older
  // response from repainting the fader while the command is being committed.
  let levelDisplayOverride = null
  let levelDisplayOverrideVersion = 0

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char])

  const showMessage = (message, kind = 'danger') => {
    if (!message) {
      alertBox.innerHTML = ''
      return
    }
    alertBox.innerHTML = `<div class="alert alert-${kind}" role="alert">${escapeHtml(message)}</div>`
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
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`)
    return data
  }

  const activeAuditorium = () => (payload.auditoriums || []).find((item) => item.id === activeAuditoriumId)
  const deviceById = (id) => (payload.devices || []).find((item) => item.id === id)
  const rememberedAuditorium = () => {
    try { return String(localStorage.getItem('tdeck:pixie:auditorium') || '') } catch (_error) { return '' }
  }

  const stopPolling = () => {
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
  }

  const shouldPoll = () => activeTab === 'devices' && !!activeAuditoriumId && !document.hidden

  const schedulePoll = () => {
    stopPolling()
    if (!shouldPoll()) return
    pollTimer = setTimeout(async () => {
      try {
        await loadState({ refresh: true, quiet: true })
      } finally {
        schedulePoll()
      }
    }, 1000)
  }

  const setTab = (tab) => {
    activeTab = tab === 'scenes' ? 'scenes' : 'devices'
    document.querySelectorAll('[data-pixie-tab]').forEach((button) => {
      const selected = button.dataset.pixieTab === activeTab
      button.classList.toggle('active', selected)
      button.setAttribute('aria-selected', selected ? 'true' : 'false')
    })
    document.getElementById('pixie-devices-view').classList.toggle('d-none', activeTab !== 'devices')
    document.getElementById('pixie-scenes-view').classList.toggle('d-none', activeTab !== 'scenes')
    renderScenes()
    schedulePoll()
  }

  const auditoriumTile = (auditorium) => `
    <button type="button" class="pixie-auditorium-tile" data-auditorium-id="${escapeHtml(auditorium.id)}">
      <div class="pixie-auditorium-kicker">Auditorium</div>
      <div class="pixie-auditorium-main">
        <div class="pixie-auditorium-icon" aria-hidden="true"><span></span></div>
        <div class="flex-grow-1 min-w-0">
        <div class="pixie-tile-name">${escapeHtml(auditorium.name)}</div>
        <div class="pixie-tile-state"><span>${auditorium.deviceCount} device${auditorium.deviceCount === 1 ? '' : 's'}</span><span>${rememberedAuditorium() === auditorium.id ? 'Last used' : 'Open &rarr;'}</span></div>
        </div>
      </div>
    </button>`

  const renderAuditoriumChooser = () => {
    const auditoriums = payload.auditoriums || []
    auditoriumTiles.innerHTML = auditoriums.map(auditoriumTile).join('')
    chooser.classList.toggle('d-none', !!activeAuditoriumId || auditoriums.length === 0)
    controls.classList.toggle('d-none', !activeAuditoriumId)
    backButton.classList.toggle('d-none', auditoriums.length <= 1)
  }

  const resetAuditoriumDisplay = ({ loading = false } = {}) => {
    const auditorium = activeAuditorium()
    auditoriumTitle.textContent = auditorium ? auditorium.name : ''
    auditoriumStatus.textContent = loading ? 'Loading current device state…' : ''
    deviceTiles.innerHTML = loading
      ? '<div class="pixie-loading-card grid-column-all" role="status"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Loading devices…</span></div>'
      : ''
    selectionPanel.classList.add('d-none')
  }

  const deviceStateText = (device) => {
    if (device.online === false) return 'Offline'
    if (device.controlType === 'on_off') return device.on ? 'On' : 'Off'
    if (device.brightness === null || device.brightness === undefined) return device.on ? 'On' : 'Unknown'
    return `${Math.round(Number(device.brightness))}%`
  }

  const deviceTile = (device) => {
    const checked = selectedIds.has(device.id)
    const disabled = !!device.disabled
    const availabilityText = device.online === true ? 'Online' : (device.online === false ? 'Offline' : 'Status unknown')
    const availabilityClass = device.online === true ? '' : (device.online === false ? 'offline' : 'unknown')
    return `<label class="pixie-tile pixie-device-tile ${checked ? 'pixie-tile-selected' : ''} ${disabled ? 'pixie-tile-disabled' : ''} ${device.on ? 'pixie-device-on' : ''}" data-device-id="${escapeHtml(device.id)}">
      <input class="form-check-input pixie-device-checkbox" type="checkbox" value="${escapeHtml(device.id)}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} aria-label="Select ${escapeHtml(device.name)}">
      ${disabled ? '<span class="pixie-disabled-mark" aria-hidden="true">&times;</span>' : ''}
      <div class="pixie-device-icon" aria-hidden="true"></div>
      <div>
        <div class="pixie-tile-name">${escapeHtml(device.name)}</div>
        <div class="pixie-tile-state">
          <span><span class="pixie-online-dot ${availabilityClass}"></span>${availabilityText}</span>
          <strong>${escapeHtml(deviceStateText(device))}</strong>
        </div>
      </div>
    </label>`
  }

  const selectedDevices = () => selectionOrder
    .filter((id) => selectedIds.has(id))
    .map(deviceById)
    .filter(Boolean)

  const currentFaderDevice = () => [...selectedDevices()].reverse().find((device) => device.controlType === 'dimmer')

  const setDisplayedLevel = (value) => {
    const level = Math.max(0, Math.min(100, Math.round(Number(value))))
    levelRange.value = String(level)
    levelOutput.value = `${level}%`
    levelOutput.textContent = `${level}%`
    return level
  }

  const holdDisplayedLevel = (value) => {
    const level = setDisplayedLevel(value)
    const version = ++levelDisplayOverrideVersion
    levelDisplayOverride = { level, version }
    return version
  }

  const clearDisplayedLevelHold = () => {
    levelDisplayOverride = null
    levelDisplayOverrideVersion += 1
  }

  const releaseDisplayedLevel = async (version) => {
    if (!levelDisplayOverride || levelDisplayOverride.version !== version) return
    await loadState({ refresh: true, quiet: true })
    if (!levelDisplayOverride || levelDisplayOverride.version !== version) return
    levelDisplayOverride = null
    updateSelectionControls()
  }

  const updateSelectionControls = ({ preserveLevel = false } = {}) => {
    const devices = selectedDevices()
    const hasDimmer = devices.some((device) => device.controlType === 'dimmer')
    selectionPanel.classList.toggle('d-none', devices.length === 0)
    selectionCount.textContent = `${devices.length} device${devices.length === 1 ? '' : 's'} selected`
    faderControls.classList.toggle('d-none', !hasDimmer)
    switchControls.classList.toggle('d-none', hasDimmer)
    levelOutput.classList.toggle('d-none', !hasDimmer)
    selectionHelp.textContent = hasDimmer && devices.some((device) => device.controlType === 'on_off')
      ? 'On/Off devices change only at 0% or 100%.'
      : ''
    const enabled = !!payload.controlReady
    levelRange.disabled = !enabled
    document.querySelectorAll('[data-pixie-level], [data-pixie-switch]').forEach((button) => { button.disabled = !enabled })
    if (hasDimmer && !dragging && !preserveLevel) {
      const device = currentFaderDevice()
      const feedbackLevel = device && device.brightness != null ? device.brightness : 0
      setDisplayedLevel(levelDisplayOverride ? levelDisplayOverride.level : feedbackLevel)
    }
  }

  const renderDevices = () => {
    const auditorium = activeAuditorium()
    if (!auditorium) return
    const currentIds = new Set((payload.devices || []).map((item) => item.id))
    selectedIds = new Set([...selectedIds].filter((id) => currentIds.has(id) && !deviceById(id).disabled))
    selectionOrder = selectionOrder.filter((id) => selectedIds.has(id))
    auditoriumTitle.textContent = auditorium.name
    const reachabilityNote = !payload.reachabilityAvailable
      ? ' · availability unavailable'
      : (payload.reachabilityStale ? ' · availability may be delayed' : '')
    auditoriumStatus.textContent = payload.controlReady
      ? `${payload.devices.length} accessible device${payload.devices.length === 1 ? '' : 's'}${reachabilityNote}`
      : (payload.mode === 'observe' ? 'Observe only — controls are disabled' : (payload.lastError || 'Controls are unavailable'))
    deviceTiles.innerHTML = (payload.devices || []).map(deviceTile).join('') || '<div class="alert alert-info grid-column-all">No accessible devices are configured in this auditorium.</div>'
    updateSelectionControls({ preserveLevel: dragging })
  }

  const renderScenes = () => {
    const scenes = payload.scenes || []
    sceneTiles.innerHTML = scenes.map((scene) => `
      <button type="button" class="pixie-tile" data-scene-id="${escapeHtml(scene.id)}" ${!payload.controlReady || !scene.available ? 'disabled' : ''}>
        <div class="pixie-device-icon" aria-hidden="true"></div>
        <div>
          <div class="pixie-tile-name">${escapeHtml(scene.name)}</div>
          <div class="pixie-tile-state"><span>${scene.available ? 'Scene' : 'Unavailable'}</span><span>Activate</span></div>
        </div>
      </button>`).join('') || '<div class="alert alert-info">No scenes are assigned to you.</div>'
  }

  const chooseAuditorium = async (auditoriumId, { remember = true } = {}) => {
    stopPolling()
    if (stateAbortController) stateAbortController.abort()
    activeAuditoriumId = String(auditoriumId || '')
    selectedIds.clear()
    selectionOrder = []
    clearDisplayedLevelHold()
    if (remember && activeAuditoriumId) {
      try { localStorage.setItem('tdeck:pixie:auditorium', activeAuditoriumId) } catch (_error) {}
    }
    resetAuditoriumDisplay({ loading: !!activeAuditoriumId })
    renderAuditoriumChooser()
    if (activeAuditoriumId) await loadState({ refresh: true })
    schedulePoll()
  }

  const initialNavigation = async () => {
    const auditoriums = payload.auditoriums || []
    const scenes = payload.scenes || []
    noAccess.classList.toggle('d-none', auditoriums.length > 0 || scenes.length > 0)
    if (!auditoriums.length && scenes.length) {
      setTab('scenes')
      return
    }
    if (!auditoriums.length) return
    const target = auditoriums.length === 1 ? auditoriums[0].id : ''
    if (target) await chooseAuditorium(target, { remember: false })
    else renderAuditoriumChooser()
  }

  async function loadState({ refresh = false, quiet = false } = {}) {
    const requestedAuditoriumId = activeAuditoriumId
    const requestVersion = ++stateRequestVersion
    if (stateAbortController) stateAbortController.abort()
    const abortController = new AbortController()
    stateAbortController = abortController
    const query = new URLSearchParams()
    if (requestedAuditoriumId) query.set('auditorium_id', requestedAuditoriumId)
    if (refresh) query.set('refresh', '1')
    try {
      const next = await request(`/api/pixie/state?${query.toString()}`, { signal: abortController.signal })
      if (requestVersion !== stateRequestVersion || requestedAuditoriumId !== activeAuditoriumId) return null
      payload = next
      if (!quiet) showMessage('')
      renderAuditoriumChooser()
      if (activeAuditoriumId) renderDevices()
      renderScenes()
      return next
    } catch (error) {
      if (error && error.name === 'AbortError') return null
      if (requestVersion !== stateRequestVersion || requestedAuditoriumId !== activeAuditoriumId) return null
      if (!quiet) showMessage(error.message || String(error))
      if (!quiet && activeAuditoriumId) {
        auditoriumStatus.textContent = 'Device state could not be loaded'
        deviceTiles.innerHTML = ''
      }
      return null
    } finally {
      if (stateAbortController === abortController) stateAbortController = null
    }
  }

  const sendLevel = async (level, final) => {
    if (!activeAuditoriumId || !selectedIds.size) return
    const body = {
      auditorium_id: activeAuditoriumId,
      device_ids: [...selectedIds],
      level: Math.max(0, Math.min(100, Math.round(Number(level)))),
      final: !!final,
    }
    try {
      const result = await request('/api/pixie/devices/brightness', { method: 'POST', body: JSON.stringify(body) })
      if (result.failed && result.failed.length) {
        showMessage(`Some devices could not be updated: ${result.failed.map((item) => item.error).join('; ')}`, 'warning')
      } else {
        showMessage('')
      }
    } catch (error) {
      showMessage(error.message || String(error))
    }
  }

  const flushSliderWrite = async (final = false) => {
    if (final) sliderFinalRequested = true
    if (pendingLevel === null) return
    if (sliderWriteInFlight) {
      sliderWriteQueued = true
      return
    }
    const level = pendingLevel
    const sendAsFinal = sliderFinalRequested
    pendingLevel = null
    sliderFinalRequested = false
    sliderWriteInFlight = true
    try {
      await sendLevel(level, sendAsFinal)
    } finally {
      sliderWriteInFlight = false
      if (sliderWriteQueued || pendingLevel !== null) {
        sliderWriteQueued = false
        await flushSliderWrite(false)
      }
      if (!sliderTimer && !sliderWriteInFlight && !sliderWriteQueued && pendingLevel === null) {
        const waiters = sliderDrainWaiters
        sliderDrainWaiters = []
        waiters.forEach((resolve) => resolve())
      }
    }
  }

  const waitForSliderWrites = () => {
    if (!sliderTimer && !sliderWriteInFlight && !sliderWriteQueued && pendingLevel === null) {
      return Promise.resolve()
    }
    return new Promise((resolve) => { sliderDrainWaiters.push(resolve) })
  }

  const queueSliderWrite = (level) => {
    pendingLevel = level
    if (sliderTimer) return
    sliderTimer = setTimeout(() => {
      sliderTimer = null
      flushSliderWrite(false)
    }, 80)
  }

  const commitLevel = async (value) => {
    const level = Math.max(0, Math.min(100, Math.round(Number(value))))
    const displayVersion = holdDisplayedLevel(level)
    if (sliderTimer) clearTimeout(sliderTimer)
    sliderTimer = null
    pendingLevel = level
    await flushSliderWrite(true)
    await waitForSliderWrites()
    await releaseDisplayedLevel(displayVersion)
  }

  document.querySelectorAll('[data-pixie-tab]').forEach((button) => {
    button.addEventListener('click', () => setTab(button.dataset.pixieTab))
  })

  auditoriumTiles.addEventListener('click', (event) => {
    const tile = event.target.closest('[data-auditorium-id]')
    if (tile) chooseAuditorium(tile.dataset.auditoriumId)
  })

  backButton.addEventListener('click', () => {
    if (stateAbortController) stateAbortController.abort()
    stateRequestVersion += 1
    activeAuditoriumId = ''
    selectedIds.clear()
    selectionOrder = []
    clearDisplayedLevelHold()
    resetAuditoriumDisplay()
    renderAuditoriumChooser()
    stopPolling()
  })

  deviceTiles.addEventListener('change', (event) => {
    const checkbox = event.target.closest('input[type="checkbox"][value]')
    if (!checkbox) return
    clearDisplayedLevelHold()
    const id = String(checkbox.value)
    if (checkbox.checked) {
      selectedIds.add(id)
      selectionOrder = selectionOrder.filter((value) => value !== id)
      selectionOrder.push(id)
    } else {
      selectedIds.delete(id)
      selectionOrder = selectionOrder.filter((value) => value !== id)
    }
    renderDevices()
  })

  document.getElementById('pixie-select-all').addEventListener('click', () => {
    clearDisplayedLevelHold()
    selectedIds = new Set((payload.devices || []).filter((item) => !item.disabled).map((item) => item.id))
    selectionOrder = [...selectedIds]
    renderDevices()
  })

  document.getElementById('pixie-clear-selection').addEventListener('click', () => {
    clearDisplayedLevelHold()
    selectedIds.clear()
    selectionOrder = []
    renderDevices()
  })

  levelRange.addEventListener('pointerdown', () => { dragging = true })
  levelRange.addEventListener('pointerup', () => {
    if (pendingLevel === null) dragging = false
  })
  levelRange.addEventListener('pointercancel', () => { dragging = false })
  levelRange.addEventListener('blur', () => { dragging = false })
  levelRange.addEventListener('touchstart', () => { dragging = true }, { passive: true })
  levelRange.addEventListener('input', () => {
    dragging = true
    const level = Math.round(Number(levelRange.value))
    levelOutput.value = `${level}%`
    levelOutput.textContent = `${level}%`
    queueSliderWrite(level)
  })
  levelRange.addEventListener('change', async () => {
    dragging = false
    await commitLevel(levelRange.value)
  })

  document.querySelectorAll('[data-pixie-level]').forEach((button) => {
    button.addEventListener('click', async () => {
      await commitLevel(button.dataset.pixieLevel)
    })
  })

  document.querySelectorAll('[data-pixie-switch]').forEach((button) => {
    button.addEventListener('click', () => sendLevel(button.dataset.pixieSwitch === 'on' ? 100 : 0, true))
  })

  sceneTiles.addEventListener('click', async (event) => {
    const tile = event.target.closest('[data-scene-id]')
    if (!tile || tile.disabled) return
    tile.disabled = true
    try {
      await request(`/api/pixie/scenes/${encodeURIComponent(tile.dataset.sceneId)}/activate`, {
        method: 'POST', body: JSON.stringify({}),
      })
      showMessage('')
    } catch (error) {
      showMessage(error.message || String(error))
    } finally {
      renderScenes()
    }
  })

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && shouldPoll()) loadState({ refresh: true, quiet: true }).finally(schedulePoll)
    else schedulePoll()
  })

  loadState().then(initialNavigation).finally(schedulePoll)
})()
