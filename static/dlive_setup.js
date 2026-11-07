(function () {
  'use strict';
  const provider = document.getElementById('personal-mix-provider');
  if (!provider) return;
  const byId = id => document.getElementById(id);
  const auxes = byId('dlive-aux-mappings');
  const inputs = byId('dlive-input-mappings');
  let saved = {};
  let nextId = 0;
  function dirty() { window.TDeckMixerItemEditor.dirty(); }

  function toggle() {
    const dlive = provider.value === 'dlive';
    byId('dlive-setup').hidden = !dlive;
    byId('digico-legacy-connection').hidden = dlive;
    for (const kind of ['aux', 'input']) {
      byId(`dlive-${kind}-editor`).hidden = !dlive;
      byId(kind === 'aux' ? 'digico-aux-table' : 'digico-channel-table').hidden = dlive;
    }
    for (const button of document.querySelectorAll('.digico-reset-discovered')) button.hidden = dlive;
    for (const target of ['devices']) {
      const tab = document.querySelector(`[data-bs-target="#digico-tab-${target}"]`);
      if (tab) tab.disabled = dlive;
    }
  }

  function input(row, title, key, value, type) {
    const label = document.createElement('label');
    label.className = 'dlive-mapping-field';
    label.textContent = title;
    const field = document.createElement('input');
    field.className = 'form-control form-control-sm';
    field.dataset.key = key;
    field.type = type || 'text';
    if (field.type === 'checkbox') {
      field.className = 'form-check-input d-block';
      field.checked = value !== false;
    } else {
      field.value = value == null ? '' : String(value);
      if (field.type === 'number') { field.min = '1'; field.max = key === 'bus' ? '62' : '128'; }
      else field.maxLength = 80;
    }
    label.appendChild(field);
    row.appendChild(label);
  }

  function add(target, item, aux) {
    const row = document.createElement('div');
    row.className = 'dlive-mapping-row';
    row.dataset.mapping = 'true';
    row._mixOriginal = item;
    input(row, 'Enabled', 'enabled', item.enabled, 'checkbox');
    input(row, aux ? 'AUX ordinal' : 'Input number', aux ? 'bus' : 'channel', aux ? item.bus : item.channel, 'number');
    if (aux) input(row, 'Stereo', 'stereo', !!item.stereo, 'checkbox');
    input(row, 'Label', 'label', item.label);
    if (aux) input(row, 'Group', 'group', item.group);
    if (aux) input(row, 'Colour', 'colour', item.colour || '#3478f6', 'color');
    const editor = window.TDeckMixerItemEditor;
    const icon = editor.iconControl(item, aux ? 'aux' : 'channel');
    icon.querySelector('input').dataset.key = 'icon';
    row.appendChild(icon);

    const remove = document.createElement('button');
    remove.className = 'btn btn-sm btn-outline-danger';
    remove.textContent = 'Delete';
    remove.type = 'button';
    remove.addEventListener('click', () => { target._mixOrganization.remove(row); });
    row.appendChild(remove);
    const entry = {id: String(item.channel || 'new-' + (++nextId)), node: row,
      label: item.label || (aux ? 'AUX ' + item.bus : 'Input ' + item.channel), group: item.group,
      identity: () => aux ? (row.querySelector('[data-key="stereo"]').checked ? 2000 : 1000) + Number(row.querySelector('[data-key="bus"]').value) : Number(row.querySelector('[data-key="channel"]').value)};
    return entry;
  }

  function collect(target, aux) {
    return Array.from(target.querySelectorAll('[data-mapping]')).map((row, index) => {
      const item = {order: index + 1};
      for (const field of row.querySelectorAll('[data-key]')) {
        item[field.dataset.key] = field.type === 'checkbox' ? field.checked : (field.type === 'number' ? Number(field.value) : field.value.trim());
      }
      if (aux) item.channel = (item.stereo ? 2000 : 1000) + item.bus;
      // Preserve supported presentation fields not edited here.
      const old = row._mixOriginal;
      return {...(old || {}), ...item};
    });
  }

  window.TDeckDLiveSetup = {
    render(config) {
      saved = config;
      provider.value = config.personal_mix_provider || 'digico';
      byId('dlive-enabled').checked = !!config.dlive_enabled;
      byId('dlive-ip').value = config.dlive_ip || '';
      byId('dlive-port').value = config.dlive_port || 51325;
      byId('dlive-midi-channel').value = config.dlive_midi_channel || 1;
      auxes.textContent = '';
      inputs.textContent = '';
      const ordered = items => (items || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
      window.TDeckMixOrganization.attach(auxes, null, ordered(config.dlive_auxes).map(item => add(auxes, item, true)), false, dirty);
      window.TDeckMixOrganization.attach(inputs, config.dlive_channel_organization, ordered(config.dlive_channels).map(item => add(inputs, item, false)), true, dirty);
      toggle();
    },
    collect() {
      return {personal_mix_provider: provider.value, dlive_enabled: byId('dlive-enabled').checked,
        dlive_ip: byId('dlive-ip').value.trim(), dlive_port: Number(byId('dlive-port').value),
        dlive_midi_channel: Number(byId('dlive-midi-channel').value),
        dlive_auxes: collect(auxes, true), dlive_channels: collect(inputs, false),
        dlive_channel_organization: inputs._mixOrganization.organization()};
    },
  };
  provider.addEventListener('change', toggle);
  byId('dlive-add-aux').addEventListener('click', () => auxes._mixOrganization.add(add(auxes, {bus: 1, stereo: false}, true)));
  byId('dlive-add-input').addEventListener('click', () => {
    const used = collect(inputs, false).map(item => item.channel);
    let number = 1; while (used.indexOf(number) >= 0) number += 1;
    // A temporary DOM identity remains independent from editable input numbers.
    const entry = add(inputs, {channel: number}, false); entry.id = 'new-' + (++nextId);
    inputs._mixOrganization.add(entry);
  });
})();
