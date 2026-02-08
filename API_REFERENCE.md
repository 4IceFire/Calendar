# API Reference (WebUI)

This document lists the HTTP API endpoints implemented by the Flask Web UI server (`webui.py`).

## Basics

- Base URL: `http://<host>:<port>`
- Port: configured by `webserver_port` in `config.json` (default typically `5000`)
- Auth: none (intended for trusted LAN usage)
- Format: JSON (unless otherwise noted)

### Note about `/api` in trigger editors

All HTTP API endpoints are served under `/api/...`.

When configuring a scheduled **API Call** trigger in the UI, you can enter paths like `/videohub/ping` or `videohub/ping` and the app will automatically normalize them to `/api/videohub/ping` for execution.

### Indexing conventions

- Calendar event IDs: integers assigned by the server.
- Timer preset selection for `/api/timers/apply`: **1-based** (1 selects the first preset).
- VideoHub routing:
  - `/api/videohub/route`: defaults to **1-based**, unless `zero_based=true`
  - VideoHub presets store routes using **1-based** numbers.

---

## Calendar

### List events (for UI)
- **GET** `/api/ui/events`
- **Returns:** JSON array of events.
- **Notes:** Reads from the configured `EVENTS_FILE`.

Event shape (simplified):
```json
{
  "id": 1,
  "name": "Sunday Service",
  "date": "2026-01-21",
  "time": "09:30:00",
  "repeating": false,
  "active": true,
  "times": [
    {"minutes": 10, "typeOfTrigger": "BEFORE", "actionType": "companion", "buttonURL": "location/1/0/1/press"},
    {"minutes": 0, "typeOfTrigger": "AT", "actionType": "api", "api": {"method": "POST", "path": "/api/videohub/presets/1/apply"}}
  ]
}
```

### Create event
- **POST** `/api/ui/events`
- **Body:** event object (similar to the shape above; `id` is assigned server-side).
- **Returns:** `{ "ok": true, "id": <new_id> }` on success.

### Get event by id
- **GET** `/api/events/<id>`
- **Returns:** single event object.

### Update event by id
- **PUT** `/api/events/<id>`
- **Body:** event fields to update.
- **Returns:** `{ "ok": true, "id": <id> }` on success.

### Delete event by id
- **DELETE** `/api/events/<id>`
- **Returns:** `{ "removed": true, "id": <id>, "name": "..." }` on success.

### Upcoming triggers (dashboard)
- **GET** `/api/upcoming_triggers`
- **Returns:** `{ now_ms, triggers: [...] }`
- **Notes:** Used by the UI to display the next few trigger actions.
- **Query:** optional `limit` (default `3`, max `500`).

Trigger entry shape (simplified):
```json
{
  "due_ms": 1730000000000,
  "seconds_until": 120,
  "event": "Sunday Service",
  "event_id": 1,
  "offset_min": 10,
  "offset": "10m",
  "actionType": "companion",
  "buttonURL": "location/1/0/1/press",
  "api": null,
  "button": {"label": "Start Stream", "pattern": "1/0/1"}
}
```

---

## Timers

### Get timer settings + presets
- **GET** `/api/timers`
- **Returns:**
  - `propresenter_timer_index` (1-based index)
  - `stream_start_preset` (1-based index, or `0` when not configured)
  - `timer_presets` (array)

### Save timer presets + ProPresenter timer index
- **POST** `/api/timers`
- **Body:**
```json
{
  "propresenter_timer_index": 1,
  "stream_start_preset": 4,
  "timer_presets": [
    {
      "time": "08:15",
      "name": "Timer 1",
      "button_presses": [{"buttonURL": "location/1/0/1/press"}]
    }
  ]
}
```
- **Notes:** Presets are persisted to `timer_presets.json` (not stored inline in `config.json`).
  - `stream_start_preset` is optional. Use `0` or omit to disable stream-start stage messages.

### Update one timer preset time (no full list required)
- **PATCH** (or **POST**) `/api/timers/preset`
- **Body:**
```json
{ "preset": 2, "time": "08:15" }
```
- **Notes:**
  - `preset` is **1-based** (2 means the 2nd preset in `timer_presets.json`).
  - Also accepts optional `name` to rename that preset.
  - `time` can also be relative to the event start time when called by a scheduled API trigger:
    - Example: `{ "preset": 2, "time": "$-60" }` means “event_start minus 60 minutes”.
    - The scheduler automatically injects `event_start` into internal API trigger bodies.
    - If you call this endpoint manually and use `$...`, include `event_start` (or `base_time`) as an ISO datetime:
      - Example: `{ "preset": 2, "time": "$-60", "event_start": "2026-01-25T12:00:00" }`
  - Optional: set `apply: true` to immediately apply/start that preset (same behavior as calling `/api/timers/apply` right after).
    - Example: `{ "preset": 2, "time": "08:15", "apply": true }`
  - Best-effort: updates the corresponding Companion custom variable for that preset index.

### Apply a timer preset (Companion → WebUI)
- **POST** `/api/timers/apply`
- **Input (either):**
  - JSON body: `{ "preset": 1 }`
  - Query string: `?preset=1`
- **Notes:** `preset` is always **1-based** (1 selects the first preset).
- **Returns:** JSON describing what happened (button presses fired + ProPresenter timer set/reset/start attempts).

