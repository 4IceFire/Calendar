# TDeck Codebase Feature Report

This report maps the current codebase by feature, explains how each feature works end to end, and calls out places where code looks unused, legacy, or mismatched.

## 1. High-Level Architecture

The app is centered around `webui.py`, which acts as the main Flask server, HTML page router, and most API surface.

The main feature areas are:

1. Calendar scheduler and event triggers
2. Auth, roles, admin, config, home, and console
3. Timer presets, ProPresenter control, and Companion timer sync
4. Transcription sender/server/display flow
5. VideoHub routing, presets, and room layout management

Core persistence is file-based:

- `config.json`: app configuration
- `events.json`: calendar events and trigger definitions
- `timer_presets.json`: timer presets
- `button_templates.json`: button templates
- `trigger_templates.json`: trigger templates
- `videohub_presets.json`: VideoHub presets
- `videohub_rooms.json`: VideoHub room/layout config
- `transcription_keyword_rules.json`: spoken keyword rules
- `auth.db`: users, roles, page access, and related auth data

## 2. Calendar / Schedule Feature

### What is implemented

The schedule feature lets the user create events, attach one or more triggers, persist them to JSON, and have a background scheduler execute them at the correct time.

Core files:

- [models.py](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/package/apps/calendar/models.py>)
- [storage.py](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/package/apps/calendar/storage.py>)
- [scheduler.py](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/package/apps/calendar/scheduler.py>)
- [cli.py](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/cli.py>)
- [webui.py](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/webui.py>)
- [calendar.html](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/templates/calendar.html>)
- [calendar_new.html](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/templates/calendar_new.html>)
- [calendar_triggers.html](</c:/Users/dedwa/OneDrive/Daniel's Stuff/Church/Companion/Calendar/templates/calendar_triggers.html>)

Important entry points:

- `CalendarApp.start()` in `package/apps/calendar/__init__.py`
- `ClockScheduler.start()` in `package/apps/calendar/scheduler.py`
- `/api/ui/events`, `/api/events/<id>`, `/api/upcoming_triggers` in `webui.py`
- `calendarctl list/show/add/edit/remove/enable/disable/trigger` in `cli.py`

### Data model

An event contains:

- `id`
- `name`
- `day`
- `date`
- `time`
- `repeating`
- `active`
- `times` list of triggers

Each trigger contains:

- `minutes`
- `typeOfTrigger`: `BEFORE`, `AT`, or `AFTER`
- `enabled`
- `uid`
- `name`
- `actionType`
- either `buttonURL`, `api`, or `timer`

The trigger model converts the human trigger type into a signed offset:

- `BEFORE` -> negative minutes
- `AT` -> zero
- `AFTER` -> positive minutes

### End-to-end flow

#### Create or edit an event

1. The user opens `/calendar/new` or `/calendar/edit/<id>`.
2. `calendar_new.html` collects event fields and trigger rows.
3. The browser posts JSON to:
   - `POST /api/ui/events` for a new event
   - `PUT /api/events/<id>` for an existing event
4. `webui.py` validates the payload, normalizes trigger action specs, and builds `Event` and `TimeOfTrigger` objects.
5. `storage.save_events(...)` writes the normalized list back to `events.json`.

#### Load events

1. The UI calls `GET /api/ui/events`.
2. The scheduler calls `storage.load_events_safe(...)`.
3. `storage.py` reads `events.json`.
4. Missing fields are backfilled:
   - event `id`
   - event `active`
   - trigger `uid`
   - trigger `enabled`
   - trigger `actionType`
   - missing `api`, `timer`, or `buttonURL`
5. If defaults were missing, `storage.py` writes the corrected shape back to disk.

#### Schedule execution

1. The background calendar app starts `ClockScheduler`.
2. The scheduler watches both the events file and `config.json`.
3. On reload it:
   - reads all events
   - skips inactive events
   - finds the next relevant occurrence for each event
   - calculates due times for each enabled trigger
   - pushes future jobs into a heap
4. It writes a runtime snapshot to `calendar_triggers.json`.
5. The execution loop waits for the next due job and dispatches it.

#### Trigger execution

Each trigger is routed by `actionType`:

- `companion`: POST to Bitfocus Companion using `buttonURL`
- `api`: internal HTTP call to the app’s own `/api/*` endpoints
- `timer`: translated into an internal `POST /api/timers/preset`

### Trigger timing logic

For one-off events:

- If the event base time is still in the future, schedule it.
- If the event base time is in the past, keep it only if at least one trigger due time is still in the future.

For repeating events:

- The scheduler prefers the most recent weekly occurrence if that occurrence still has future trigger(s).
- Otherwise it advances to the next week.
- After the last trigger for one repeating occurrence fires, the scheduler reschedules the next weekly occurrence.

One subtle behavior:

- Jobs are only enqueued when `due > now`, so a trigger that lands exactly on rebuild time can be skipped.

### Manual trigger flow

Manual trigger firing exists in the CLI, not the web UI:

1. User runs `calendarctl trigger <id|name> --which N`
2. `cli.py` loads events from `events.json`
3. It finds the event
4. It selects the Nth trigger
5. It posts `trigger.buttonURL` to Companion if connected

### Storage used

- `events.json`: source of truth
- `calendar_triggers.json`: generated snapshot of upcoming jobs
- `trigger_templates.json`: reusable trigger bundles
- `button_templates.json`: named button mappings

### Unused / stale / risky pieces

- `cli.py` only properly understands legacy Companion-style triggers. Editing events through CLI can strip newer `api` and `timer` trigger payloads.
- `cli.py show` and `cli.py trigger` assume `buttonURL`, so newer trigger types are not fully supported there.
- `storage.events` module-level cache appears legacy and unused.
- `scheduler.py` uses `Path.cwd()` for some files while other code uses repo-relative paths, so running from a different working directory could desync labels or snapshots.
- `calendar_triggers.json` is only a snapshot. If the scheduler is not running, the CLI can show stale trigger data.

## 3. Auth, Roles, Admin, Config, Home, and Console

### What is implemented

This part of the app handles:

- login/logout
- password change
- session cookies
- idle timeout
- CSRF
- role-based page access
- role/user admin pages
- config editor
- home dashboard
- console page for running `cli.py` commands

Core files:

- `webui.py`
- `templates/base.html`
- `templates/login.html`
- `templates/account_password.html`
- `templates/admin_roles.html`
- `templates/admin_users.html`
- `templates/config.html`
- `templates/home.html`
- `templates/console.html`
- `static/app.js`
- `auth.db`

### End-to-end auth flow

1. User opens a page route.
2. `_auth_gate()` in `webui.py` runs before the request.
3. It skips static paths and most `/api/*` routes.
4. For protected pages it:
   - checks login
   - checks idle timeout
   - checks CSRF for mutating page requests
   - checks page permission by role
5. Flask-Login loads the user from `auth.db`.
6. `base.html` renders only the nav links the current user can access.

### Role and page permission flow

1. A route uses `@require_page('page:key', 'Friendly Name')`.
2. `require_page()` tags the route with the page key.
3. `_auth_gate()` looks at that page key.
4. `can_access()` checks whether the user’s role is allowed that page.
5. Admin is treated as allow-all.

Important nuance:

- `require_page()` itself does not enforce auth.
- Real enforcement happens in `_auth_gate()`.
- Most `/api/*` endpoints are intentionally outside that global page gate.

### Admin roles flow

1. User opens `/admin/roles`.
2. The page loads roles, page keys, and VideoHub-specific role settings.
3. JS in `static/app.js` autosaves role changes to `/api/admin/roles/<id>`.
4. The server updates:
   - `role_pages`
   - VideoHub allowed outputs/inputs
   - VideoHub allowed presets
   - VideoHub edit toggle

### Admin users flow

1. User opens `/admin/users`.
2. Server loads current users and roles from `auth.db`.
3. Form submissions create users, change role, toggle active, reset password, or delete users.
4. Guardrails prevent deleting yourself and prevent deleting the last active Admin.

### Config page flow

1. Browser loads `/config`.
2. `static/app.js` calls `GET /api/config`.
3. It renders groups based on `CONFIG_META`.
4. On save it posts the full config to `POST /api/config`.
5. The server writes the updated `config.json`.

The config page also contains the transcription setup test, which calls `/api/transcription/config/test`.

### Home dashboard flow

The home page is a summary view over other features. It does not own data itself.

It reads from:

- `events.json`
- `calendar_triggers.json`
- timer state helpers
- VideoHub state helpers
- `home_state.json`

It shows:

- upcoming event data
- connection status badges
- timer quick-apply controls
- last/next helper state

### Console page flow

The console page is not a generic system shell.

Flow:

1. Browser loads `/console`.
2. JS polls `/api/console/logs`.
3. When the user runs something, the browser posts to `/api/console/run`.
4. `webui.py` runs `subprocess.run([python, cli.py, ...])`.
5. Output is appended to the console log stream.

### Storage used

- `auth.db`: users, roles, role pages, audit
- `config.json`: app config and auth settings
- `home_state.json`: small dashboard convenience state

### Unused / stale / risky pieces

- Per-role idle timeout override exists in schema/helper code but is not wired into actual `_auth_gate()` timeout enforcement.
- Some legacy config aliases still exist for compatibility, like `server_port` and `timer_index`.
- Many `/api/*` endpoints are outside the global auth gate, so protection depends on endpoint-level checks and UI-level hiding.
- The console page only launches `cli.py` commands, despite looking like a broader console.

## 4. Timers / ProPresenter / Companion Glue

### What is implemented

This feature lets the user maintain timer presets, sync their labels to Companion variables, and push selected times into ProPresenter timers.

Core files:

- `webui.py`
- `package/apps/calendar/utils.py`
- `propresentor.py`
- `companion.py`
- `cli.py`
- `timer_presets.json`
- `templates/timers.html`
- `templates/home.html`
- `static/app.js`

### Preset CRUD flow

1. Browser loads `/timers`.
2. JS calls `GET /api/timers`.
3. The server reads `timer_presets.json`.
4. Presets are normalized by utility helpers.
5. The page auto-saves edits through `POST /api/timers`.

Bulk save behavior:

- replaces the whole preset list
- validates `HH:MM`
- limits preset count and button-press count
- blocks list shrink unless `allow_delete=true`
- persists `stream_start_preset`
- persists `propresenter_timer_index`
- best-effort syncs Companion custom variables

Single preset save behavior:

- `POST|PATCH /api/timers/preset`
- updates one preset
- may apply immediately if `apply=true`

### Apply preset flow

1. User presses Apply from Home, Timers page, CLI, or a scheduler timer trigger.
2. Browser or scheduler calls `POST /api/timers/apply` or `POST /api/timers/preset`.
3. The server resolves the preset number as 1-based.
4. It optionally fires configured Companion button presses.
5. It sets the target ProPresenter timer.
6. It resets it.
7. It starts it.

Legacy mode:

- If `propresenter_is_latest` is false, it uses `stop -> set -> reset -> start` with configurable waits.

### Stage message flow

The app also wraps ProPresenter stage message APIs:

- send stage message
- clear stage message
- derive a “stream start” message from the configured timer preset

`/api/propresenter/stage/stream_start` builds a message like `STREAM 9:30AM` from `stream_start_preset`.

### Companion sync flow

On timer save, the app writes Companion variables using:

- prefix from `companion_timer_name`
- 1-based preset number

Example:

- `timer_name_1`
- `timer_name_2`

The stored value is formatted as label plus a pretty time string.

### Scheduler interaction

Calendar triggers can store `actionType: "timer"`.

Flow:

1. Scheduler sees a due timer trigger.
2. It converts the trigger payload into an internal `/api/timers/preset` call.
3. The timer endpoint updates and optionally applies the timer.

### Storage used

- `timer_presets.json`: canonical timer preset store
- `config.json`: timer/ProPresenter/Companion settings

### Unused / stale / risky pieces

- `companion.py.GetVariable()` appears unused.
- Several ProPresenter wrapper methods in `propresentor.py` appear unused.
- Timer-action job scaffolding in `webui.py` appears unused.
- High-confidence bug: `_apply_timer_preset_number()` likely fires preset Companion button presses twice when ProPresenter is available.
- Default fallback ports are inconsistent across `webui.py`, `utils.py`, `companion.py`, and config.
- Some endpoints may return `ok: true` while still embedding partial failure details in the payload.

## 5. Transcription Feature

### What is implemented

The transcription stack supports:

- remote sender setup UI
- remote microphone upload
- server-side transcription
- live operator view
- live display view
- keyword action rules
- pause markers
- optional session history
- SSE state streaming

Core files:

- `package/apps/calendar/transcription_service.py`
- `tools/transcription_sender.py`
- `tools/transcription_sender_lib.py`
- `tools/transcription_sender_ui.py`
- `webui.py`
- `templates/transcription.html`
- `templates/transcription_display.html`
- `templates/transcription_actions.html`
- `static/app.js`
- `transcription_keyword_rules.json`

### End-to-end flow

#### Sender setup

1. On the capture computer, the operator runs the sender UI.
2. They enter:
   - server URL
   - ingest token
   - source name
   - microphone choice
3. Settings are stored in `transcription_sender_config.json`.

#### Audio ingest

1. `SenderService.start()` opens a local input stream.
2. Audio chunks are queued and uploaded to `/api/transcription/audio`.
3. Request headers include token and source metadata.
4. `webui.py` validates the ingest token.
5. The server normalizes PCM if needed and feeds `TranscriptionService.ingest_audio()`.

#### Live transcription

1. Realtime callbacks update live and stabilized text.
2. Finalized text is appended into transcript segments.
3. Clients subscribe to `/api/transcription/stream` using `EventSource`.
4. The server pushes full state on change.

### Keyword action flow

1. Rules are stored in `transcription_keyword_rules.json`.
2. CRUD happens through `/api/transcription/keyword-rules`.
3. Matching happens only on finalized text.
4. Match style is simple case-insensitive substring inclusion.
5. The built-in action is `flash_red_twice`.
6. The display page reacts to emitted action events.

No cooldown exists, so repeated mentions retrigger.

### Pause and history flow

- Soft/hard pause markers are inserted based on configured speech gaps.
- Session history is optionally archived into `transcription_sessions.json`.
- Recent action events are in-memory only and are not persisted.

### State model

The service exposes separate `server_state` and `client_state`.

Server state reflects things like:

- feature enabled/off
- dependency missing
- recorder error
- ready
- idle

Client state reflects:

- waiting
- streaming
- paused
- disconnected

### Unused / stale / risky pieces

- `/api/sender/shutdown` in the sender UI appears unused by the normal launcher flow.
- The CLI sender is a fallback path, not the main intended workflow.
- Several advanced transcription config fields are intentionally hidden from the normal config UI but still active in backend behavior.

## 6. VideoHub / Routing Feature

### What is implemented

The VideoHub feature includes:

- TCP transport client
- preset CRUD
- apply preset to device
- save preset from current device state
- room/layout editor
- background uploads for rooms
- routing page
- role-based UI filtering

Core files:

- `videohub.py`
- `package/apps/videohub/app.py`
- `package/apps/videohub/storage.py`
- `package/apps/videohub/models.py`
- `webui.py`
- `templates/videohub.html`
- `templates/videohub_rooms.html`
- `templates/videohub_input_select.html`
- `templates/routing.html`
- `static/app.js`
- `videohub_presets.json`
- `videohub_rooms.json`

### Transport and preset flow

`videohub.py` handles low-level TCP communication and uses 0-based protocol indexes.

The rest of the app uses 1-based human numbering.

Preset flow:

1. UI calls `/api/videohub/presets`
2. Backend loads `videohub_presets.json`
3. Create/update/delete/lock operations go through app-layer helpers
4. Apply converts stored 1-based routes to 0-based router commands
5. Save-from-device snapshots current routing back into a preset

### Room config flow

1. UI loads `/api/videohub/rooms/config`
2. Server reads `videohub_rooms.json`
3. It injects `background_url` for display use
4. UI edits rooms, output layouts, and filtered inputs
5. UI writes the full normalized config back with `PUT /api/videohub/rooms/config`

Background upload flow:

1. Browser uploads room background
2. Server validates room, extension, and size
3. File is stored in `videohub_room_images/`
4. Room config is updated
5. Old asset may be removed if replaced

### Routing page flow

1. User opens `/routing`
2. Server passes allowed outputs/inputs from the role record
3. Browser loads VideoHub state
4. Browser filters visible outputs/inputs client-side
5. User selects route
6. Browser posts `/api/videohub/route`
7. Server converts 1-based numbers to 0-based router indexes and sends the command

Important nuance:

- routing restrictions are enforced in the UI, not rechecked by the route API

### Storage used

- `config.json`: VideoHub host/port/timeout/preset file
- `videohub_presets.json`: preset definitions
- `videohub_rooms.json`: room/layout/filter config
- `videohub_room_images/`: uploaded room images
- `auth.db`: role-based VideoHub restrictions

### Unused / stale / risky pieces

- `send_raw()` in `videohub.py` appears unused.
- Backend support for monitoring routes exists, but the normal UI does not expose it.
- Preset CRUD/apply APIs are intentionally not enforcing full server-side auth/role restrictions.
- `/videohub/monitor` is public and not wrapped in `require_page(...)`.

## 7. Biggest Cross-Feature Observations

The app is mostly organized around one big Flask host plus feature-specific helper modules.

The strongest patterns are:

- page/UI flow in `templates/*.html` + `static/app.js`
- API flow in `webui.py`
- persistence in JSON or SQLite
- background scheduling in `package/apps/calendar/scheduler.py`
- device integration through thin clients like `companion.py`, `propresentor.py`, and `videohub.py`

The main architectural tradeoff is that many features are intentionally UI-gated instead of API-gated. That matches the repo notes, but it means behavior often depends on who can reach the UI rather than who can call the backend.

## 8. Most Important Unused / Suspicious Areas

These are the highest-signal items that stood out:

1. CLI calendar editing is legacy-focused and can damage newer trigger types (`api` and `timer`) by round-tripping only `buttonURL`.
2. Timer apply logic likely fires configured Companion button presses twice in at least one code path.
3. Per-role idle timeout override appears implemented in schema/helpers but not actually enforced.
4. `storage.events` cache looks unused.
5. `companion.py.GetVariable()` looks unused.
6. `videohub.py.send_raw()` looks unused.
7. Multiple ProPresenter wrapper methods appear unused.
8. VideoHub monitoring-route support exists in backend models/storage but is not surfaced in the main UI.
9. Some file-path behavior depends on `Path.cwd()`, which can cause runtime mismatches if launched from a different folder.

## 9. Suggested Reading Order

If you want to understand the app quickly, this is the best order:

1. `README.md`
2. `webui.py`
3. `package/apps/calendar/scheduler.py`
4. `package/apps/calendar/storage.py`
5. `package/apps/calendar/utils.py`
6. `static/app.js`
7. `propresentor.py`, `companion.py`, and `videohub.py`
8. feature-specific templates under `templates/`

## 10. Short Feature-by-Feature Input -> Processing -> Output Summary

### Calendar

User input:
Event form or CLI command

Processing:
`webui.py` or `cli.py` -> models -> `storage.py` -> `events.json` -> `scheduler.py`

Output:
Companion press, internal API call, or timer action at the computed due time

### Auth/Admin

User input:
Login form, roles page, users page

Processing:
`webui.py` -> `auth.db` -> session + role/page checks

Output:
Protected page access and admin-managed user/role state

### Timers

User input:
Timers page, home quick apply, CLI, or scheduler timer trigger

Processing:
`webui.py` -> `timer_presets.json` / `config.json` -> Companion variable sync -> ProPresenter timer API

Output:
Updated preset store, Companion variable updates, ProPresenter timer set/reset/start

### Transcription

User input:
Sender UI and microphone stream

Processing:
sender -> `/api/transcription/audio` -> `transcription_service.py` -> SSE stream + keyword action matching

Output:
Operator view, display view, action flashes, optional saved session history

### VideoHub

User input:
Preset editor, rooms page, routing page

Processing:
UI -> `webui.py` -> VideoHub app/storage -> TCP VideoHub client

Output:
Saved presets/rooms and applied live routes on the router
