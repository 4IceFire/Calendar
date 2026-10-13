(function () {
  'use strict';

  function _initApiTokensPage() {
    var root = document.getElementById('api-token-page');
    if (!root) return;

    var createForm = document.getElementById('api-token-create-form');
    var createButton = document.getElementById('api-token-create');
    var refreshButton = document.getElementById('api-token-refresh');
    var rowsElement = document.getElementById('api-token-rows');
    var loadingElement = document.getElementById('api-token-loading');
    var emptyElement = document.getElementById('api-token-empty');
    var tableWrap = document.getElementById('api-token-table-wrap');
    var alertElement = document.getElementById('api-token-alert');
    var rotateModalElement = document.getElementById('api-token-rotate-modal');
    var rotateForm = document.getElementById('api-token-rotate-form');
    var rotateButton = document.getElementById('api-token-rotate-confirm');
    var secretModalElement = document.getElementById('api-token-secret-modal');
    var secretInput = document.getElementById('api-token-secret');
    var secretMeta = document.getElementById('api-token-secret-meta');
    var copyButton = document.getElementById('api-token-copy');
    var rotateModal = window.bootstrap.Modal.getOrCreateInstance(rotateModalElement);
    var secretModal = window.bootstrap.Modal.getOrCreateInstance(secretModalElement);
    var tokensById = {};
    var editElement = document.getElementById('api-token-edit-modal');
    var editForm = document.getElementById('api-token-edit-form');
    var editModal = window.bootstrap.Modal.getOrCreateInstance(editElement);
    var editToken = null;
    var editSaving = false;

    function clearElement(element) {
      while (element.firstChild) element.removeChild(element.firstChild);
    }

    function setAlert(message, kind) {
      clearElement(alertElement);
      if (!message) return;
      var box = document.createElement('div');
      box.className = 'alert alert-' + (kind || 'danger');
      box.setAttribute('role', 'alert');
      box.textContent = message;
      alertElement.appendChild(box);
    }

    function formatDate(value) {
      if (!value) return 'Never';
      var date = new Date(value);
      if (isNaN(date.getTime())) return String(value);
      try {
        return new Intl.DateTimeFormat(undefined, {
          dateStyle: 'medium',
          timeStyle: 'short'
        }).format(date);
      } catch (error) {
        return date.toLocaleString();
      }
    }

    function tokenStatus(token) {
      if (token.revoked_at) {
        return {label: 'Revoked', badge: 'text-bg-secondary', detail: formatDate(token.revoked_at)};
      }
      var expiry = token.expires_at ? new Date(token.expires_at) : null;
      var remaining = expiry && !isNaN(expiry.getTime()) ? expiry.getTime() - Date.now() : null;
      var days = remaining === null ? null : Math.max(0, Math.ceil(remaining / 86400000));
      if (token.expired || (remaining !== null && remaining <= 0)) {
        return {label: 'Expired', badge: 'text-bg-danger', detail: formatDate(token.expires_at)};
      }
      if (days !== null && days <= 7) {
        return {label: 'Expires soon', badge: 'text-bg-danger', detail: days + ' day' + (days === 1 ? '' : 's') + ' left'};
      }
      if (days !== null && days <= 30) {
        return {label: 'Expiring', badge: 'text-bg-warning', detail: days + ' days left'};
      }
      return {label: 'Active', badge: 'text-bg-success', detail: expiry ? 'Expires ' + formatDate(token.expires_at) : 'No expiry'};
    }

    function addText(parent, tag, text, className) {
      var element = document.createElement(tag);
      if (className) element.className = className;
      element.textContent = text;
      parent.appendChild(element);
      return element;
    }

    function restrictionCount(constraints) {
      var count = 0;
      Object.keys(constraints || {}).forEach(function (key) {
        var values = constraints[key];
        if (Array.isArray(values)) count += values.length;
      });
      return count;
    }

    function makeActionButton(label, style, action, tokenId, disabled) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-' + style + ' btn-sm';
      button.textContent = label;
      button.dataset.tokenAction = action;
      button.dataset.tokenId = String(tokenId);
      button.disabled = Boolean(disabled);
      return button;
    }

    function renderTokens(tokens) {
      tokensById = {};
      clearElement(rowsElement);
      (tokens || []).forEach(function (token) {
        tokensById[String(token.id)] = token;
        var row = document.createElement('tr');

        var identityCell = document.createElement('td');
        addText(identityCell, 'div', token.name || 'Unnamed token', 'fw-semibold');
        addText(identityCell, 'code', 'tdk_' + String(token.token_prefix || '') + '_…', 'small');
        if (token.description) addText(identityCell, 'div', token.description, 'small text-muted mt-1');
        addText(identityCell, 'div', 'Created ' + formatDate(token.created_at) + ' by ' + (token.created_by || 'Unknown'), 'small text-muted');
        row.appendChild(identityCell);

        var statusCell = document.createElement('td');
        var status = tokenStatus(token);
        addText(statusCell, 'span', status.label, 'badge ' + status.badge);
        addText(statusCell, 'div', status.detail, 'small text-muted mt-1');
        row.appendChild(statusCell);

        var scopesCell = document.createElement('td');
        (token.scopes || []).forEach(function (scope) {
          addText(scopesCell, 'span', scope, 'badge text-bg-light border me-1 mb-1');
        });
        var restrictions = restrictionCount(token.constraints);
        if (restrictions) addText(scopesCell, 'div', restrictions + ' advanced restriction' + (restrictions === 1 ? '' : 's'), 'small text-muted');
        row.appendChild(scopesCell);

        var usedCell = document.createElement('td');
        addText(usedCell, 'span', token.last_used_at ? formatDate(token.last_used_at) : 'Never', token.last_used_at ? '' : 'text-muted');
        row.appendChild(usedCell);

        var actionCell = document.createElement('td');
        actionCell.className = 'text-end text-nowrap';
        actionCell.appendChild(makeActionButton('Edit permissions', 'outline-primary', 'edit', token.id, false));
        actionCell.appendChild(document.createTextNode(' '));
        actionCell.appendChild(makeActionButton('Rotate', 'outline-warning', 'rotate', token.id, !token.active));
        actionCell.appendChild(document.createTextNode(' '));
        actionCell.appendChild(makeActionButton('Revoke', 'outline-danger', 'revoke', token.id, Boolean(token.revoked_at)));
        row.appendChild(actionCell);
        rowsElement.appendChild(row);
      });
      loadingElement.classList.add('d-none');
      emptyElement.classList.toggle('d-none', Boolean(tokens && tokens.length));
      tableWrap.classList.toggle('d-none', !(tokens && tokens.length));
    }

    async function apiRequest(url, options) {
      var response = await fetch(url, options || {});
      var data = await response.json().catch(function () { return {}; });
      if (!response.ok || !data.ok) {
        throw new Error(data.message || data.error || 'The request could not be completed.');
      }
      return data;
    }

    async function loadTokens() {
      refreshButton.disabled = true;
      try {
        var data = await apiRequest('/api/config/service-tokens', {cache: 'no-store'});
        renderTokens(data.tokens || []);
      } catch (error) {
        loadingElement.classList.add('d-none');
        setAlert(String(error.message || error), 'danger');
      } finally {
        refreshButton.disabled = false;
      }
    }

    function splitValues(value) {
      return String(value || '').split(/[\n,]+/).map(function (item) {
        return item.trim();
      }).filter(Boolean);
    }

    function positiveNumbers(value, label) {
      var raw = splitValues(value);
      return raw.map(function (item) {
        var number = Number(item);
        if (!Number.isInteger(number) || number <= 0) {
          throw new Error(label + ' must contain only positive whole numbers.');
        }
        return number;
      });
    }

    function permissionsPayload(form, prefix) {
      var checkedScopes = Array.prototype.map.call(
        form.querySelectorAll('input[name="scopes"]:checked'),
        function (input) { return input.value; }
      );
      if (!checkedScopes.length) throw new Error('Choose at least one permission.');
      var routingPresets = splitValues(document.getElementById(prefix + 'routing-presets').value);
      if(routingPresets.some(function (id) { return !/^[a-f0-9]{32}$/.test(id); })) throw new Error('Routing presets must be exact saved preset IDs, not all.');
      var routingOutputs = positiveNumbers(document.getElementById(prefix + 'routing-outputs').value, 'Routing outputs');
      var extra = {};
      if(routingPresets.length) extra.routing_presets = routingPresets;
      if(routingOutputs.length) extra.routing_outputs = routingOutputs;
      return {
        scopes: checkedScopes,
        constraints: Object.assign(extra, {
          allowed_paths: splitValues(document.getElementById(prefix + 'allowed-paths').value),
          tv_targets: splitValues(document.getElementById(prefix + 'tv-targets').value),
          videohub_outputs: positiveNumbers(document.getElementById(prefix + 'vh-outputs').value, 'VideoHub outputs'),
          videohub_inputs: positiveNumbers(document.getElementById(prefix + 'vh-inputs').value, 'VideoHub inputs'),
          videohub_presets: positiveNumbers(document.getElementById(prefix + 'vh-presets').value, 'VideoHub presets'),
          atem_sources: splitValues(document.getElementById(prefix + 'atem-sources').value)
        })
      };
    }

    function creationPayload() {
      return Object.assign({name:document.getElementById('api-token-name').value.trim(),
        description:document.getElementById('api-token-description').value.trim(),
        expires_in_days:Number(document.getElementById('api-token-expiry').value)}, permissionsPayload(createForm, 'api-token-'));
    }

    function openPermissions(token) {
      editToken = {id:token.id, scopes:token.scopes.slice(), constraints:JSON.parse(JSON.stringify(token.constraints || {}))};
      document.getElementById('api-token-edit-title').textContent = 'Edit permissions — ' + token.name;
      document.getElementById('api-token-edit-error').textContent = '';
      document.getElementById('api-token-edit-confirmed').checked = false;
      var slot = document.getElementById('api-token-edit-fields'); clearElement(slot);
      [createForm.querySelector('fieldset'), createForm.querySelector('details')].forEach(function(source){
        var copy = source.cloneNode(true);
        if(copy.tagName === 'DETAILS') copy.open = Object.keys(token.constraints || {}).length > 0;
        Array.prototype.forEach.call(copy.querySelectorAll('[id],[for]'), function(el){
          ['id','for'].forEach(function(attr){if(el.hasAttribute(attr)) el.setAttribute(attr,el.getAttribute(attr).replace('api-token-','api-token-edit-'));});
        });
        slot.appendChild(copy);
      });
      var star = document.createElement('div'); star.className='form-check';
      var check=document.createElement('input'); check.type='checkbox';check.name='scopes';check.value='*';check.id='api-token-edit-scope-all';check.className='form-check-input';
      var label=document.createElement('label');label.htmlFor=check.id;label.className='form-check-label';label.textContent='* — all API scopes';star.appendChild(check);star.appendChild(label);
      slot.querySelector('fieldset').appendChild(star);
      Array.prototype.forEach.call(editForm.querySelectorAll('input[name="scopes"]'),function(el){el.checked=token.scopes.indexOf(el.value)>=0;});
      var fields={allowed_paths:'allowed-paths',tv_targets:'tv-targets',videohub_outputs:'vh-outputs',videohub_inputs:'vh-inputs',videohub_presets:'vh-presets',atem_sources:'atem-sources',routing_presets:'routing-presets',routing_outputs:'routing-outputs'};
      Object.keys(fields).forEach(function(key){document.getElementById('api-token-edit-'+fields[key]).value=(token.constraints[key] || []).join(', ');});
      editModal.show();
    }
    editElement.addEventListener('hide.bs.modal', function(event){if(editSaving) event.preventDefault();});
    editForm.addEventListener('submit', async function(event){
      event.preventDefault(); if(editSaving || !editForm.reportValidity()) return;
      var error=document.getElementById('api-token-edit-error');
      try {
        var value=permissionsPayload(editForm,'api-token-edit-');
        Object.keys(value.constraints).forEach(function(key){if(!value.constraints[key].length) delete value.constraints[key];});
        value.expected={scopes:editToken.scopes,constraints:editToken.constraints};value.confirmed=true;
        editElement.focus();editSaving=true;editForm.setAttribute('aria-busy','true');error.textContent='Saving…';
        Array.prototype.forEach.call(editForm.querySelectorAll('input,textarea,select,button'),function(el){el.disabled=true;});
        await apiRequest('/api/config/service-tokens/'+editToken.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
        editSaving=false;editModal.hide();await loadTokens();setAlert('Permissions updated. The existing token is unchanged.','success');
      } catch(e) {error.textContent=String(e.message || e);}
      finally {editSaving=false;editForm.setAttribute('aria-busy','false');Array.prototype.forEach.call(editForm.querySelectorAll('input,textarea,select,button'),function(el){el.disabled=false;});}
    });

    function showSecret(token, message) {
      secretInput.value = token.token || '';
      secretMeta.textContent = (message || 'Created') + ' “' + (token.name || '') + '”; expires ' + formatDate(token.expires_at) + '.';
      copyButton.textContent = 'Copy';
      secretModal.show();
      window.setTimeout(function () { secretInput.focus(); secretInput.select(); }, 250);
    }

    createForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      setAlert('', '');
      if (!createForm.reportValidity()) return;
      createButton.disabled = true;
      try {
        var data = await apiRequest('/api/config/service-tokens', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(creationPayload())
        });
        createForm.reset();
        await loadTokens();
        showSecret(data.token, 'Created');
      } catch (error) {
        setAlert(String(error.message || error), 'danger');
      } finally {
        createButton.disabled = false;
      }
    });

    refreshButton.addEventListener('click', loadTokens);

    rowsElement.addEventListener('click', async function (event) {
      var button = event.target.closest('[data-token-action]');
      if (!button) return;
      var token = tokensById[String(button.dataset.tokenId)];
      if (!token) return;
      if (button.dataset.tokenAction === 'edit') { openPermissions(token); return; }
      if (button.dataset.tokenAction === 'rotate') {
        document.getElementById('api-token-rotate-id').value = String(token.id);
        document.getElementById('api-token-rotate-name').value = token.name || '';
        document.getElementById('api-token-rotate-expiry').value = root.dataset.defaultExpiryDays || '365';
        rotateModal.show();
        return;
      }
      if (button.dataset.tokenAction === 'revoke') {
        if (!window.confirm('Revoke “' + token.name + '”? Any integration using it will stop working immediately.')) return;
        button.disabled = true;
        setAlert('', '');
        try {
          await apiRequest('/api/config/service-tokens/' + encodeURIComponent(token.id), {method: 'DELETE'});
          await loadTokens();
          setAlert('Token “' + token.name + '” was revoked.', 'success');
        } catch (error) {
          button.disabled = false;
          setAlert(String(error.message || error), 'danger');
        }
      }
    });

    rotateForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!rotateForm.reportValidity()) return;
      rotateButton.disabled = true;
      setAlert('', '');
      var tokenId = document.getElementById('api-token-rotate-id').value;
      try {
        var data = await apiRequest('/api/config/service-tokens/' + encodeURIComponent(tokenId) + '/rotate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: document.getElementById('api-token-rotate-name').value.trim(),
            expires_in_days: Number(document.getElementById('api-token-rotate-expiry').value)
          })
        });
        rotateModal.hide();
        await loadTokens();
        showSecret(data.token, 'Rotated');
      } catch (error) {
        setAlert(String(error.message || error), 'danger');
      } finally {
        rotateButton.disabled = false;
      }
    });

    copyButton.addEventListener('click', async function () {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(secretInput.value);
        } else {
          secretInput.focus();
          secretInput.select();
          document.execCommand('copy');
        }
        copyButton.textContent = 'Copied';
      } catch (error) {
        secretInput.focus();
        secretInput.select();
        copyButton.textContent = 'Select and copy';
      }
    });

    secretModalElement.addEventListener('hidden.bs.modal', function () {
      secretInput.value = '';
      secretMeta.textContent = '';
      copyButton.textContent = 'Copy';
    });

    loadTokens();
  }

  window._initApiTokensPage = _initApiTokensPage;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initApiTokensPage, {once: true});
  } else {
    _initApiTokensPage();
  }
})();
