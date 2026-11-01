function initializeTvRooms() {
  const root = document.getElementById('tv-room-editor'); if (!root) return;
  const view = window.TvRoomView, el = function(id) { return document.getElementById(id); };
  let rooms = [], layouts = {}, images = [], roomId = '', selected = '', busy = false, dirty = false;
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function room() { return rooms.find(function(item) { return item.id === roomId; }); }
  function layout() { if (!layouts[roomId]) layouts[roomId] = {positions: {}}; return layouts[roomId]; }
  function message(text) { el('tv-editor-status').textContent = text; }
  function changed() { dirty = true; message('Unsaved changes.'); }
  function imageUrl(image) { return '/tv-room-images/' + encodeURIComponent(roomId) + '/' + image + '?preview=1'; }
  function controls() {
    el('tv-editor-room').disabled = busy || !rooms.length; el('tv-editor-tools').disabled = busy || !room();
    el('tv-room-save').disabled = busy || !room(); el('tv-position-fields').disabled = busy || !selected;
    root.setAttribute('aria-busy', String(busy));
    root.querySelectorAll('[data-position-tv]').forEach(function(n) { n.disabled = busy; });
  }
  function fields() {
    const tv = room() && room().tvs.find(function(item) { return item.id === selected; });
    el('tv-position-name').textContent = tv ? tv.name : 'Select a TV to place';
    const point = layout().positions[selected] || {x: .5, y: .5};
    el('tv-position-x').value = String(Math.round(point.x * 100)); el('tv-position-y').value = String(Math.round(point.y * 100));
    controls();
  }
  function setPoint(x, y) {
    if (busy || !selected) return;
    layout().positions[selected] = {x: Math.round(Math.max(0, Math.min(1, x)) * 1000) / 1000, y: Math.round(Math.max(0, Math.min(1, y)) * 1000) / 1000};
    changed();
    let button = el('tv-editor-canvas').querySelector('[data-position-tv="' + selected + '"]');
    if (!button) { renderCanvas(); button = el('tv-editor-canvas').querySelector('[data-position-tv="' + selected + '"]'); }
    if (button) view.place(button, layout().positions[selected]);
    fields();
  }
  function pointAt(event, picture) {
    const box = picture.getBoundingClientRect(), style = getComputedStyle(picture);
    const xInset = parseFloat(style.getPropertyValue('--tv-inset-x')) || 80, yInset = parseFloat(style.getPropertyValue('--tv-inset-y')) || 100;
    setPoint((event.clientX - box.left - xInset) / Math.max(1, box.width - xInset * 2), (event.clientY - box.top - yInset) / Math.max(1, box.height - yInset * 2));
  }
  function renderCanvas() {
    const target = el('tv-editor-canvas'); clear(target); if (!room()) return;
    const picture = view.canvas(layout().background ? imageUrl(layout().background) : '', room().name);
    picture.addEventListener('click', function(event) { if (event.target === picture || event.target.tagName === 'IMG' || event.target.classList.contains('tv-picture-caption')) pointAt(event, picture); });
    room().tvs.forEach(function(tv) {
      if (!layout().positions[tv.id]) return;
      const button = view.node('button', '', 'tv-editor-tv tv-positioned btn btn-secondary'); button.type = 'button'; button.dataset.positionTv = tv.id;
      button.appendChild(view.icon('tv')); button.appendChild(view.node('span', tv.name)); button.setAttribute('aria-label', 'Position ' + tv.name); button.setAttribute('aria-pressed', String(selected === tv.id));
      view.place(button, layout().positions[tv.id]);
      function select() { selected = tv.id; fields(); picture.querySelectorAll('button').forEach(function(n) { n.setAttribute('aria-pressed', String(n === button)); }); }
      button.addEventListener('click', select);
      button.addEventListener('keydown', function(event) {
        if (busy || !/^Arrow(Left|Right|Up|Down)$/.test(event.key)) return;
        event.preventDefault(); select(); const p = layout().positions[tv.id], step = event.shiftKey ? .1 : .01;
        setPoint(p.x + (event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0), p.y + (event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0));
      });
      let dragging = false;
      button.addEventListener('pointerdown', function(event) { if (busy || event.button !== 0) return; select(); dragging = true; button.setPointerCapture(event.pointerId); });
      button.addEventListener('pointermove', function(event) { if (dragging && !busy) pointAt(event, picture); });
      button.addEventListener('pointerup', function() { dragging = false; });
      button.addEventListener('pointercancel', function() { dragging = false; });
      picture.appendChild(button);
    });
    target.appendChild(picture); controls();
  }
  function renderImages() {
    clear(el('tv-room-images'));
    images.forEach(function(image, index) {
      const button = view.node('button', '', 'tv-image-choice'); button.type = 'button'; button.setAttribute('aria-label', 'Use room picture ' + (index + 1)); button.setAttribute('aria-pressed', String(layout().background === image));
      const img = document.createElement('img'); img.src = imageUrl(image); img.alt = ''; button.appendChild(img);
      button.addEventListener('click', function() { if (!busy) { layout().background = image; changed(); renderImages(); renderCanvas(); } }); el('tv-room-images').appendChild(button);
    });
  }
  function renderRoom() {
    clear(el('tv-room-members')); if (!room()) return;
    room().tvs.forEach(function(tv) {
      const button = view.node('button', tv.name, 'btn btn-outline-secondary'); button.type = 'button'; button.dataset.placeTv = tv.id;
      button.addEventListener('click', function() { selected = tv.id; fields(); renderCanvas(); }); el('tv-room-members').appendChild(button);
    });
    renderImages(); renderCanvas(); fields();
  }
  async function request(path, options) {
    const response = await fetch(path, Object.assign({credentials: 'same-origin', cache: 'no-store'}, options || {}));
    const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.error || 'Room setup request failed.'); return data;
  }
  el('tv-editor-room').addEventListener('change', function() { roomId = this.value; selected = ''; renderRoom(); });
  el('tv-position-apply').addEventListener('click', function() {
    const x = el('tv-position-x'), y = el('tv-position-y');
    if (x.value === '' || y.value === '' || !x.reportValidity() || !y.reportValidity()) { message('Enter both percentages from 0 to 100.'); return; }
    setPoint(Number(x.value) / 100, Number(y.value) / 100);
  });
  el('tv-position-remove').addEventListener('click', function() { if (!busy && selected) { delete layout().positions[selected]; changed(); renderCanvas(); } });
  el('tv-room-remove-image').addEventListener('click', function() { if (!busy) { delete layout().background; changed(); renderImages(); renderCanvas(); } });
  el('tv-room-upload').addEventListener('click', async function() {
    const file = el('tv-room-file').files[0]; if (!file || busy) { if (!file) message('Choose a picture first.'); return; }
    if (file.size > 10 * 1024 * 1024) { message('Choose a picture up to 10 MB.'); return; }
    const target = roomId, form = new FormData(); form.append('file', file); busy = true; controls(); message('Uploading picture…');
    try {
      const result = await request('/api/config/tv-room-layouts/background', {method: 'POST', body: form});
      images.push(result.image); if (!layouts[target]) layouts[target] = {positions: {}}; layouts[target].background = result.image;
      dirty = true; renderImages(); renderCanvas(); message('Uploaded. Save to use this picture in diagnostics.'); el('tv-room-file').value = '';
    } catch (error) { message(error.message); }
    finally { busy = false; controls(); }
  });
  el('tv-room-save').addEventListener('click', async function() {
    if (busy) return; busy = true; controls(); message('Saving…');
    try { await request('/api/config/tv-room-layouts', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({layouts: layouts})}); dirty = false; message('Saved. No device commands sent.'); }
    catch (error) { message(error.message + ' Changes are still here; review before saving again.'); }
    finally { busy = false; controls(); }
  });
  window.addEventListener('beforeunload', function(event) { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  busy = true; controls();
  request('/api/config/tv-room-layouts').then(function(data) {
    rooms = data.rooms; layouts = data.layouts; images = data.images;
    rooms.forEach(function(item) { const option = view.node('option', item.name); option.value = item.id; el('tv-editor-room').appendChild(option); });
    roomId = rooms.length ? rooms[0].id : ''; busy = false; renderRoom(); controls();
  }).catch(function(error) { message(error.message); busy = false; controls(); });
}
initializeTvRooms();
