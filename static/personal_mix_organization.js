/* Setup-only draft adapter. All movement is owned by catalog_organization.js. */
(function () {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  function legacy(rows, grouped) {
    const tree = {folders: [], items: []};
    let folderId = null, previous = '';
    rows.forEach(row => {
      const heading = String(row.group || row.title || '').trim();
      if (grouped && heading && heading !== previous) {
        folderId = 'legacy-' + row.id;
        tree.folders.push({id: folderId, name: heading});
      }
      tree.items.push({id: String(row.id), folderId});
      previous = heading;
    });
    return tree;
  }
  window.TDeckMixOrganization = {
    legacy,
    attach(root, organization, rows, grouped, dirty) {
      let entries = rows.slice();
      let tree = organization ? clone(organization) : legacy(entries, grouped);
      function render() {
        const ids = entries.map(row => String(row.id));
        tree.items = tree.items.filter(item => ids.indexOf(String(item.id)) >= 0);
        entries.forEach(row => {
          if (!tree.items.some(item => String(item.id) === String(row.id))) tree.items.push({id: String(row.id), folderId: null});
        });
        window.renderCatalogOrganization(root, tree, entries, {
          editable: true, folders: grouped, draft: true,
          labels: {root: grouped ? 'Ungrouped' : 'AUX order', singular: 'group', create: 'New group', prompt: 'Group name'},
          save(next) { tree = clone(next); dirty(); },
          error(message) { dirty(message); },
        });
      }
      const controller = {
        organization() {
          // Editable manual numbers are physical identities, not DOM positions.
          const result = clone(tree);
          result.items.forEach(item => {
            const entry = entries.find(row => String(row.id) === String(item.id));
            if (entry.identity) item.id = String(entry.identity());
          });
          return result;
        },
        add(entry) { entries.push(entry); render(); dirty(); },
        remove(node) { entries = entries.filter(row => row.node !== node); render(); dirty(); },
        physicalOrder() { tree.items.sort((a, b) => Number(a.id) - Number(b.id)); render(); dirty(); },
      };
      root._mixOrganization = controller;
      render();
      return controller;
    },
  };
}());
