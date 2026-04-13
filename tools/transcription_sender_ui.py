from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from werkzeug.serving import make_server

from transcription_sender_lib import (
    DEFAULT_LOCAL_PORT,
    SenderService,
    list_input_devices,
    load_sender_config,
    normalize_sender_config,
    open_local_browser,
    save_sender_config,
    sender_pid_path,
)


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TDeck Transcription Sender</title>
  <style>
    :root{
      --bg:#0f172a;
      --panel:#111827;
      --soft:#1f2937;
      --text:#e5e7eb;
      --muted:#94a3b8;
      --accent:#38bdf8;
      --accent2:#6366f1;
      --good:#22c55e;
      --warn:#f59e0b;
      --bad:#ef4444;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:Segoe UI, Arial, sans-serif;
      background:radial-gradient(circle at top left, rgba(99,102,241,.25), transparent 35%), radial-gradient(circle at top right, rgba(56,189,248,.18), transparent 35%), var(--bg);
      color:var(--text);
    }
    .wrap{max-width:980px;margin:0 auto;padding:24px}
    .hero{margin-bottom:18px}
    .hero h1{margin:0 0 6px;font-size:2rem}
    .hero p{margin:0;color:var(--muted)}
    .grid{display:grid;grid-template-columns:1.2fr .9fr;gap:18px}
    .card{
      background:linear-gradient(180deg, rgba(17,24,39,.96), rgba(15,23,42,.96));
      border:1px solid rgba(148,163,184,.18);
      border-radius:16px;
      padding:18px;
      box-shadow:0 12px 30px rgba(0,0,0,.25);
    }
    .card h2{margin:0 0 14px;font-size:1.1rem}
    label{display:block;font-size:.88rem;color:var(--muted);margin-bottom:6px}
    input,select{
      width:100%;
      padding:11px 12px;
      border-radius:10px;
      border:1px solid rgba(148,163,184,.2);
      background:var(--soft);
      color:var(--text);
      margin-bottom:14px;
    }
    .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .actions{display:flex;gap:10px;flex-wrap:wrap}
    button{
      border:0;
      border-radius:999px;
      padding:11px 16px;
      cursor:pointer;
      font-weight:600;
    }
    .primary{background:linear-gradient(135deg,var(--accent2),var(--accent));color:white}
    .secondary{background:rgba(148,163,184,.16);color:var(--text)}
    .danger{background:rgba(239,68,68,.15);color:#fecaca;border:1px solid rgba(239,68,68,.25)}
    .status{
      padding:12px 14px;
      border-radius:12px;
      margin-bottom:14px;
      background:rgba(148,163,184,.14);
    }
    .status.good{background:rgba(34,197,94,.14);color:#bbf7d0}
    .status.warn{background:rgba(245,158,11,.14);color:#fde68a}
    .status.bad{background:rgba(239,68,68,.14);color:#fecaca}
    .kv{display:grid;grid-template-columns:120px 1fr;gap:8px;font-size:.92rem}
    .kv div:nth-child(odd){color:var(--muted)}
    .help{font-size:.88rem;color:var(--muted);line-height:1.45}
    .code{font-family:Consolas, monospace;word-break:break-all}
    @media (max-width:820px){
      .grid,.row2{grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>TDeck Transcription Sender</h1>
      <p>Choose the microphone on this computer, save the server details once, then start streaming without command-line flags.</p>
    </div>
    <div class="grid">
      <div class="card">
        <h2>Sender Setup</h2>
        <div id="sender-status" class="status">Loading…</div>
        <form id="sender-form">
          <label for="server">TDeck Server URL</label>
          <input id="server" name="server" placeholder="http://192.168.1.118:5000">

          <label for="token">Ingest Token</label>
          <input id="token" name="token" type="password" placeholder="Shared token from TDeck Config">

          <label for="source_name">Source Name</label>
          <input id="source_name" name="source_name" placeholder="e.g. FOH Comms Mac">

          <label for="device">Microphone</label>
          <select id="device" name="device"></select>

          <div class="row2">
            <div>
              <label for="sample_rate">Sample Rate</label>
              <input id="sample_rate" name="sample_rate" type="number" min="8000" max="96000">
            </div>
            <div>
              <label for="chunk_ms">Chunk Size (ms)</label>
              <input id="chunk_ms" name="chunk_ms" type="number" min="40" max="4000">
            </div>
          </div>

          <div class="row2">
            <div>
              <label for="channels">Channels</label>
              <select id="channels" name="channels">
                <option value="1">Mono</option>
                <option value="2">Stereo</option>
              </select>
            </div>
            <div>
              <label for="ui_port">Local UI Port</label>
              <input id="ui_port" name="ui_port" type="number" min="1024" max="65535">
            </div>
          </div>

          <div class="actions">
            <button type="submit" class="primary">Save Settings</button>
            <button type="button" class="secondary" id="test-btn">Test Connection</button>
            <button type="button" class="secondary" id="refresh-btn">Refresh Mics</button>
            <button type="button" class="primary" id="start-btn">Start Streaming</button>
            <button type="button" class="danger" id="stop-btn">Stop</button>
          </div>
        </form>
      </div>
      <div class="card">
        <h2>Live Status</h2>
        <div class="kv" id="live-status">
          <div>State</div><div id="state">-</div>
          <div>Microphone</div><div id="active-device">-</div>
          <div>Server</div><div id="active-server" class="code">-</div>
          <div>Last Success</div><div id="last-ok">-</div>
          <div>Last Error</div><div id="last-error">-</div>
        </div>
        <hr style="border-color:rgba(148,163,184,.18);margin:18px 0">
        <div class="help">
          <p>Normal volunteer workflow:</p>
          <p>1. Open this local page.</p>
          <p>2. Choose the microphone once.</p>
          <p>3. Click <strong>Start Streaming</strong>.</p>
          <p>4. Leave this page open or use the double-click launchers next time.</p>
        </div>
      </div>
    </div>
  </div>
  <script>
    let currentConfig = null;
    let currentDevices = [];
    let formDirty = false;

    function fmtTime(ts){
      if(!ts) return '-';
      try{ return new Date(Number(ts) * 1000).toLocaleTimeString(); }catch(e){ return '-'; }
    }

    function statusClass(state){
      if(state === 'streaming') return 'status good';
      if(state === 'connecting') return 'status warn';
      if(state === 'error') return 'status bad';
      return 'status';
    }

    function formData(){
      return {
        server: document.getElementById('server').value,
        token: document.getElementById('token').value,
        source_name: document.getElementById('source_name').value,
        device: document.getElementById('device').value,
        sample_rate: Number(document.getElementById('sample_rate').value || 16000),
        chunk_ms: Number(document.getElementById('chunk_ms').value || 200),
        channels: Number(document.getElementById('channels').value || 1),
        ui_port: Number(document.getElementById('ui_port').value || 8766)
      };
    }

    function applyConfig(cfg, devices, {force=false} = {}){
      currentConfig = cfg || {};
      currentDevices = Array.isArray(devices) ? devices : [];
      const sel = document.getElementById('device');
      const prior = sel.value;
      sel.innerHTML = '';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = 'Default system microphone';
      sel.appendChild(blank);
      currentDevices.forEach(dev => {
        const opt = document.createElement('option');
        opt.value = String(dev.id);
        opt.textContent = `${dev.name} (${dev.max_input_channels} in)`;
        sel.appendChild(opt);
      });

      if(force || !formDirty){
        document.getElementById('server').value = currentConfig.server || '';
        document.getElementById('token').value = currentConfig.token || '';
        document.getElementById('source_name').value = currentConfig.source_name || '';
        document.getElementById('sample_rate').value = currentConfig.sample_rate || 16000;
        document.getElementById('chunk_ms').value = currentConfig.chunk_ms || 200;
        document.getElementById('channels').value = String(currentConfig.channels || 1);
        document.getElementById('ui_port').value = currentConfig.ui_port || 8766;
        sel.value = String(currentConfig.device || '');
      } else {
        sel.value = prior;
      }
    }

    function applyStatus(st){
      const state = String(st.status || 'idle');
      const box = document.getElementById('sender-status');
      box.className = statusClass(state);
      box.textContent = state === 'error'
        ? `Error: ${st.last_error || 'Unknown error'}`
        : (state.charAt(0).toUpperCase() + state.slice(1));
      document.getElementById('state').textContent = state;
      document.getElementById('active-device').textContent = st.active_device_name || 'Default system microphone';
      document.getElementById('active-server').textContent = (st.config && st.config.server) || '-';
      document.getElementById('last-ok').textContent = fmtTime(st.last_ok_at);
      document.getElementById('last-error').textContent = st.last_error || '-';
    }

    async function loadState({refreshForm=false} = {}){
      const res = await fetch('/api/sender/state', {cache:'no-store'});
      const data = await res.json();
      applyConfig(data.config || {}, data.devices || [], {force: refreshForm});
      applyStatus(data.status || {});
    }

    async function postJson(url, body){
      const res = await fetch(url, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body || {})
      });
      const data = await res.json().catch(() => ({}));
      if(!res.ok || data.ok === false){
        throw new Error(data.error || 'Request failed');
      }
      return data;
    }

    document.getElementById('sender-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const data = await postJson('/api/sender/config', formData());
      formDirty = false;
      applyConfig(data.config || {}, data.devices || [], {force:true});
      applyStatus(data.status || {});
    });

    document.getElementById('test-btn').addEventListener('click', async () => {
      const data = await postJson('/api/sender/test', formData());
      const box = document.getElementById('sender-status');
      box.className = data.ok ? 'status good' : 'status bad';
      box.textContent = data.ok
        ? `Connected. Ingest URL: ${(data.remote && data.remote.ingest_url) || 'ok'}`
        : (data.error || 'Connection failed');
    });

    document.getElementById('refresh-btn').addEventListener('click', () => loadState({refreshForm:true}));

    document.getElementById('start-btn').addEventListener('click', async () => {
      const data = await postJson('/api/sender/start', formData());
      formDirty = false;
      applyConfig(data.config || {}, data.devices || [], {force:true});
      applyStatus(data.status || {});
    });

    document.getElementById('stop-btn').addEventListener('click', async () => {
      const data = await postJson('/api/sender/stop', {});
      applyStatus(data.status || {});
    });

    document.querySelectorAll('#sender-form input, #sender-form select').forEach(el => {
      el.addEventListener('input', () => { formDirty = true; });
      el.addEventListener('change', () => { formDirty = true; });
    });

    loadState({refreshForm:true});
    setInterval(() => loadState({refreshForm:false}), 2000);
  </script>
</body>
</html>"""


service = SenderService()
app = Flask(__name__)


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/sender/state', methods=['GET'])
def api_sender_state():
    return jsonify({
        'ok': True,
        'config': service.get_config(),
        'devices': list_input_devices(),
        'status': service.status_snapshot(),
    })


@app.route('/api/sender/config', methods=['POST'])
def api_sender_config():
    body = request.get_json() or {}
    cfg = service.save_config(body)
    return jsonify({
        'ok': True,
        'config': cfg,
        'devices': list_input_devices(),
        'status': service.status_snapshot(),
    })


@app.route('/api/sender/test', methods=['POST'])
def api_sender_test():
    body = request.get_json() or {}
    cfg = normalize_sender_config(body)
    ok, err, remote = service.test_connection(cfg)
    return jsonify({
        'ok': ok,
        'error': err,
        'remote': remote,
    }), (200 if ok else 400)


@app.route('/api/sender/start', methods=['POST'])
def api_sender_start():
    body = request.get_json() or {}
    cfg = service.save_config(body)
    ok, err = service.start(cfg)
    return jsonify({
        'ok': ok,
        'error': err,
        'config': cfg,
        'devices': list_input_devices(),
        'status': service.status_snapshot(),
    }), (200 if ok else 400)


@app.route('/api/sender/stop', methods=['POST'])
def api_sender_stop():
    service.stop()
    return jsonify({
        'ok': True,
        'status': service.status_snapshot(),
    })


@app.route('/api/sender/shutdown', methods=['POST'])
def api_sender_shutdown():
    service.stop()

    def _shutdown_later():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_shutdown_later, daemon=True).start()
    return jsonify({'ok': True})


def _write_pid() -> None:
    sender_pid_path().write_text(str(os.getpid()), encoding='utf-8')


def _clear_pid() -> None:
    try:
        sender_pid_path().unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Run the local TDeck transcription sender UI.')
    parser.add_argument('--port', type=int, default=None, help='Local UI port. Default comes from saved config or 8766.')
    parser.add_argument('--no-browser', action='store_true', help='Do not open the local UI in a browser.')
    args = parser.parse_args()

    cfg = load_sender_config()
    port = int(args.port or cfg.get('ui_port') or DEFAULT_LOCAL_PORT)
    _write_pid()

    server = make_server('127.0.0.1', port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f'TDeck transcription sender UI running at http://127.0.0.1:{port}')
    if not args.no_browser and bool(cfg.get('open_browser_on_launch', True)):
      open_local_browser(port)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            service.stop()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        _clear_pid()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
