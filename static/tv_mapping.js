function initializeTvMapping() {
  const root = document.getElementById('tv-mapping-setup');
  if (!root) return;
  const rows = document.getElementById('tv-mapping-rows');
  const status = document.getElementById('tv-mapping-status');
  const save = document.getElementById('tv-mapping-save');
  let loaded = false;
  async function request(options) {
    const response = await fetch('/api/config/tv-media-mappings', options || {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save mappings. Check access and field values.');
    return data;
  }
  request().then(function(data) {
    if (data.warning) status.textContent = data.warning;
    data.rooms.forEach(function(room) { room.tvs.forEach(function(tv) {
      const row = document.createElement('div'); row.className = 'row g-2 align-items-end mb-3'; row.dataset.tv = tv.id;
      const title = document.createElement('div'); title.className = 'col-12 col-md-4'; title.textContent = room.name + ' · ' + tv.name; row.appendChild(title);
      [['output', 'VideoHub output (blank = unmapped)', 'number'], ['source', 'TV feed input ID', 'text']].forEach(function(field) {
        const box = document.createElement('div'); box.className = 'col-12 col-md-4';
        const label = document.createElement('label'); label.className = 'form-label'; label.textContent = field[1];
        const input = document.createElement('input'); input.className = 'form-control'; input.type = field[2]; input.id = 'tv-map-' + tv.id + '-' + field[0]; label.htmlFor = input.id;
        input.dataset.field = field[0]; input.value = data.mappings[tv.id] ? data.mappings[tv.id][field[0]] : '';
        if (field[0] === 'output') { input.min = '1'; input.max = '65535'; } else input.placeholder = 'For example HDMI1';
        box.appendChild(label); box.appendChild(input); row.appendChild(box);
      }); rows.appendChild(row);
    }); }); loaded = true;
  }).catch(function(error) { status.textContent = error.message; });
  save.addEventListener('click', async function() {
    if (!loaded) return;
    const mappings = {};
    rows.querySelectorAll('[data-tv]').forEach(function(row) {
      const output = row.querySelector('[data-field="output"]').value;
      const source = row.querySelector('[data-field="source"]').value.trim();
      if (output || source) mappings[row.dataset.tv] = {output: Number(output), source: source};
    });
    save.disabled = true; status.textContent = 'Saving…';
    try { await request({method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mappings: mappings})}); status.textContent = 'Saved. No device commands sent.'; }
    catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  });
}
initializeTvMapping();
