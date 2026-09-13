function initializeRoutingPresets() {
  const root = document.getElementById('routing-presets-page');
  if (!root) return;
  const el = id => document.getElementById(id);
  const query = new URLSearchParams(window.location.search);
  const storageKey = 'tdeck.routing.preset-job';
  let presets = [], confirmation = null, busy = false, timer = null, polling = false, jobId = '', active = true;
  const modalElement = el('preset-confirm-modal');
  // Keep the modal above the backdrop outside the page's stacking context.
  document.body.appendChild(modalElement);
  const modal = new bootstrap.Modal(modalElement);
  function message(copy, error) {
    el('preset-message').className = copy ? 'alert alert-' + (error ? 'danger' : 'info') : '';
    el('preset-message').textContent = copy || '';
  }
  async function request(path, options) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), 15000) : null;
    try {
      const response = await fetch(path, Object.assign({cache: 'no-store', credentials: 'same-origin', signal: controller ? controller.signal : undefined}, options || {}));
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) { const error = new Error(data.message || data.error || 'Unable to load presets. Please try again.'); error.status = response.status; throw error; }
      return data;
    } finally { if (timeout) clearTimeout(timeout); }
  }
  function post(path, data) { return request(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)}); }
  function controls() {
    root.querySelectorAll('.media-tile').forEach(button => { button.disabled = busy; });
    el('preset-confirm-apply').disabled = busy || !confirmation;
  }
  function render() {
    const grid = el('preset-grid'); grid.textContent = '';
    const search = el('preset-search').value.trim().toLowerCase();
    presets.filter(item => item.name.toLowerCase().indexOf(search) !== -1).forEach(item => {
      const tile = document.createElement('button'); tile.type = 'button'; tile.className = 'media-tile';
      tile.setAttribute('aria-label', 'Select ' + item.name);
      const image = document.createElement('img'); image.src = item.thumbnail_url; image.alt = ''; image.className = 'media-tile-image'; image.loading = 'lazy';
      const name = document.createElement('span'); name.className = 'media-tile-caption'; name.textContent = item.name;
      tile.appendChild(image); tile.appendChild(name); tile.addEventListener('click', () => choose(item)); grid.appendChild(tile);
    });
    grid.setAttribute('aria-busy', 'false'); controls();
  }
  async function choose(item, output) {
    if (busy) return;
    if (item.output === null && !output) { window.location.assign('/routing?preset=' + encodeURIComponent(item.id)); return; }
    busy = true; controls(); message(''); confirmation = null;
    try {
      confirmation = await post('/api/routing/presets/' + item.id + '/prepare', {revision: item.revision, output: item.output === null ? output : item.output});
      el('preset-confirm-copy').textContent = 'Apply “' + item.name + '” to ' + confirmation.output_label + '?';
      el('preset-confirm-description').textContent = item.description;
      el('preset-confirm-image').src = item.thumbnail_url;
      el('preset-confirm-actions').textContent = '';
      confirmation.actions.forEach(label => { const li = document.createElement('li'); li.textContent = label; el('preset-confirm-actions').appendChild(li); });
      modal.show();
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }
  function remember() { try { if (jobId) sessionStorage.setItem(storageKey, jobId); else sessionStorage.removeItem(storageKey); } catch (_) {} }
  function progress(job) {
    el('preset-progress').classList.remove('d-none'); el('preset-progress').textContent = job.message;
    if (job.status === 'succeeded') {
      const finishedId = jobId; jobId = ''; remember();
      window.location.assign('/routing?preset_job=' + encodeURIComponent(finishedId));
    } else if (job.status === 'failed') {
      jobId = ''; busy = false; remember(); controls();
      el('preset-progress').classList.add('border-danger');
    } else { busy = true; controls(); schedule(); }
  }
  function schedule() { if (timer) clearTimeout(timer); timer = null; if (jobId && active && !document.hidden) timer = setTimeout(poll, 1000); }
  async function poll() {
    if (polling || !jobId || !active || document.hidden) return;
    polling = true;
    try { const data = await request('/api/routing/presets/jobs/' + jobId); progress(data.job); }
    catch (error) {
      if (error.status === 403 || error.status === 404) { message(error.message, true); jobId = ''; busy = false; remember(); controls(); }
      else { message('Connection interrupted. Checking the preset again…'); schedule(); }
    } finally { polling = false; }
  }
  async function apply() {
    if (!confirmation || busy) return;
    busy = true; jobId = confirmation.execution_id; remember(); controls(); modal.hide();
    el('preset-progress').classList.remove('d-none', 'border-danger'); el('preset-progress').textContent = 'Applying preset…';
    try { const data = await post('/api/routing/presets/' + confirmation.preset.id + '/apply', {confirmation_token: confirmation.confirmation_token}); progress(data.job); }
    catch (error) {
      message(error.message, true);
      if (error.status && error.status < 500) { jobId = ''; busy = false; remember(); controls(); }
      else schedule(); // Check this execution; never replay a POST automatically.
    }
  }
  async function load(initial) {
    try {
      const data = await request('/api/routing/presets'); presets = data.presets; render();
      if (!presets.length) message('No presets are available for your access.');
      if (initial && query.get('selected') && !jobId) {
        const selected = presets.find(item => item.id === query.get('selected'));
        if (selected) choose(selected, Number(query.get('output')) || null);
        else message('This preset is no longer available for your access.', true);
      }
    } catch (error) { message(error.message, true); el('preset-grid').setAttribute('aria-busy', 'false'); }
  }
  el('preset-search').addEventListener('input', render);
  el('preset-refresh').addEventListener('click', () => { message(''); load(false); if (jobId) poll(); });
  el('preset-confirm-apply').addEventListener('click', apply);
  document.addEventListener('visibilitychange', schedule);
  window.addEventListener('pagehide', () => { active = false; if (timer) clearTimeout(timer); });
  window.addEventListener('pageshow', () => { active = true; schedule(); });
  try { jobId = sessionStorage.getItem(storageKey) || ''; } catch (_) {}
  if (!/^[a-f0-9]{32}$/.test(jobId)) jobId = '';
  busy = !!jobId; load(true); if (jobId) poll();
}
initializeRoutingPresets();