---

## ProPresenter Timers

These endpoints control a ProPresenter countdown timer directly (useful for scheduled API triggers).

Timer selection is either:
- `timer_id` (0-based, ProPresenter-native), OR
- `timer_index` / `propresenter_timer_index` (1-based, human-friendly)

### Set a timer to a time
- **POST** `/api/propresenter/timer/set`
- **Body:**
```json
{ "time": "08:15", "timer_index": 2, "reset": true }
```

### Start a timer
- **POST** `/api/propresenter/timer/start`
- **Body:**
```json
{ "timer_index": 2 }
```

### Stop a timer
- **POST** `/api/propresenter/timer/stop`
- **Body:**
```json
{ "timer_index": 2 }
```

### Reset a timer
- **POST** `/api/propresenter/timer/reset`
- **Body:**
```json
{ "timer_index": 2 }
```

---

## ProPresenter Stage Messages

### Send a stage message
- **POST** `/api/propresenter/stage/message`
- **Body:**
```json
{ "message": "STREAM 9:30AM" }
```
- **Notes:** Use this generic endpoint for future custom stage messages.

### Send stream-start stage message
- **POST** `/api/propresenter/stage/stream_start`
- **Body:** none required.
- **Notes:** Uses `stream_start_preset` and `timer_presets` to build a message like `STREAM 9:30AM`.

### Clear stage message
- **POST** `/api/propresenter/stage/clear`
- **Body:** none required.

---

## VideoHub

### Ping VideoHub
- **GET** `/api/videohub/ping`
- **Returns:** `{ "ok": true|false }`
- **Errors:** `400` if `videohub_ip` isn’t configured.

### Route a single output
- **POST** `/api/videohub/route`
- **Body (preferred):**
```json
{
  "output": 1,
  "input": 3,
  "monitor": false,
  "zero_based": false
}
```
- **Notes:**
  - By default, `output`/`input` are treated as **1-based** for humans.
  - Set `zero_based=true` to pass VideoHub-native indexes.

### Get input/output labels (for dropdowns)
- **GET** `/api/videohub/labels`
- **Returns:**
```json
{
  "ok": true,
  "configured": true,
  "inputs": [{"number": 1, "label": "Camera 1"}],
  "outputs": [{"number": 1, "label": "TV 1"}]
}
```
- **Notes:** Best-effort. Falls back to numeric-only 1..40 if labels can’t be fetched.

### Get labels + current routing snapshot
- **GET** `/api/videohub/state`
- **Returns:**
```json
{
  "ok": true,
  "configured": true,
  "inputs": [{"number": 1, "label": "Camera 1"}],
  "outputs": [{"number": 1, "label": "TV 1"}],
  "routing": [4, 1, 2, 3]
}
```
- **Notes:**
  - `routing` is a 1-based list where index 0 corresponds to output #1.
  - Best-effort. If routing can’t be fetched, returns an identity-style routing (1..40).

### Presets: list
- **GET** `/api/videohub/presets`
- **Returns:** `{ ok: true, presets: [...] }`

### Presets: create
- **POST** `/api/videohub/presets`
- **Body:**
```json
{
  "name": "Sunday Service",
  "locked": false,
  "routes": [
    {"output": 1, "input": 4, "monitoring": false}
  ]
}
```
- **Notes:**
  - Presets save **numbers only**; names/labels are fetched separately.
  - `locked=true` prevents updates/deletes until unlocked.

### Presets: update
- **PUT** `/api/videohub/presets/<id>`
- **Body:** same as create.

### Presets: lock/unlock (prevents edits)
- **POST** `/api/videohub/presets/<id>/lock`
- **Body:**
```json
{ "locked": true }
```
- **Notes:**
  - If `locked` is omitted, the server toggles the current value.

### Presets: delete
- **DELETE** `/api/videohub/presets/<id>`

### Presets: save snapshot from device
- **POST** `/api/videohub/presets/from_device`
- **Body:**
```json
{ "name": "Default routing" }
```
- **Notes:** Pulls current routing from the configured VideoHub and saves it as a preset snapshot (outputs 1..40).

### Presets: apply (Companion → WebUI)
- **POST** `/api/videohub/presets/<id>/apply`
- **Body:** none required.
- **Returns:** `{ ok: true, result: {...} }`

---

## Templates (used by Calendar + Timers UI)

### Get templates
- **GET** `/api/templates`
- **Returns:** `{ buttons: [...], triggers: [...] }`

### Button templates
- **POST** `/api/templates/button`
- **PUT** `/api/templates/button/<idx>`
- **DELETE** `/api/templates/button/<idx>`

`idx` is a **0-based array index** into the JSON file (not a stable ID).

### Trigger templates
- **POST** `/api/templates/trigger`
- **PUT** `/api/templates/trigger/<idx>`
- **DELETE** `/api/templates/trigger/<idx>`

`idx` is a **0-based array index** into the JSON file (not a stable ID).

---

## System / Status

### Config
- **GET** `/api/config`
- **POST** `/api/config` (merge provided keys into `config.json`)

### Status indicators
- **GET** `/api/companion_status`
- **GET** `/api/propresenter_status`
- **GET** `/api/videohub_status`

---

## Web Console

### Fetch logs
- **GET** `/api/console/logs`

### Run CLI command
- **POST** `/api/console/run`
