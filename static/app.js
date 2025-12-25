// Poll companion status endpoint and update indicator
async function updateCompanion() {
  try {
    const res = await fetch('/api/companion_status');
    if (!res.ok) throw new Error('fetch failed');
    const data = await res.json();
    const dot = document.getElementById('companion-dot');
    const label = document.getElementById('companion-label');
    if (data.connected) {
      dot.classList.remove('bg-danger');
      dot.classList.add('bg-success');
      label.textContent = 'Companion: Online';
    } else {
      dot.classList.remove('bg-success');
      dot.classList.add('bg-danger');
      label.textContent = 'Companion: Offline';
    }
  } catch (e) {
    const dot = document.getElementById('companion-dot');
    const label = document.getElementById('companion-label');
    dot.classList.remove('bg-success');
    dot.classList.add('bg-danger');
    label.textContent = 'Companion: Unknown';
  }
}

// initial check
updateCompanion();
// refresh every 10s for more responsive UI
setInterval(updateCompanion, 10000);

// --- App Control functions ---
async function loadApps() {
  try {
    const res = await fetch('/api/apps');
    const data = await res.json();
    const body = document.getElementById('apps-body');
    if (!body) return;
    body.innerHTML = '';
    data.forEach(a => {
      const tr = document.createElement('tr');
      const nameTd = document.createElement('td');
      nameTd.textContent = a.name;
      const statusTd = document.createElement('td');
      statusTd.textContent = a.running ? 'Running' : 'Stopped';
      const actionTd = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = a.running ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-success';
      btn.textContent = a.running ? 'Stop' : 'Start';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const method = a.running ? 'stop' : 'start';
          const resp = await fetch(`/api/apps/${encodeURIComponent(a.name)}/${method}`, {method: 'POST'});
          // refresh apps list and companion indicator immediately after action
          await loadApps();
          try { updateCompanion(); } catch (e) {}
        } catch (e) {
          console.error(e);
        } finally {
          btn.disabled = false;
        }
      };
      actionTd.appendChild(btn);
      tr.appendChild(nameTd);
      tr.appendChild(statusTd);
      tr.appendChild(actionTd);
      body.appendChild(tr);
    });
  } catch (e) {
    console.error('Failed to load apps', e);
  }
}

// If on apps page, load apps periodically
if (document.getElementById('apps-body')) {
  loadApps();
  setInterval(loadApps, 5000);
}
