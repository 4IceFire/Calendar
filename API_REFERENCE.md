# API Reference (WebUI)

This document lists the HTTP API endpoints implemented by the Flask Web UI server (`webui.py`).

## Basics

- Base URL: `http://<host>:<port>`
- Port: configured by `webserver_port` in `config.json` (default typically `5000`)
- Auth: authenticated browser session or scoped Bearer service token when `auth_enabled` is true
- Format: JSON (unless otherwise noted)

## Media library and ATEM players

These endpoints are browser-session only, including their `/api/v1/...` aliases.
Service tokens and scheduler calls are denied. Mutations require the usual CSRF
and same-origin checks. Image numbers in configuration and responses are 1-based.

| Method and path | Access | Contract |
| --- | --- | --- |
| `GET /api/media` | Media or Config | `{ok, items, permissions}`; each item has `id`, `name`, `preset`, dimensions, size, creation time, `url` and `thumbnail_url`. |
| `POST /api/media/upload` | Config, or Media + upload | Multipart `file` and optional `name`; returns `201 {ok, item}`. JPEG/PNG/WebP/HEIC/HEIF, 20 MiB, 40 MP. Saving does not display the image. |
| `PATCH /api/media/<id>` | Config | JSON `name` and/or boolean `preset`; returns `{ok, item}`. |
| `DELETE /api/media/<id>` | Config | Removes the local image. An active display/load of it returns 409. Does not clear ATEM stills. |
| `POST /api/media/display` | Routing + Media | JSON `{"media_id":"<id>","output":1}`; returns `202 {ok,job}`. Validates output and mapped input against the user's Routing allow-lists. Server chooses the player/input; overrides are rejected. ATEM output routing is never changed. |
| `GET /api/media/display/<job_id>` | Routing + Media, allowed output | `{ok,job}` with `id`, `mediaId`, `output`, `status`, `message`, `error`. No hardware assignments or internal diagnostics. |
| `GET /api/atem/media/state` | Config | Cached connection, detected format/capacity, configured destinations, players/stills/AUXes and current/last ATEM `job`. An offline state is a successful HTTP read. |
| `POST /api/atem/media/load` | Config + Media | Administrative player test. JSON `{"media_id":"<id>","player":2}`; returns `202 {ok, job}`. Does not route a TV. |
| `GET /api/config/atem-media` | Config | `{ok, config}` with `atem_media_enabled`, `atem_media_node_path` and `atem_media_destinations`. Obsolete destination `aux` values are omitted from the response without changing saved configuration. |
| `PUT /api/config/atem-media` | Config | Partial configuration object with those keys only. Executable-path changes additionally require Admin. Destinations are `{player,label,slots,videohub_input}` with at least two exclusive still slots. Omitting the VideoHub input preserves a test-only player. Players, inputs and slots must be distinct. ATEM output routing is managed manually; legacy `aux` values are ignored and removed on save. |

Display statuses are `queued`, `preparing`, `loading`, `routing`, `succeeded`,
`failed`; 202 only means queued. Completion requires image/hash, player selection
and VideoHub readback. The mapped VideoHub input must not feed other outputs.
ATEM output routing and the feed into that input are maintained manually;
destinations already sharing a media player also see its new image. The
200-second display deadline wraps a maximum 180-second ATEM transfer. Only recent
32 job records are retained in memory; restart does not repeat requests. While a
display owns routing, normal VideoHub route/preset writes and raw player tests
return 409 immediately. External controllers are not part of that reservation.

The existing ATEM test job also uses `uploading` and `selecting` statuses and
includes player/slot details for Config. Library image URLs require Media
or Config and return PNGs with private/no-store caching. Validation returns 400,
missing media/jobs 404, oversized uploads 413 and storage/runtime failures 503.
`page:media` permits browsing and selecting existing images. Optional
`page:media_upload` is edited in the group's Media tab and requires Media access
for operator uploads. The legacy `page:media_load` and `page:media_manage` keys
are ignored; management now requires Config.

## Administrator user testing

`POST /admin/users/<id>/view-as` starts an interactive browser view using the
target's current page and resource permissions. `POST /auth/view-as/stop`
returns to the initiating administrator. These are HTML form routes with 303
redirects, not token APIs; both require same-origin requests, a valid `_csrf`
form token, and a current login in the protected Admin group. Nested testing
is rejected. The target must be active, unlocked, and not awaiting a password
change. Each transition rotates CSRF.

