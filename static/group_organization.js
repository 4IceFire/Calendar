(function () {
  'use strict';
  async function initGroupOrganization() {
    const root = document.getElementById('access-levels-role-list');
    if (!root) return;
    const message = document.createElement('div'); message.className = 'alert alert-danger d-none';
    root.parentNode.insertBefore(message, root);
    function error(text) { message.textContent = text; message.classList.remove('d-none'); }
    async function request(url, value) {
      const response = await fetch(url, value === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not save group changes');
      message.classList.add('d-none'); return data;
    }
    document.getElementById('group-editor-header').addEventListener('click', async event => {
      const control = event.target.closest('[data-group-rename]');
      if (!control) return;
      const name = window.prompt('Group name', control.getAttribute('data-group-current-name'));
      if (!name || !name.trim()) return;
      control.disabled = true;
      try {
        await request('/api/admin/groups/' + control.getAttribute('data-group-rename') + '/rename', {name: name.trim()});
        // Refresh all membership labels and accessible tab names from the saved identity.
        window.location.reload();
      } catch (failure) { error(failure.message); control.disabled = false; }
    });
    try {
      const rows = Array.from(root.querySelectorAll('[data-role-item]')).map(node => ({id: node.getAttribute('data-role-id'), node, label: node.querySelector('.fw-semibold').textContent}));
      const data = await request('/api/admin/groups/organization');
      window.renderCatalogOrganization(root, data.organization, rows, {
        editable: true, error,
        save: async value => (await request('/api/admin/groups/organization', value)).organization
      });
    } catch (failure) { error(failure.message); }
  }
  document.addEventListener('DOMContentLoaded', initGroupOrganization);
}());
