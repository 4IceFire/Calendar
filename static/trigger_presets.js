/* Shared preset action controls for Calendar and Trigger Templates. */
(function () {
  let catalogPromise;
  function catalog() {
    if (!catalogPromise) catalogPromise = fetch('/api/templates/preset-actions', {cache: 'no-store'})
      .then(function (response) { if (!response.ok) throw new Error('Preset choices unavailable'); return response.json(); });
    return catalogPromise;
  }
  function option(select, value, label) {
    const item = document.createElement('option');
    item.value = String(value); item.textContent = label; select.appendChild(item);
  }
  window.triggerPresetFields = function (row) {
    const area = document.createElement('div');
    area.className = 'mt-2 gap-2 align-items-center flex-wrap preset-action-fields';
    area.style.display = 'none';
    area.innerHTML = '<select name="savedPreset" aria-label="Saved preset" class="form-select form-select-sm" style="flex:1;min-width:180px"></select>' +
      '<select name="presetOutput" aria-label="Preset destination" class="form-select form-select-sm" style="flex:1;min-width:180px"></select>' +
      '<small class="text-muted preset-action-note w-100"></small>';
    row.querySelector('.trigger-body').appendChild(area);
    const select = area.querySelector('[name="savedPreset"]');
    const output = area.querySelector('[name="presetOutput"]');
    const note = area.querySelector('small');
    let data = null, previousType = null, saved = {};
    function destination() {
      const routing = previousType === 'routing_preset';
      const found = data && (data.routing || []).find(function (item) { return String(item.id) === select.value; });
      output.style.display = routing ? '' : 'none';
      output.disabled = !routing || !!(found && found.output);
      if (found && found.output) output.value = String(found.output);
      note.textContent = routing ? 'Runs the saved image and actions. If all players are busy, the least recently used alternative is replaced; screens sharing it will also change.' : '';
    }
    function populate() {
      const chosen = saved.preset || select.value;
      select.textContent = ''; option(select, '', '(select saved preset)');
      const items = data ? (previousType === 'routing_preset' ? data.routing : data.videohub) || [] : [];
      items.forEach(function (item) { option(select, item.id, item.name || 'Preset ' + item.id); });
      if (chosen && !items.some(function (item) { return String(item.id) === String(chosen); })) option(select, chosen, 'Unavailable preset: ' + chosen);
      select.value = String(chosen || '');
      const target = saved.output || output.value;
      output.textContent = ''; option(output, '', '(select output)');
      (data ? data.outputs || [] : []).forEach(function (item) { option(output, item.number, item.label || 'Output ' + item.number); });
      if (target && !Array.from(output.options).some(function (item) { return item.value === String(target); })) option(output, target, 'Output ' + target);
      output.value = String(target || '');
      destination();
    }
    row.__syncPresetFields = function (type) {
      const active = type === 'videohub_preset' || type === 'routing_preset';
      area.style.display = active ? 'flex' : 'none';
      if (!active) return;
      if (type !== previousType) { previousType = type; select.value = ''; saved = {}; populate(); }
      ['.companion-fields', '.api-fields', '.timer-fields'].forEach(function (selector) { row.querySelector(selector).style.display = 'none'; });
    };
    row.__restorePreset = function (value) { saved = value || {}; populate(); };
    row.__readPreset = function () {
      if (!select.value) throw new Error('Choose a saved preset');
      const result = {preset: previousType === 'videohub_preset' ? Number(select.value) : select.value};
      if (previousType === 'routing_preset') {
        if (!output.value) throw new Error('Choose an output for the routing preset');
        result.output = Number(output.value);
      }
      return result;
    };
    select.addEventListener('change', function () { saved = {}; destination(); });
    output.addEventListener('change', function () { saved = {}; });
    catalog().then(function (value) { data = value; populate(); }).catch(function () { note.textContent = 'Preset choices could not load. Refresh before saving.'; });
  };
})();