The original login/session remains in place. The selected user's own sessions
are unchanged. Target permission checks apply to API calls and uploads, which
perform real actions; account credential changes are blocked. The server checks
administrator authority/session and target account/session version on each
request. Invalid target state ends testing without running the pending write.
Activity events include both the administrator and target under `details.view_as`.

## API authentication and migration

TDeck applies an explicit, fail-closed policy to every `/api` endpoint when
`auth_enabled` is true. A newly added endpoint is denied until it is assigned a
server-side capability policy.

- Browser reads require a logged-in session and the page permission associated
  with that API. Browser writes additionally require `X-CSRF-Token` and a
  same-origin `Origin` or `Referer`. The bundled `api_client.js` adds the CSRF
  header to same-origin API writes automatically.
- Automation uses `Authorization: Bearer <service-token>`. A service token does
  not use cookies or CSRF. Tokens are stored as SHA-256 hashes in `auth.db`; the
  plaintext is displayed only once.
- `/api/v1/...` is a stable alias for every documented `/api/...` route and
  returns `X-TDeck-API-Version: 1`. Existing `/api/...` routes remain during the
  migration so saved Companion action option IDs and URLs keep working.
- `/videohub/monitor` now requires normal VideoHub page access because its live
  state API is authenticated; it is no longer a silently broken public shell.

Available token scopes are `read`, `timers`, `videohub`, `tvs`, `atem`,
`propresenter`, `ccb`, `pixie`, `digico`, `calendar`, `config`, and `admin`.
`read` permits read-only calls across resource APIs; a resource scope permits
that resource's reads and writes. Avoid `*` unless a tightly constrained
internal automation genuinely spans every capability.

Administrators normally create and maintain tokens at **Config → API Tokens**.
The page provides scoped creation, optional path/device restrictions, expiry and
last-used visibility, one-click copy, atomic rotation, and revocation. Plaintext
is shown only after creation or rotation and is cleared when the one-time dialog
closes. Service tokens cannot call these management APIs, so even an `admin`
Bearer token cannot mint or revoke credentials.

The CLI remains available for recovery and automation when the Web UI cannot be
used. To create a Companion token from the TDeck folder:

```powershell
python cli.py service-tokens create "Companion Production" `
  --scope read --scope timers --scope videohub --scope tvs --scope ccb `
  --expires-in-days 365
```

Copy the printed token immediately into the existing **API token** field on the
Companion TDeck connection. TDeck cannot display it again. CLI lifecycle
commands never relist plaintext:

```powershell
python cli.py service-tokens list
python cli.py service-tokens rotate <id-or-prefix> --expires-in-days 365
python cli.py service-tokens revoke <id-or-prefix>
```

Optional least-privilege constraints can limit both actions and targets:

```powershell
python cli.py service-tokens create "Foyer TVs" `
  --scope read --scope tvs `
  --allow-path "GET /api/tvs" `
  --allow-path "POST /api/tvs/*/power" `
  --tv-target foyer-display --expires-in-days 180

python cli.py service-tokens create "Auditorium Router" `
  --scope videohub --allow-path "POST /api/videohub/route" `
  --videohub-output 1 --videohub-output 2 `
  --videohub-input 3 --videohub-input 4 --expires-in-days 180
