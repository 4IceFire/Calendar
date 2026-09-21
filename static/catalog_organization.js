/* Shared group/preset folders: pointer and keyboard moves retain saved IDs. */
(function () {
  'use strict';
  window.renderCatalogOrganization = function (root, organization, rows, options) {
    if (root._catalogDispose) root._catalogDispose();
    let tree = JSON.parse(JSON.stringify(organization || {folders: [], items: []}));
    const collapsed = root._catalogCollapsed || (root._catalogCollapsed = {});
    const byId = {}, labels = {};
    let busy = false, drag = null, frame = null, disposed = false;
    root._catalogMoving = () => busy || !!drag;
    function flushRender() {
      if (!busy && !drag && root._catalogRenderAfterMove) {
        const redraw = root._catalogRenderAfterMove; root._catalogRenderAfterMove = null; redraw();
      }
    }
    rows.forEach(row => { byId[String(row.id)] = row.node; labels[String(row.id)] = row.label || String(row.id); });
    root.classList.add('catalog-tree');
    const live = document.createElement('div');
    live.className = 'visually-hidden'; live.setAttribute('role', 'status'); live.setAttribute('aria-live', 'polite');
    const hintId = root.id + '-move-help';
    function announce(text) { live.textContent = text; }
    function button(label, title, action) {
      const el = document.createElement('button'); el.type = 'button'; el.className = 'btn btn-sm btn-outline-secondary';
      el.dataset.catalogControl = '1'; el.textContent = label; el.title = title; el.setAttribute('aria-label', title);
      el.addEventListener('click', event => { event.preventDefault(); if (!busy && !drag) action(); });
      return el;
    }
    function positions() {
      const result = {};
      root.querySelectorAll('[data-catalog-item]').forEach(el => { result[el.dataset.catalogItem] = el.getBoundingClientRect(); });
      return result;
    }
    function focusItem(id) {
      const row = Array.from(root.querySelectorAll('[data-catalog-item]')).find(el => el.dataset.catalogItem === String(id));
      if (row) row.querySelector('.catalog-handle').focus({preventScroll: true});
    }
    async function change(action, focusId) {
      if (busy || disposed) return;
      const before = JSON.parse(JSON.stringify(tree)), oldPositions = positions();
      action();
      if (JSON.stringify(before) === JSON.stringify(tree)) { announce('Position unchanged.'); return; }
      busy = true; render(oldPositions); // Move immediately; save only once on drop.
      announce('Saving organisation…');
      // The server normalizes order fields; keep these objects so row/header
      // handlers retain their identity without rebuilding the list on success.
      try { await options.save(tree); }
      catch (error) {
        if (disposed) return;
        const previousPositions = positions(); tree = before; busy = false; render(previousPositions);
        if (focusId) focusItem(focusId);
        options.error(error.message || 'Could not save organisation'); announce('Move could not be saved. Previous position restored.');
        flushRender();
        return;
      }
      if (disposed) return;
      busy = false;
      root.querySelectorAll('[data-catalog-control]').forEach(el => { el.disabled = false; });
      root.setAttribute('aria-busy', 'false');
      if (focusId) focusItem(focusId);
      announce('Organisation saved.');
      flushRender();
    }
    function moveFolder(folder, step) {
      const index = tree.folders.indexOf(folder), other = tree.folders[index + step];
      if (other) { tree.folders[index] = other; tree.folders[index + step] = folder; }
    }
    function moveItem(id, target) {
      const item = tree.items.find(entry => String(entry.id) === id);
      const rest = tree.items.filter(entry => entry !== item);
      item.folderId = target.folderId;
      let index = target.before ? rest.findIndex(entry => String(entry.id) === target.before) : -1;
      if (index < 0) {
        const siblings = rest.filter(entry => (entry.folderId || null) === target.folderId);
        index = siblings.length ? rest.indexOf(siblings[siblings.length - 1]) + 1 : rest.length;
      }
      rest.splice(index, 0, item); tree.items = rest;
      if (target.folderId) collapsed[target.folderId] = false;
    }
    function targets(id) {
      const result = [];
      [null].concat(tree.folders.map(folder => folder.id)).forEach(folderId => {
        tree.items.filter(item => String(item.id) !== id && byId[String(item.id)] && (item.folderId || null) === folderId)
          .forEach(item => result.push({folderId, before: String(item.id)}));
        result.push({folderId, before: null});
      });
      return result;
    }
    function sectionFor(folderId) {
      return Array.from(root.querySelectorAll('[data-catalog-folder]')).find(el => el.dataset.catalogFolder === (folderId || ''));
    }
    function clearIndicator() {
      root.querySelectorAll('.catalog-drop-target').forEach(el => el.classList.remove('catalog-drop-target'));
      if (drag) drag.line.hidden = true;
    }
    function showTarget(target, speak) {
      clearIndicator(); drag.target = target;
      if (!target) return;
      const section = sectionFor(target.folderId);
      if (!section) return;
      const header = section.querySelector('.catalog-header'); header.classList.add('catalog-drop-target');
      const content = section.querySelector('.catalog-items');
      const next = Array.from(content.children).find(el => el.dataset.catalogItem === target.before);
      const rect = (next || content).getBoundingClientRect();
      const line = drag.line;
      line.hidden = content.hidden; line.style.left = rect.left + 'px';
      line.style.top = (next ? rect.top : rect.bottom) + 'px'; line.style.width = rect.width + 'px';
      if (speak) {
        const folder = tree.folders.find(entry => entry.id === target.folderId);
        announce((folder ? folder.name : 'No folder') + (target.before ? ', before ' + labels[target.before] : ', at end') + '. Enter to drop, Escape to cancel.');
        header.scrollIntoView({block: 'nearest'});
      }
    }
    function start(id, handle, keyboard) {
      const origin = handle.closest('[data-catalog-item]');
      const line = document.createElement('div'); line.className = 'catalog-drop-line'; line.hidden = true;
      document.body.appendChild(line);
      drag = {id, handle, origin, line, keyboard, target: null, active: keyboard, x: 0, y: 0};
      handle.setAttribute('aria-pressed', 'true');
      if (keyboard) {
        origin.classList.add('catalog-drag-origin');
        const available = targets(id), item = tree.items.find(entry => String(entry.id) === id);
        const siblings = tree.items.filter(entry => (entry.folderId || null) === (item.folderId || null));
        const next = siblings[siblings.indexOf(item) + 1];
        drag.index = Math.max(0, available.findIndex(target => target.folderId === (item.folderId || null) && target.before === (next ? String(next.id) : null)));
        showTarget(available[drag.index], true);
      }
    }
    function finish(commit) {
      if (!drag) return;
      const current = drag; clearIndicator(); drag = null;
      if (frame) cancelAnimationFrame(frame); frame = null;
      current.line.remove(); if (current.ghost) current.ghost.remove();
      current.origin.classList.remove('catalog-drag-origin'); current.handle.setAttribute('aria-pressed', 'false');
      document.body.classList.remove('catalog-dragging');
      document.documentElement.classList.remove('catalog-dragging');
      if (commit && current.active && current.target) change(() => moveItem(current.id, current.target), current.id);
      else { announce('Move cancelled.'); focusItem(current.id); }
      flushRender();
    }
    function locate(x, y) {
      const hit = document.elementFromPoint(x, y);
      const section = hit && hit.closest('[data-catalog-folder]');
      if (!section || !root.contains(section)) return null;
      const folderId = section.dataset.catalogFolder || null;
      if (hit.closest('.catalog-header')) return {folderId, before: null};
      const siblings = Array.from(section.querySelector('.catalog-items').children)
        .filter(el => el.dataset.catalogItem && el.dataset.catalogItem !== drag.id);
      const next = siblings.find(el => { const r = el.getBoundingClientRect(); return y < r.top + r.height / 2; });
      return {folderId, before: next ? next.dataset.catalogItem : null};
    }
    function animatePointer() {
      if (!drag || !drag.active || drag.keyboard) return;
      const {x, y} = drag;
      drag.ghost.style.transform = 'translate3d(' + Math.max(4, Math.min(x + 14, window.innerWidth - drag.ghost.offsetWidth - 4)) + 'px,' + (y + 14) + 'px,0)';
      const bounds = root.getBoundingClientRect();
      const scrollable = root.scrollHeight > root.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(root).overflowY);
      const top = scrollable ? Math.max(0, bounds.top) : 0;
      const bottom = scrollable ? Math.min(window.innerHeight, bounds.bottom) : window.innerHeight;
      const speed = y < top + 48 ? -Math.min(14, (top + 48 - y) / 3) : (y > bottom - 48 ? Math.min(14, (y - bottom + 48) / 3) : 0);
      if (speed && x >= bounds.left && x <= bounds.right) {
        if (scrollable) root.scrollTop += speed;
        else window.scrollBy(0, speed);
      }
      showTarget(locate(x, y), false); frame = requestAnimationFrame(animatePointer);
    }
    function pointerMove(event) {
      if (!drag || drag.keyboard || event.pointerId !== drag.pointerId) return;
      drag.x = event.clientX; drag.y = event.clientY;
      if (!drag.active && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) >= 6) {
        drag.active = true; drag.origin.classList.add('catalog-drag-origin'); document.body.classList.add('catalog-dragging');
        document.documentElement.classList.add('catalog-dragging');
        const ghost = document.createElement('div'); ghost.className = 'catalog-drag-ghost'; ghost.textContent = labels[drag.id]; ghost.setAttribute('aria-hidden', 'true');
        document.body.appendChild(ghost); drag.ghost = ghost; animatePointer();
      }
      if (drag.active) event.preventDefault();
    }
    function pointerUp(event) {
      if (drag && !drag.keyboard && event.pointerId === drag.pointerId) {
        if (drag.active) showTarget(locate(event.clientX, event.clientY), false);
        finish(event.type === 'pointerup');
      }
    }
    function keyDown(event) {
      if (!drag) return;
      if (event.key === 'Escape') { event.preventDefault(); finish(false); return; }
      if (!drag.keyboard) return;
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); finish(true); }
      else if (/^Arrow(Up|Down|Left|Right)$/.test(event.key)) {
        event.preventDefault(); const available = targets(drag.id);
        drag.index = Math.max(0, Math.min(available.length - 1, drag.index + (/Up|Left/.test(event.key) ? -1 : 1)));
        showTarget(available[drag.index], true);
      } else if (event.key === 'Tab') finish(false);
    }
    function renderItems(parent, folderId) {
      tree.items.filter(item => (item.folderId || null) === folderId && byId[String(item.id)]).forEach(item => {
        const id = String(item.id), wrap = document.createElement('div'); wrap.className = 'catalog-item'; wrap.dataset.catalogItem = id;
        if (options.editable) {
          const handle = document.createElement('button'); handle.type = 'button'; handle.className = 'catalog-handle';
          handle.textContent = '⠿'; handle.title = 'Drag to move ' + labels[id]; handle.dataset.catalogControl = '1';
          handle.setAttribute('aria-label', 'Move ' + labels[id]); handle.setAttribute('aria-describedby', hintId); handle.setAttribute('aria-pressed', 'false');
          handle.addEventListener('pointerdown', event => {
            if (busy || drag || event.button !== 0 || event.isPrimary === false) return;
            event.preventDefault(); handle.focus({preventScroll: true}); start(id, handle, false);
            Object.assign(drag, {pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY});
            handle.setPointerCapture(event.pointerId);
          });
          handle.addEventListener('keydown', event => {
            if (!busy && !drag && (event.key === ' ' || event.key === 'Enter')) {
              event.preventDefault(); event.stopPropagation(); start(id, handle, true);
            }
          });
          wrap.appendChild(handle);
        }
        const content = document.createElement('div'); content.className = 'catalog-item-content'; content.appendChild(byId[id]); wrap.appendChild(content); parent.appendChild(wrap);
      });
    }
    function render(oldPositions) {
      const scrollTop = root.scrollTop;
      root.textContent = ''; root.setAttribute('aria-busy', String(busy));
      if (options.editable) {
        const toolbar = document.createElement('div'); toolbar.className = 'catalog-toolbar';
        toolbar.appendChild(button('New folder', 'New folder', () => {
          const name = window.prompt('Folder name');
          if (name && name.trim()) change(() => tree.folders.push({id: 'folder-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2), name: name.trim()}));
        }));
        const hint = document.createElement('span'); hint.className = 'catalog-help'; hint.textContent = 'Drag ⠿ to move'; toolbar.appendChild(hint); root.appendChild(toolbar);
      }
      const help = document.createElement('span'); help.id = hintId; help.className = 'visually-hidden';
      help.textContent = 'Drag to a folder or between rows. Or press Space, use arrow keys to choose a position, then Enter to drop. Escape cancels.';
      root.appendChild(help); root.appendChild(live);
      [null].concat(tree.folders).forEach(folder => {
        const folderId = folder ? folder.id : null;
        const section = document.createElement('section'); section.className = 'catalog-section'; section.dataset.catalogFolder = folderId || '';
        const header = document.createElement('div'); header.className = 'catalog-header';
        if (folder) {
          const toggle = button((collapsed[folderId] ? '▸ ' : '▾ ') + folder.name, 'Expand or collapse ' + folder.name, () => {
            collapsed[folderId] = !collapsed[folderId]; render();
          });
          toggle.setAttribute('aria-expanded', String(!collapsed[folderId])); header.appendChild(toggle);
          if (options.editable) {
            header.appendChild(button('↑', 'Move folder up', () => change(() => moveFolder(folder, -1))));
            header.appendChild(button('↓', 'Move folder down', () => change(() => moveFolder(folder, 1))));
            header.appendChild(button('Rename', 'Rename folder', () => {
              const name = window.prompt('Folder name', folder.name);
              if (name && name.trim()) change(() => { folder.name = name.trim(); });
            }));
            header.appendChild(button('Delete', 'Delete folder', () => {
              if (window.confirm('Delete this folder? Its items will move to No folder.')) change(() => {
                tree.items.forEach(item => { if (item.folderId === folderId) item.folderId = null; });
                tree.folders = tree.folders.filter(value => value !== folder);
              });
            }));
          }
        } else { const title = document.createElement('span'); title.className = 'small text-muted'; title.textContent = 'No folder'; header.appendChild(title); }
        section.appendChild(header);
        const content = document.createElement('div'); content.className = 'catalog-items'; content.hidden = !!collapsed[folderId];
        renderItems(content, folderId); section.appendChild(content); root.appendChild(section);
      });
      root.scrollTop = scrollTop;
      root.querySelectorAll('[data-catalog-control]').forEach(el => { el.disabled = busy; });
      if (oldPositions && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        root.querySelectorAll('[data-catalog-item]').forEach(el => {
          const before = oldPositions[el.dataset.catalogItem], after = el.getBoundingClientRect();
          if (before && before.height && after.height && typeof el.animate === 'function') {
            el.animate([{transform: 'translate(' + (before.left - after.left) + 'px,' + (before.top - after.top) + 'px)'}, {transform: 'translate(0,0)'}], {duration: 180, easing: 'ease-out'});
          }
        });
      }
    }
    function cancel() { finish(false); }
    document.addEventListener('pointermove', pointerMove, {passive: false});
    document.addEventListener('pointerup', pointerUp); document.addEventListener('pointercancel', pointerUp);
    document.addEventListener('keydown', keyDown); window.addEventListener('blur', cancel);
    root._catalogDispose = function () {
      root._catalogRenderAfterMove = null;
      finish(false); disposed = true;
      root._catalogMoving = null;
      document.removeEventListener('pointermove', pointerMove); document.removeEventListener('pointerup', pointerUp);
      document.removeEventListener('pointercancel', pointerUp); document.removeEventListener('keydown', keyDown); window.removeEventListener('blur', cancel);
    };
    render();
  };
}());
