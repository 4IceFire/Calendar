/* Shared running order. Serialized revision-checked saves; never replay triggers. */
(function () {
  'use strict';
  function initPropsPage() {
    const root = document.getElementById('props-page');
    if (!root) return;
    const configure = root.dataset.propsView === 'configure';
    const error = document.getElementById('props-error');
    const status = document.getElementById('props-status');
    let state = null, busy = false, generation = 0, timer = null, signature = '';
    const clone = value => JSON.parse(JSON.stringify(value));
    function newLibraryId() {
      const bytes = new Uint8Array(16); window.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
      return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20);
    }
    function showError(message) { error.textContent = message; error.hidden = !message; }
    function setBusy(value) {
      busy = value;
      root.setAttribute('aria-busy', String(value));
      const picker = document.getElementById('props-picker');
      if (value && picker && !document.getElementById('props-picker-backdrop').hidden) picker.focus();
      root.querySelectorAll('button,input,select').forEach(el => { el.disabled = value || el.dataset.unavailable === '1'; });
    }
    async function api(path, method, value) {
      const response = await fetch('/api/props' + path, {method: method || 'GET', headers: {'Content-Type':'application/json'},
        ...(value === undefined ? {} : {body: JSON.stringify(value)})});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Props request failed');
      return data;
    }
    function catalogStatus() {
      const c = state.catalog;
      status.textContent = c.lastError ? 'Catalog unavailable: ' + c.lastError + '. Last successful catalog retained; refresh before triggering.' :
        c.refreshing ? 'Refreshing catalog…' : c.stale ? 'Catalog stale or not loaded. Refresh before triggering.' : 'Catalog ready. Changes save automatically.';
      const last = document.getElementById('props-last-triggered');
      if (last) last.textContent = state.last_triggered ? 'Last successfully triggered: ' + state.last_triggered.position + '. ' + state.last_triggered.name + ' (not active visibility feedback).' : 'No successfully triggered preset yet. This is not active visibility feedback.';
    }
    function plainLibrary() {
      return state.library.map(item => ({id:item.id, name:item.name, prop_uuid:item.prop_uuid, macro_uuid:item.macro_uuid}));
    }
    async function mutate(path, value, preserveTree) {
      if (busy) return;
      generation++; setBusy(true); showError('');
      try {
        state = await api(path, path === '/catalog/refresh' || path === '/trigger' ? 'POST' : 'PUT', value);
        if (path === '/trigger') state = await api('');
        if (preserveTree) { catalogStatus(); signature = ''; }
        else render(true);
        return true;
      } catch (e) {
        showError(e.message || 'Outcome unknown. Reload; do not automatically retry.');
        // Re-read safely after uncertain writes. Never replay a mutation.
        try { state = await api(''); render(true); } catch (_) { /* retain last valid view */ }
        if (preserveTree) throw e;
      } finally { setBusy(false); }
    }
    function button(label, action, disabled) {
      const el = document.createElement('button'); el.type = 'button'; el.className = 'btn btn-sm btn-outline-secondary';
      el.textContent = label; el.disabled = !!disabled; el.dataset.unavailable = disabled ? '1' : '0';
      el.addEventListener('click', action); return el;
    }
    function choices(select, entries, chosen, empty) {
      select.textContent = '';
      if (empty !== undefined) select.appendChild(new Option(empty, ''));
      entries.forEach(item => select.appendChild(new Option(item.name, item.uuid || item.id)));
      const match = chosen && entries.find(item => (item.uuid || item.id).toLowerCase() === chosen.toLowerCase());
      if (chosen && !match) select.appendChild(new Option('Unavailable saved target', chosen));
      if (chosen) select.value = match ? (match.uuid || match.id) : chosen;
    }
    function presetChoices(select, chosen) {
      const search = select.id === 'props-selector' ? document.getElementById('props-search').value.toLowerCase() : '';
      select.textContent = '';
      if (select.id === 'props-selector') select.appendChild(new Option('Choose a preset', ''));
      [null].concat(state.organization.folders).forEach(folder => {
        const group = document.createElement('optgroup'); group.label = folder ? folder.name : 'No folder';
        state.organization.items.filter(item => (item.folderId || null) === (folder ? folder.id : null)).forEach(item => {
          const entry = state.library.find(row => row.id === item.id);
          if (entry && (entry.name + ' ' + group.label).toLowerCase().includes(search)) group.appendChild(new Option(entry.name, entry.id));
        });
        if (group.children.length) select.appendChild(group);
      });
      if (chosen && state.library.some(item => item.id === chosen)) select.value = chosen;
    }
    function renderOrder() {
      const order = document.getElementById('props-order'); order.textContent = '';
      if (!state.presets.length) {
        const row = document.createElement('tr'), cell = document.createElement('td');
        cell.colSpan = 4; cell.textContent = 'No presets in the running order.'; row.appendChild(cell); order.appendChild(row);
      }
      state.presets.forEach((preset, index) => {
        const row = document.createElement('tr'); row.dataset.propsSlot = String(index + 1);
        const triggerCell = document.createElement('td'); triggerCell.className = 'props-trigger-cell';
        const trigger = button('▶', () => mutate('/trigger', {position:preset.position, revision:state.revision, use_macros:state.use_macros}), !preset.available);
        trigger.className = 'btn timer-apply-btn'; trigger.title = 'Trigger preset ' + preset.position;
        trigger.setAttribute('aria-label', trigger.title); triggerCell.appendChild(trigger); row.appendChild(triggerCell);
        const titleCell = document.createElement('td'); titleCell.className = 'props-name-cell';
        const title = document.createElement('strong'); title.textContent = preset.position + '. ' + preset.name; titleCell.appendChild(title);
        if (!preset.available) { const note = document.createElement('small'); note.className = 'd-block text-muted'; note.textContent = preset.unavailable_reason; titleCell.appendChild(note); }
        row.appendChild(titleCell);
        const mappingCell = document.createElement('td'); mappingCell.className = 'props-mapping-cell';
        const selector = document.createElement('select'); selector.className = 'form-select form-select-sm'; selector.setAttribute('aria-label', 'Change preset ' + preset.position);
        presetChoices(selector, preset.id);
        selector.addEventListener('change', () => { const list = state.order.slice(); list[index] = selector.value; saveOrder(list); });
        mappingCell.appendChild(selector); row.appendChild(mappingCell);
        const actionsCell = document.createElement('td'); actionsCell.className = 'props-actions-cell';
        const controls = document.createElement('div'); controls.className = 'd-flex align-items-start justify-content-end gap-2';
        const arrows = document.createElement('div'); arrows.className = 'd-flex flex-column align-items-center gap-1';
        [['▲', -1, 'up', index === 0], ['▼', 1, 'down', index === state.order.length - 1]].forEach(([label, step, direction, disabled]) => {
          const arrow = button(label, () => move(index, step), disabled);
          arrow.className = 'btn btn-sm btn-outline-secondary me-1 btn-icon-sm';
          arrow.setAttribute('aria-label', 'Move preset ' + preset.position + ' ' + direction);
          arrows.appendChild(arrow);
        });
        controls.appendChild(arrows);
        const remove = button('Delete', () => saveOrder(state.order.filter((_, i) => i !== index)));
        remove.className = 'btn btn-sm btn-outline-danger'; remove.setAttribute('aria-label', 'Delete preset ' + preset.position);
        controls.appendChild(remove); actionsCell.appendChild(controls); row.appendChild(actionsCell);
        order.appendChild(row);
      });
      const add = document.getElementById('props-add');
      add.dataset.unavailable = state.library.length ? '0' : '1';
      add.disabled = busy || !state.library.length;
      document.getElementById('props-empty-library').hidden = !!state.library.length;
      const selector = document.getElementById('props-selector'); presetChoices(selector, selector.value);
    }
    function saveOrder(order) { return mutate('/order', {revision:state.revision, order}); }
    function move(index, step) { const order = state.order.slice(); const other = order[index + step]; order[index + step] = order[index]; order[index] = other; saveOrder(order); }
    function filterCatalog(kind) {
      const select = document.getElementById('props-' + kind), search = document.getElementById('props-' + kind + '-search').value.toLowerCase();
      choices(select, state.catalog[kind === 'prop' ? 'props' : 'macros'].filter(item => item.name.toLowerCase().includes(search)), select.value, kind === 'macro' ? 'No macro' : 'Choose a prop');
    }
    function renderConfigure() {
      document.getElementById('props-macros').checked = state.use_macros;
      filterCatalog('prop'); filterCatalog('macro');
      const tree = document.getElementById('props-library');
      if (tree._catalogMoving && tree._catalogMoving()) {
        // The widget rolls back optimistic changes before invoking this hook.
        tree._catalogRenderAfterMove = () => render(true);
        return;
      }
      const rows = state.library.map(item => {
        const row = document.createElement('div'); row.className = 'props-row';
        const name = document.createElement('input'); name.className = 'form-control'; name.maxLength = 120; name.value = item.name;
        name.setAttribute('aria-label', 'Friendly name for ' + item.name);
        function update(key, value) { const library = plainLibrary(); library.find(entry => entry.id === item.id)[key] = value; mutate('/library', {revision:state.revision, library, organization:state.organization}); }
        name.addEventListener('change', () => update('name', name.value)); row.appendChild(name);
        const title = document.createElement('span'); title.textContent = item.name; title.className = 'visually-hidden'; row.appendChild(title);
        ['prop', 'macro'].forEach(kind => {
          const select = document.createElement('select'); select.className = 'form-select'; select.setAttribute('aria-label', (kind === 'prop' ? 'ProPresenter prop for ' : 'Macro for ') + item.name);
          choices(select, state.catalog[kind === 'prop' ? 'props' : 'macros'], item[kind + '_uuid'], kind === 'macro' ? 'No macro' : undefined);
          select.addEventListener('change', () => update(kind + '_uuid', select.value || null)); row.appendChild(select);
        });
        row.appendChild(button('Delete mapping', () => {
          const library = plainLibrary().filter(entry => entry.id !== item.id);
          const organization = clone(state.organization); organization.items = organization.items.filter(entry => entry.id !== item.id);
          mutate('/library', {revision:state.revision, library, organization});
        }, state.order.includes(item.id)));
        if (state.order.includes(item.id)) { const note = document.createElement('small'); note.textContent = 'Used in running order; remove presets before deleting.'; row.appendChild(note); }
        return {id:item.id, label:item.name, node:row};
      });
      window.renderCatalogOrganization(tree, state.organization, rows, {editable:true,
        save: async organization => { await mutate('/library', {revision:state.revision, library:plainLibrary(), organization}, true); },
        error: showError});
    }
    function render(force) {
      catalogStatus();
      const next = JSON.stringify([state.revision, state.use_macros, state.library, state.catalog.props, state.catalog.macros]);
      if (!force && next === signature) return;
      signature = next;
      if (configure) renderConfigure(); else renderOrder();
    }
    async function refresh() {
      if (!busy && !document.hidden && !root.contains(document.activeElement) && !(configure && document.getElementById('props-library')._catalogMoving && document.getElementById('props-library')._catalogMoving())) {
        const current = generation;
        try { const next = await api(''); if (current === generation && !busy) { state = next; render(false); } }
        catch (e) { showError(e.message); }
      }
      timer = setTimeout(refresh, 2000);
    }
    if (configure) {
      document.getElementById('props-refresh').addEventListener('click', () => mutate('/catalog/refresh'));
      document.getElementById('props-macros').addEventListener('change', event => mutate('/settings', {use_macros:event.target.checked, previous_use_macros:state.use_macros, revision:state.revision}));
      ['prop', 'macro'].forEach(kind => document.getElementById('props-' + kind + '-search').addEventListener('input', () => { if (state) filterCatalog(kind); }));
      document.getElementById('props-mapping-form').addEventListener('submit', event => {
        event.preventDefault(); if (!state || busy) return;
        const target = document.getElementById('props-prop').value;
        if (!target) return;
        const id = newLibraryId();
        const library = plainLibrary(); library.push({id, name:document.getElementById('props-name').value, prop_uuid:target, macro_uuid:document.getElementById('props-macro').value || null});
        const organization = clone(state.organization); organization.items.push({id, folderId:null});
        mutate('/library', {revision:state.revision, library, organization});
      });
    } else {
      const backdrop = document.getElementById('props-picker-backdrop');
      const picker = document.getElementById('props-picker');
      const add = document.getElementById('props-add');
      const search = document.getElementById('props-search');
      const selector = document.getElementById('props-selector');
      let background = [];
      function isolateBackground() {
        // Isolate siblings at every level; never hide an ancestor of the picker.
        for (let node = backdrop; node !== document.body; node = node.parentElement) {
          Array.from(node.parentElement.children).filter(el => el !== node).forEach(el => {
            background.push({el, hidden:el.getAttribute('aria-hidden'), inert:el.getAttribute('inert')});
            el.setAttribute('aria-hidden', 'true');
            if ('inert' in el) el.inert = true;
          });
        }
      }
      function closePicker() {
        if (busy) return;
        backdrop.hidden = true;
        background.forEach(({el, hidden, inert}) => {
          if (hidden === null) el.removeAttribute('aria-hidden'); else el.setAttribute('aria-hidden', hidden);
          if (inert === null) el.removeAttribute('inert'); else el.setAttribute('inert', inert);
        });
        background = []; add.focus();
      }
      // Capture guards also protect browsers without native inert support.
      document.addEventListener('focusin', event => {
        if (!backdrop.hidden && !picker.contains(event.target)) (busy ? picker : search).focus();
      }, true);
      ['pointerdown', 'mousedown', 'touchstart', 'click', 'auxclick'].forEach(type => {
        document.addEventListener(type, event => {
          if (!backdrop.hidden && !picker.contains(event.target)) {
            event.preventDefault(); event.stopImmediatePropagation();
          }
        }, {capture:true, passive:false});
      });
      add.addEventListener('click', () => {
        if (!state || busy) return;
        search.value = ''; presetChoices(selector, '');
        const pickerError = document.getElementById('props-picker-error'); pickerError.textContent = ''; pickerError.hidden = true;
        backdrop.hidden = false; search.focus(); isolateBackground();
      });
      search.addEventListener('input', () => { if (state) presetChoices(selector, ''); });
      selector.addEventListener('change', async () => {
        const id = selector.value;
        if (!state || busy || !id) return;
        if (await saveOrder(state.order.concat([id]))) closePicker();
        else {
          presetChoices(selector, '');
          const pickerError = document.getElementById('props-picker-error');
          pickerError.textContent = error.textContent + ' Nothing was retried. Review the current order before choosing again.';
          pickerError.hidden = false; selector.focus();
        }
      });
      document.getElementById('props-picker-cancel').addEventListener('click', closePicker);
      document.addEventListener('keydown', event => {
        if (backdrop.hidden) return;
        if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closePicker(); }
        if (event.key === 'Tab') {
          const fields = Array.from(picker.querySelectorAll('input,select,button')).filter(el => !el.disabled);
          const first = fields[0], last = fields[fields.length - 1];
          if (!first) { event.preventDefault(); picker.focus(); }
          else if (!fields.includes(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
          else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
        if (!picker.contains(event.target)) { event.preventDefault(); event.stopImmediatePropagation(); }
      }, true);
    }
    setBusy(true);
    api('').then(async value => {
      state = value;
      render(true);
      if (configure) {
        try { state = await api('/catalog/refresh', 'POST'); render(true); }
        catch (e) { showError(e.message); } // A failed refresh must not discard readable state.
      }
    }).catch(e => { showError(e.message); })
      .then(() => { setBusy(false); refresh(); });
    window.addEventListener('pagehide', () => { clearTimeout(timer); generation++; });
  }
  initPropsPage();
}());