```

Other constraints are `--videohub-preset` and `--atem-source`. Rotation
preserves the old token's scopes and constraints. Creation, use, scope denials,
rate limits, rotation, and revocation produce Activity Log security events
without recording plaintext credentials.

The browser management contract is administrator-session only:

- `GET /api/config/service-tokens` lists metadata and available scopes; it never
  returns a secret or stored hash.
- `POST /api/config/service-tokens` creates a token with `name`, optional
  `description`, `expires_in_days`, `scopes`, and optional `constraints`.
- `POST /api/config/service-tokens/<id>/rotate` atomically stores a replacement
  and revokes the old token. It accepts `name` and `expires_in_days`.
- `DELETE /api/config/service-tokens/<id>` immediately revokes a token.

Create and rotate responses contain the new plaintext once. These routes require
an administrator's Config-capable browser session; writes also require the
normal CSRF and same-origin checks.

The built-in Calendar scheduler started by `webui.py` does not call the HTTP
listener. TDeck injects a private in-process dispatcher that runs the same API
route handlers and validation with a `scheduler` principal, so scheduled API
and timer actions need no bearer token or environment variable. This principal
cannot call Admin, Config, token-management, client-telemetry, or hardware setup
APIs, and it cannot be created through a network request.

CLI commands and a separately launched `cli.py start calendar` process are not
in-process components. If they call the secured Web UI, they remain external API
clients and read a scoped token from `TDECK_INTERNAL_API_TOKEN`. Do not run that
standalone scheduler alongside `webui.py`, because duplicate schedulers can
duplicate cues.

### Deployment migration sequence

1. Back up `auth.db` through the normal Config export.
2. Create a separate token for each external integration, such as each Companion
   connection; do not share credentials between consumers.
3. Put the Companion token in its existing API-token field. The built-in
   scheduler requires no token.
4. Restart the relevant services, verify status polling and one harmless action,
   then confirm **Last used** at **Config → API Tokens**.
5. Rotate a token to practise the recovery procedure, update the consumer with
   the newly displayed plaintext, verify it, then revoke the previous token if
   it was not rotated through TDeck's token manager.

For a short migration only, an administrator may set both fields below. The
expiry is mandatory and must be no more than 31 days in the future:

```json
{
  "api_legacy_anonymous_enabled": true,
  "api_legacy_anonymous_until": "YYYY-MM-DDTHH:MM:SS+10:00"
}
```

This flag permits only the previous Companion status/home/timer/VideoHub/TV/CCB
contract. It never permits Config, Admin, browser telemetry, ATEM, Pixie,
DiGiCo, ProPresenter, Calendar editing, or newly added endpoints. Every accepted
legacy request emits a recurring Activity Log warning. Remove the flag as soon
as all consumers show token `last used` timestamps.

Security-related limits can be adjusted with `api_max_request_bytes` (default
2 MiB), `api_upload_max_request_bytes` (default 64 MiB),
`api_write_rate_limit_per_minute` (default 600 per user/token), and the optional
exact-origin list `api_trusted_origins`. Cross-origin API access is not enabled.

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

## Hisense / VIDAA TVs

TV control requires a Config-authorized browser session or a service token with
the `tvs` scope. Pairing and configuration remain restricted to Config access;
normal control endpoints are available to a scoped TDeck Companion token.

The normal **Config → TVs** workflow only requires each TV's name, IP/host, and television MAC address. Authentication mode, polling/reconnect intervals, and certificate selection are backend-managed. Legacy configuration keys remain accepted for upgrades and API compatibility.

- **GET** `/api/tvs` — list ordered TVs, ordered groups, Companion target IDs, compatible-model notes, and cached connection, power, `powerOnPending`, volume, mute, source, model, protocol, authentication, certificate-profile, and error state.
- **GET** `/api/tvs/<tv_id>/state` — get one TV's cached state.
- **POST** `/api/tvs/<tv_id>/power` with `{ "state": "on" | "off" | "toggle" }`. Power-on responses include `pending` and `confirmed`; a successful Wake-on-LAN send remains pending until VIDAA reports the TV on.
- **POST** `/api/tvs/<tv_id>/volume` with `{ "level": 20 }` for absolute volume, or `{ "action": "up" | "down" | "mute" }`.
- **POST** `/api/tvs/<tv_id>/source` with `{ "source": "HDMI1" }`. Discovered display names such as `HDMI 1` are also accepted.
- **POST** `/api/tvs/<tv_id>/reconnect` — discard the current TV connection and reconnect it.
- **GET** `/api/tv-targets/<target_id>/state` — get aggregate state for `tv:<tv_id>` or `group:<group_id>`. A legacy unprefixed TV ID is also accepted.
- **POST** `/api/tv-targets/<target_id>/power|volume|source|reconnect` — use the individual-route payloads; group commands fan out in configured member order.
- **GET/PUT** `/api/hisense/config` — protected TV configuration API used by **Config → TVs**.
- **POST** `/api/tvs/<tv_id>/pair/request` — protected; show a new PIN on the TV.
- **POST** `/api/tvs/<tv_id>/pair/submit` with `{ "pin": "1234" }` — protected; approve TDeck on the TV.

Power-on sends Wake-on-LAN using each configured MAC address. Commands are serialized per TV and each TV reconnects independently in the background. Group `connected` means every enabled member is connected; power/source/volume report a shared value only when members agree and otherwise report mixed state.

For newer dynamic authentication, each TV may also have a case-sensitive `uuid`. This is the UUID/MAC of a client device paired through the official VIDAA app; it is separate from the television's `mac`, which is used for Wake-on-LAN. Operational status exposes only `uuidConfigured`, not the UUID value.

Group membership is exclusive: a TV can belong to one ordered group or remain ungrouped. If a raw config/API payload assigns a TV to more than one group, the first group in configured order wins.

---

## Pixie Controls

Pixie endpoints are intentionally stricter than most trusted-LAN APIs. They require a logged-in TDeck session and the relevant page permission when authentication is enabled. Mutating endpoints also require the session CSRF token in `X-CSRF-Token`. Auditorium/device and scene grants are enforced server-side.

- **GET** `/api/pixie/state` — return only the auditoriums and scenes available to the current user. Add `auditorium_id=<id>` to return that auditorium's accessible devices. Add `refresh=1` to refresh local inventory first. Device `online`, `brightness`, and `on` feedback are populated from the process-wide one-second Pixie Home status cache; `online` is `null` when reachability is unavailable. `reachabilityAvailable` and `reachabilityStale` describe the status cache without affecting direct Gateway control.
- **POST** `/api/pixie/devices/brightness` with `{ "auditorium_id": "main", "device_ids": ["120", "233"], "level": 50, "final": false }` — control individually validated physical devices. On/Off devices are skipped at levels 1–99. Set `final=true` for the released/final fader value so the change is recorded in Activity Log.
- **POST** `/api/pixie/scenes/<scene_id>/activate` — activate an allowed, enabled inventory scene through the Gateway's native scene operation.
- **GET/PUT** `/api/pixie/config` — protected Config API for connection settings, auditorium/device arrangement, display names, control-type overrides, and scene ordering/visibility. PUT saves and restarts the Pixie service.
- **POST** `/api/pixie/discover` — protected, passive Gateway advertisement discovery.
- **POST** `/api/pixie/homes` — protected account-assisted Home listing used by Pixie Setup. Credentials are never returned.

`pixie_secrets.json` stores the retained account email/password locally and is ignored by Git and config export. Connection identifiers and the auditorium/device/scene arrangement live in `config.json`. Native Pixie group control is never exposed or transmitted; auditorium operations fan out as physical-device commands.

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

### Scheduler health
- **GET** `/api/scheduler_status`
- **Returns:** scheduler worker/file-watcher liveness, last heartbeat and processed trigger, last trigger result, next queued trigger, queue size, reload state, and any worker/watcher error.
- **Notes:** `running=true` is based on the actual worker thread rather than the presence of an app object. `healthy=true` additionally requires a current heartbeat and live events/config watcher.

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
  - Hardware refreshes run in the background. A cold or expired cache can return `refreshing: true` with cached/fallback data; clients may retry until it becomes false.
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

## DiGiCo Personal Mixes

Unlike legacy Companion-facing APIs, every DiGiCo endpoint enforces login and page access when authentication is enabled. Mixer endpoints also enforce the current user's group AUX allow-list.

### Mixer layout and status

- **GET** `/api/digico/mixer/config`
- Returns enabled/allowed AUXes, enabled channels, snapshot, cache revision and connection status.

### Read an AUX mix

- **GET** `/api/digico/aux/<aux>/state`
- Queues desk queries and returns the currently cached channel send on/off states, levels and pans for the AUX.
- An optional `?revision=<number>` returns a compact `unchanged: true` response when the desk cache has not changed, avoiding repeated full channel payloads.

### Change a channel send

- **POST** `/api/digico/aux/<aux>/channel/<channel>/level`
- **POST** `/api/digico/aux/<aux>/channel/<channel>/pan`
- **POST** `/api/digico/aux/<aux>/channel/<channel>/on`
- Body: `{ "value": -12.5, "final": true }` for level, `{ "value": 0.5, "final": true }` for pan, or `{ "value": false, "final": true }` for send on/off.
- Level is clamped to `-150..10` dB; pan is clamped to `0..1`; on/off accepts a boolean or an equivalent `0`/`1` value. The browser uses `final` to avoid logging every intermediate fader event.

### Setup and diagnostics

- **GET / POST** `/api/digico/setup` (Config page permission)
- **POST** `/api/digico/restart` (Config page permission)
- **POST** `/api/digico/discover` (Config page permission)
- **GET** `/api/digico_status`

## System / Status

### Config
- **GET** `/api/config`
- **POST** `/api/config` (merge provided keys into `config.json`)

### Status indicators
- **GET** `/api/companion_status`
- **GET** `/api/propresenter_status`
- **GET** `/api/videohub_status`
- **GET** `/api/digico_status`

---

## Activity Log

### Fetch historical activity
- **GET** `/api/activity-log`

### Fetch live activity
- **GET** `/api/activity-log/live`
