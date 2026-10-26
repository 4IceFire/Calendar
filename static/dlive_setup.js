(function () {
  'use strict';
  const provider = document.getElementById('personal-mix-provider');
  if (!provider) return;
  const byId = id => document.getElementById(id);
  const auxes = byId('dlive-aux-mappings');
  const inputs = byId('dlive-input-mappings');
  let saved = {};

  function toggle() {
    const dlive = provider.value === 'dlive';
    byId('dlive-setup').hidden = !dlive;
    byId('digico-legacy-connection').hidden = dlive;
    for (const target of ['auxes', 'channels', 'devices']) {
      const tab = document.querySelector(`[data-bs-target="#digico-tab-${target}"]`);
      if (tab) tab.disabled = dlive;
    }
  }

  function input(row, title, key, value, type) {
    const label = document.createElement('label');
    label.className = 'col-sm';
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
    row.className = 'row g-2 align-items-end border rounded p-2 mx-0';
    row.dataset.mapping = 'true';
    input(row, 'Enabled', 'enabled', item.enabled, 'checkbox');
    input(row, aux ? 'AUX ordinal' : 'Input number', aux ? 'bus' : 'channel', aux ? item.bus : item.channel, 'number');
    if (aux) input(row, 'Stereo', 'stereo', !!item.stereo, 'checkbox');
    input(row, 'Label', 'label', item.label);
    if (!aux) input(row, 'Section heading', 'group', item.group);
    const remove = document.createElement('button');
    remove.className = 'btn btn-sm btn-outline-danger col-sm-auto';
    remove.textContent = 'Delete';
    remove.type = 'button';
    remove.addEventListener('click', () => row.remove());
    row.appendChild(remove);
    target.appendChild(row);
  }

  function collect(target, aux) {
    return Array.from(target.querySelectorAll('[data-mapping]')).map((row, index) => {
      const item = {order: index + 1};
      for (const field of row.querySelectorAll('[data-key]')) {
        item[field.dataset.key] = field.type === 'checkbox' ? field.checked : (field.type === 'number' ? Number(field.value) : field.value.trim());
      }
      if (aux) item.channel = (item.stereo ? 2000 : 1000) + item.bus;
      // Preserve supported presentation fields not edited here.
      const old = (saved[aux ? 'dlive_auxes' : 'dlive_channels'] || []).find(old => old.channel === item.channel);
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
      for (const item of config.dlive_auxes || []) add(auxes, item, true);
      for (const item of config.dlive_channels || []) add(inputs, item, false);
      toggle();
    },
    collect() {
      return {personal_mix_provider: provider.value, dlive_enabled: byId('dlive-enabled').checked,
        dlive_ip: byId('dlive-ip').value.trim(), dlive_port: Number(byId('dlive-port').value),
        dlive_midi_channel: Number(byId('dlive-midi-channel').value),
        dlive_auxes: collect(auxes, true), dlive_channels: collect(inputs, false)};
    },
  };
  provider.addEventListener('change', toggle);
  byId('dlive-add-aux').addEventListener('click', () => add(auxes, {bus: 1, stereo: false}, true));
  byId('dlive-add-input').addEventListener('click', () => add(inputs, {channel: inputs.children.length + 1}, false));
})();
