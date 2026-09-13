function initializeMediaPage() {
  const root = document.getElementById('media-page');
  if (!root) return;
  const el = function(id) { return document.getElementById(id); };
  const output = Number(root.dataset.output) || null;
  const canDisplay = root.dataset.canDisplay === 'true' && !!output;
  const uploadPage = root.dataset.uploadPage === 'true';
  const libraryPath = '/media' + (output ? '?output=' + output : '');
  const storageKey = 'tdeck.media.display.' + (output || 'library');
  let items = [];
  let chosen = null;
  let uploaded = false;
  let busy = false;
  let jobId = '';
  let pollTimer = null;
  let pollInFlight = false;
  let pollDelay = 1000;
  let pageActive = true;
  let retryChecksJob = false;
  let previewUrl = '';
  let libraryInFlight = false;
  let modal = null;

  function text(node, value) { if (node) node.textContent = value || ''; }
  function show(node, visible) { if (node) node.classList.toggle('d-none', !visible); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function message(value, kind) {
    const box = el('media-message');
    box.className = 'alert alert-' + (kind || 'danger') + ' mb-3';
    text(box, value);
  }
  function errorMessage(error) { return error && error.message ? error.message : 'Please try again.'; }
  function saveProgress() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({item: chosen, uploaded: uploaded, jobId: jobId})); } catch (_error) {}
  }
  function clearProgress() { try { sessionStorage.removeItem(storageKey); } catch (_error) {} }
  function updateControls() {
    Array.prototype.forEach.call(root.querySelectorAll('.media-return-link'), function(link) {
      link.classList.toggle('disabled', busy);
      link.setAttribute('aria-disabled', busy ? 'true' : 'false');
      link.tabIndex = busy ? -1 : 0;
    });
    Array.prototype.forEach.call(root.querySelectorAll('.media-tile'), function(tile) { tile.disabled = busy; });
    if (el('media-upload-button')) {
      el('media-upload-button').disabled = busy || uploaded;
      el('media-upload-file').disabled = busy || uploaded;
      el('media-upload-name').disabled = busy || uploaded;
      text(el('media-upload-button'), busy ? (jobId ? 'Displaying…' : 'Please wait…') : (uploaded ? 'Image uploaded' : (canDisplay ? 'Upload and display' : 'Upload image')));
    }
  }
  function progress(copy, failed, allowRetry) {
    show(el('media-display-progress'), true);
    text(el('media-progress-name'), chosen ? chosen.name : 'Your image');
    text(el('media-progress-message'), copy);
    el('media-display-progress').classList.toggle('border-danger', !!failed);
    const image = el('media-progress-image');
    const imageUrl = chosen && (chosen.thumbnail_url || chosen.url);
    show(image, !!imageUrl);
    if (imageUrl) { image.src = imageUrl; image.alt = ''; }
    show(el('media-display-retry'), !!allowRetry);
    text(el('media-display-retry'), retryChecksJob ? 'Check again' : 'Try again');
  }
  async function request(path, options) {
    const settings = Object.assign({cache: 'no-store', credentials: 'same-origin'}, options || {});
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timeout = null;
    if (controller) {
      settings.signal = controller.signal;
      timeout = setTimeout(function() { controller.abort(); }, path === '/api/media/upload' ? 60000 : 15000);
    }
    try {
      const response = await fetch(path, settings);
      const payload = await response.json().catch(function() { return {}; });
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.message || payload.error || 'The request could not be completed. Please try again.');
        error.status = response.status;
        throw error;
      }
      if (response.redirected || (response.headers.get('content-type') || '').indexOf('application/json') === -1) throw new Error('Refresh the page to sign in again.');
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The connection timed out. Check the screen before trying again.');
      throw error;
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }
  function stopPoll() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; }
  function schedulePoll() {
    stopPoll();
    if (jobId && pageActive && !document.hidden) pollTimer = setTimeout(pollJob, pollDelay);
  }
  function acceptJob(job) {
    if (!job || !job.id) throw new Error('The display request could not be confirmed. Check the screen before trying again.');
    jobId = job.id;
    if (job.status === 'succeeded') {
      stopPoll();
      clearProgress();
      progress('Image displayed. Returning to outputs…', false, false);
      window.location.assign('/routing?media_job=' + encodeURIComponent(job.id));
      return;
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      stopPoll();
      jobId = '';
      busy = false;
      retryChecksJob = false;
      saveProgress();
      progress(job.message || job.error || 'The image could not be displayed. Please try again.', true, true);
      updateControls();
      return;
    }
    busy = true;
    retryChecksJob = false;
    saveProgress();
    progress(job.message || 'Displaying your image…', false, false);
    updateControls();
    schedulePoll();
  }
  async function pollJob() {
    if (!jobId || pollInFlight || document.hidden || !pageActive) return;
    stopPoll();
    pollInFlight = true;
    const expectedId = jobId;
    try {
      const data = await request('/api/media/display/' + encodeURIComponent(expectedId));
      if (jobId !== expectedId || !pageActive) return;
      pollDelay = 1000;
      acceptJob(data.job);
    } catch (error) {
      if (jobId !== expectedId || !pageActive) return;
      retryChecksJob = true;
      pollDelay = Math.min(pollDelay * 2, 15000);
      if (error.status === 403 || error.status === 404) {
        busy = false;
        progress(errorMessage(error), true, true);
        updateControls();
      } else {
        progress('Connection interrupted. Checking the display again…', false, true);
        schedulePoll();
      }
    } finally { pollInFlight = false; }
  }
  async function displayImage(item) {
    if (busy || !canDisplay) return;
    chosen = item;
    busy = true;
    jobId = '';
    retryChecksJob = false;
    saveProgress();
    show(el('media-message'), false);
    progress('Displaying your image…', false, false);
    updateControls();
    try {
      const data = await request('/api/media/display', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({media_id: item.id, output: output})});
      acceptJob(data.job);
    } catch (error) {
      busy = false;
      progress(errorMessage(error), true, true);
      updateControls();
    }
  }
  function previewImage(item) {
    text(el('media-preview-title'), item.name);
    el('media-preview-image').src = item.url;
    el('media-preview-image').alt = item.name;
    const box = el('media-preview-modal');
    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
      if (!modal) modal = new bootstrap.Modal(box);
      modal.show();
    } else {
      box.classList.add('show');
      box.style.display = 'block';
      box.setAttribute('aria-hidden', 'false');
      box.setAttribute('aria-modal', 'true');
      el('media-preview-close').focus();
    }
  }
  function closePreview() {
    const box = el('media-preview-modal');
    if (modal) modal.hide();
    else if (box) { box.classList.remove('show'); box.style.display = 'none'; box.setAttribute('aria-hidden', 'true'); box.removeAttribute('aria-modal'); }
  }
  function renderLibrary() {
    const grid = el('media-grid');
    const query = el('media-search').value.trim().toLowerCase();
    const presetsOnly = el('media-filter').value === 'presets';
    const visible = items.filter(function(item) { return (!presetsOnly || item.preset) && String(item.name || '').toLowerCase().indexOf(query) !== -1; });
    clear(grid);
    text(el('media-library-status'), visible.length ? (canDisplay ? 'Select an image to display it.' : 'Select an image to preview it.') : (items.length ? 'No images match your search.' : 'No images have been added yet.'));
    visible.forEach(function(item) {
      const tile = document.createElement('button');
      tile.type = 'button';
      tile.className = 'media-tile';
      tile.dataset.mediaId = item.id;
      tile.setAttribute('aria-label', (canDisplay ? 'Display ' : 'Preview ') + item.name + (item.preset ? ', preset' : ''));
      const image = document.createElement('img');
      image.className = 'media-tile-image'; image.src = item.thumbnail_url; image.alt = ''; image.loading = 'lazy';
      const caption = document.createElement('span');
      caption.className = 'media-tile-caption';
      const name = document.createElement('span'); name.className = 'media-tile-name'; name.textContent = item.name;
      caption.appendChild(name);
      if (item.preset) { const badge = document.createElement('span'); badge.className = 'media-tile-preset'; badge.textContent = 'Preset'; caption.appendChild(badge); }
      tile.appendChild(image); tile.appendChild(caption);
      tile.addEventListener('click', function() { if (!busy) { if (canDisplay) { uploaded = false; displayImage(item); } else previewImage(item); } });
      grid.appendChild(tile);
    });
    grid.setAttribute('aria-busy', 'false');
    updateControls();
  }
  async function refreshLibrary() {
    if (libraryInFlight) return;
    libraryInFlight = true;
    show(el('media-library-retry'), false);
    try { const data = await request('/api/media'); items = data.items || []; renderLibrary(); }
    catch (error) { text(el('media-library-status'), 'Could not load images. ' + errorMessage(error)); show(el('media-library-retry'), true); }
    finally { libraryInFlight = false; el('media-grid').setAttribute('aria-busy', 'false'); }
  }
  function changeUploadPreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = '';
    const file = el('media-upload-file').files[0];
    show(el('media-upload-preview'), !!file);
    if (file && typeof URL.createObjectURL === 'function') { previewUrl = URL.createObjectURL(file); el('media-upload-preview-image').src = previewUrl; }
    else show(el('media-upload-preview'), false);
  }
  async function uploadImage(event) {
    event.preventDefault();
    if (busy || uploaded) return;
    const file = el('media-upload-file').files[0];
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { message('Choose an image smaller than 20 MB.'); return; }
    const form = new FormData();
    form.append('file', file); form.append('name', el('media-upload-name').value.trim());
    busy = true;
    show(el('media-message'), false);
    progress('Uploading your image…', false, false);
    updateControls();
    try {
      const data = await request('/api/media/upload', {method: 'POST', body: form});
      if (!data.item || !data.item.id) throw new Error('The upload could not be confirmed. Check the image library before uploading again.');
      chosen = data.item;
      uploaded = true;
      busy = false;
      saveProgress();
      if (canDisplay) await displayImage(chosen);
      else { clearProgress(); window.location.assign(libraryPath); }
    } catch (error) { busy = false; progress(errorMessage(error), true, false); updateControls(); }
  }
  function restoreProgress() {
    if (!canDisplay) return;
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!stored || !stored.item || !/^[a-f0-9]{32}$/.test(stored.item.id)) return;
      chosen = stored.item; uploaded = !!stored.uploaded; jobId = typeof stored.jobId === 'string' ? stored.jobId : '';
      if (jobId) { busy = true; progress('Checking your image display…', false, false); pollJob(); }
      else { retryChecksJob = false; progress('Your image is saved. Check the screen, then try again if needed.', false, true); }
    } catch (_error) {}
  }

  el('media-display-retry').addEventListener('click', function() { if (retryChecksJob && jobId) pollJob(); else if (chosen) displayImage(chosen); });
  Array.prototype.forEach.call(root.querySelectorAll('.media-return-link'), function(link) { link.addEventListener('click', function(event) { if (busy) event.preventDefault(); }); });
  if (uploadPage) {
    el('media-upload-form').addEventListener('submit', uploadImage);
    el('media-upload-file').addEventListener('change', changeUploadPreview);
    el('media-upload-preview-image').addEventListener('error', function() { show(el('media-upload-preview'), false); });
    el('media-upload-preview-image').addEventListener('load', function() { show(el('media-upload-preview'), !!el('media-upload-file').files.length); });
    // A restored file input, or a choice made while scripts are loading, may
    // already contain a file before the change listener is attached.
    changeUploadPreview();
  } else {
    el('media-search').addEventListener('input', renderLibrary);
    el('media-filter').addEventListener('change', renderLibrary);
    el('media-library-retry').addEventListener('click', refreshLibrary);
    el('media-preview-close').addEventListener('click', closePreview);
    document.addEventListener('keydown', function(event) { if (event.key === 'Escape') closePreview(); });
    refreshLibrary();
  }
  document.addEventListener('visibilitychange', function() { stopPoll(); if (!document.hidden) pollJob(); });
  window.addEventListener('pagehide', function() { pageActive = false; stopPoll(); if (previewUrl) URL.revokeObjectURL(previewUrl); });
  window.addEventListener('pageshow', function(event) { pageActive = true; if (event.persisted) pollJob(); });
  restoreProgress();
  updateControls();
}

initializeMediaPage();
