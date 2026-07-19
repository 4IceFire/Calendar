# Repository Guidelines

This repo contains TDeck, a Python app for scheduling service cues, controlling production integrations, and serving operator/personal-control pages. Keep changes focused on the scheduler, CLI, Web UI, VideoHub and DiGiCo flows described in `README.md`.

## Project Structure & Module Organization
- `package/`: Python package code. `package/apps/calendar/` contains scheduler, storage, and utilities.
- Entry points: `webui.py` (Flask Web UI), `cli.py` (CLI), `companion.py`/`propresentor.py` (external integrations), and `digico.py` (DiGiCo OSC transport/cache/relay).
- `static/`: front-end JS/CSS assets. `templates/`: HTML templates.
- Data/config: `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, `videohub_rooms.json`, `auth.db`.
- VideoHub room images: local uploads live in `videohub_room_images/` and should remain ignored by Git.
- Logs/runtime files: `calendar.log`, `calendar_triggers.json`, and `calendar.pid`.
- Config import rollback zips live in `config_import_backups/` and should remain ignored by Git.

## Build, Test, and Development Commands
- Create venv and install deps:
  - `python -m venv .venv`
  - `pip install -r requirements.txt`
- Run Web UI: `python webui.py` (uses `webserver_port` in `config.json`).
- Run CLI:
  - `python cli.py apps` (list apps)
  - `python cli.py start calendar --background` (scheduler)
  - `python cli.py stop` (stop background scheduler)
- Docker:
  - `docker build -t tdeck-calendar:latest .`
  - `docker compose up --build`

## Coding Style & Naming Conventions
- Python: 4-space indentation, PEP 8-style naming. Use `snake_case` for functions/vars, `CapWords` for classes, `UPPER_CASE` for constants.
- JavaScript (in `static/`): prefer `camelCase` for variables and functions.
- No formatter or linter is enforced in-repo; keep changes consistent with surrounding files.

## Testing Guidelines
- Tests use the standard-library `unittest` runner: `python -m unittest discover -s tests -p "test_*.py" -v`.
- DiGiCo tests include an in-process UDP desk simulator and Flask API/page coverage; keep tests independent of real church hardware and production config.
- For manual checks: start `python webui.py`, load the UI, and run a CLI command like `python cli.py list`.
- Place new tests under `tests/` with `test_*.py` and document any additional runner in `README.md`.

## Commit & Pull Request Guidelines
- Existing commits use short, sentence-case summaries (e.g., `Updated UI`, `Added authentication to app`). Follow the same style.
- PRs should include: summary of changes, config/data file updates (`config.json`, `events.json`), and screenshots for UI changes.
- Note any migration steps or new dependencies.

## Configuration & Security Tips
- Keep secrets and environment-specific values out of Git; use `config.json` and local overrides.
- If you change `webserver_port`, update Docker port mappings (`docker-compose.yml`) accordingly.
- Keep `videohub_room_images/` out of source control; room backgrounds are local media, not repo assets.

## Config Export/Import
- Config transport lives under the normal Config page access: `/config/export` and `/config/import`.
- Do not add a separate permission for config transport; anyone who can access Config can export/import selected setup items.
- Exports are a single TDeck zip containing `manifest.json` plus selected payload files/folders.
- Exportable setup items include `config.json`, the configured events file, `timer_presets.json`, `trigger_templates.json`, `button_templates.json`, `calendar_triggers.json`, `companion_surfaces.json`, the configured VideoHub presets file, `videohub_rooms.json`, `home_state.json`, `auth.db`, and optional `videohub_room_images/` media.
- VideoHub room backgrounds depend on both `videohub_rooms.json` and `videohub_room_images/`; selecting VideoHub rooms should automatically carry room media when that media exists in the export/import package.
- When importing `videohub_room_images/`, keep the folder itself and replace its contents; deleting/recreating the root folder can fail with access denied on Windows/OneDrive.
- Import inspects the zip first, then lets the user choose which contained items to overwrite on the target instance.
- Imports overwrite selected files/folders instead of merging. Before replacing anything, the app creates a timestamped rollback zip in `config_import_backups/`.
- Config transport actions should log to the server console and persistent Activity Log with a `[CONFIG]` prefix.
- The old standalone Auth DB backup UI should stay removed; user/group/session data is transported via the `auth.db` item in Config export/import.

## Activity Logging
- The user-facing log system is the persistent Activity Log, shown at `/console` for route/backward-compatibility but titled Activity Log in the UI.
- Use `log_event(...)` in `webui.py` for every user-visible state change, external action, permission/security event, config transport action, and hardware/control action.
- Let `log_event(...)` infer the logged-in actor from `current_user` whenever possible. For non-user actions, set `source` to one of `web`, `api`, `companion`, `scheduler`, or `system`.
- Use stable dotted action names such as `videohub.preset.apply`, `config.import`, `user.password_reset`, `timers.preset.apply`, and `propresenter.timer.start`.
- Use `status` values `success`, `failure`, `warning`, or `info`.
- Put human-readable text in `summary`; put structured context in `details` so the Activity Log can show expandable diagnostic data.
- Never log secrets, passwords, tokens, session cookies, CSRF values, or raw credentials. The helper redacts common sensitive keys, but callers should still avoid passing secrets.
- Do not use `print(...)`, `_console_append(...)`, or raw Python logging as the primary user-facing activity record. Keep `calendar.log` and Python logging for low-level runtime diagnostics only.

## Auth Model Notes
- UI pages are protected by group-based page access in the Web UI (`require_page` checks).
- Users can belong to multiple groups. A user's effective page permissions are the union of all non-admin groups they belong to.
- The `Admin` group is the only protected full-access group. It grants every page and management permission and should remain non-deletable.
- Legacy `roles` / `role_pages` data may still exist in `auth.db` only as a migration source. New permissions work should use `groups`, `group_pages`, and `user_groups`.
- User management lives on `/admin/permissions` for browsing/creating users and groups, and `/admin/users/<id>` for per-user profile, access, security, sessions, and activity management.
- Account security state lives on the `users` table: email/full name, active/locked status, failed login count, force-password-change flag, password timestamps, and session version.
- Logged-in sessions are tracked in `user_sessions`; revoking sessions or forcing password changes should use that table/session-version flow.
- API endpoints are intentionally callable without login unless an endpoint is explicitly marked otherwise.
- DiGiCo mixer/setup APIs are an explicit exception: they control live audio and must enforce login, the relevant page permission, enabled routes, and the per-group AUX allow-list on the server.
- For VideoHub preset visibility, enforce group restrictions in the UI (hide non-allowed preset IDs) and do not add API auth/authorization checks for this behavior.
- VideoHub room metadata is global for all presets and users. Access control applies to who can manage the room layout UI, not to the room data itself.

## ATEM Record Audio Controls
- The Record Audio page controls a Blackmagic ATEM 4 M/E Broadcast Studio 4K used as an audio switcher. The page route remains `/foyer-audio` and the nav label is `Record Audio`.
- Keep ATEM code in separate integration files, matching the repo's external-integration style:
  - `atem.py`: PyATEMMax-backed control/state wrapper for volume, ON/mix option, solo, monitor dim/mute/volume, labels, and source discovery.
  - `atem_meter.py`: independent legacy UDP metering client for `SALN`/`AMLv`; do not enable PyATEMMax audio level streaming for this switcher.
- Config keys live in `config.json` / the Config page:
  - `atem_ip` defaults to `127.0.0.1`
  - `atem_port` defaults to `9910`
  - `atem_timeout` defaults to `3`
- Dependency: `PyATEMMax` is used for ATEM controls. Metering is implemented locally because PyATEMMax's `AMLv` parser can crash with this older ATEM model.
- ATEM source labels should come from the switcher when available. Fallback source IDs include master, inputs 1-20, XLR 1001, AES/EBU 1101, RCA 1201, and media players.
- Group permissions live on `groups`:
  - `atem_allowed_audio_sources`: JSON list of allowed source IDs; `master` is the master volume ID. Empty list/no checked channels means no audio strips for non-admin users.
  - `atem_can_solo_audio`: allows headphone solo buttons.
  - `atem_can_monitor_audio`: allows monitor On/Dim/Volume controls.
- A user in any admin group (`groups.is_admin=1`, normally the protected `Admin` group) can always access Record Audio and see/control every source, solo, and monitor control. This is group-based, not tied to the username `admin`.
- Non-admin users only see the union of source IDs granted by their groups. Solo and monitor permissions also union across groups.
- Monitor On/Off in TDeck must use `setAudioMixerMonitorMute(...)`, inverted so On means `mute=False` and Off means `mute=True`. Do not use `setAudioMixerMonitorMonitorAudio(...)` for the TDeck On button, because that disables the monitor path and can break solo / route normal audio through headphones loudly on this switcher.
- The monitor controls are intentionally styled like TDeck controls, not like the ATEM Software Control panel.
- The Record Audio UI polls `/api/atem/audio/state` frequently so hardware/ATEM Software Control changes update TDeck live. Keep slider updates responsive but throttled to avoid flooding the switcher.
- `/foyer-audio/debug` is intentionally kept for production diagnosis. It reports effective permissions, ATEM sources, metering status, packet counters, and current levels. It is protected by Record Audio page access.
- API auth in this repo is intentionally light unless explicitly guarded. The monitor control endpoint has a server-side permission guard; source visibility/solo permissions are primarily enforced in the UI.

## VideoHub Group Controls (Where To Look)
- Storage: group settings live in `auth.db` table `groups` and are migrated/used in `webui.py`.
- Preset storage remains in `videohub_presets.json`; global room metadata is stored separately in `videohub_rooms.json`.
- Room background uploads are served from `/media/videohub_room_images/<filename>` and stored in `videohub_room_images/`.
- Routing page allow-lists (per group):
  - Columns: `videohub_allowed_outputs`, `videohub_allowed_inputs`
  - Semantics: blank/NULL/"all" => allow all; otherwise JSON list or CSV of 1-based port numbers.
  - UI: configured on the Groups tab of `/admin/permissions`; enforced on `/routing` page.
  - If a user has multiple groups, blank/all in any applicable group means all ports are allowed; otherwise restricted lists are unioned.
- VideoHub presets visibility (per group, UI-only):
  - Column: `videohub_allowed_presets`
  - Semantics: blank/NULL/"all" => all presets visible; otherwise JSON list or CSV of 1-based preset IDs.
  - UI config: `templates/admin_permissions.html` + autosave payload in `static/app.js`.
  - Enforcement: only in the VideoHub page UI (the `/api/videohub/presets*` endpoints remain unauthenticated by design).
- VideoHub preset editing toggle (per group, UI-only):
  - Column: `videohub_can_edit_presets` (INTEGER, default allow when NULL for backward compatibility).
  - Meaning: when off, VideoHub page allows viewing/applying presets but disables create/save/delete/lock and room-based routing edits.
  - UI config: checkbox on the Groups tab of `/admin/permissions`; autosave in `static/app.js`.
  - Enforcement: `webui.py` passes `can_edit_presets` into `templates/videohub.html` via `data-can-edit-presets`.
- VideoHub Rooms management:
  - The room editor is a separate page at `/videohub/rooms`, but it is not a page-access permission that can be assigned independently in Access Levels.
  - Access is derived from existing VideoHub access plus `videohub_can_edit_presets`.
  - Keep this behavior intact: users with VideoHub access but without edit permission can still view/apply presets on `/videohub`, but cannot manage rooms.
- Current VideoHub UI structure:
  - `/videohub`: room-based preset editor and viewer.
  - `/videohub/input-select`: dedicated input grid used when changing a single output route.
  - `/videohub/rooms`: global room/background/output-position/input-filter management.
  - `templates/videohub.html`, `templates/videohub_rooms.html`, and `templates/videohub_input_select.html` are the main templates for this flow.
- Room-layout semantics:
  - Rooms are global, shared across all presets.
  - An output can belong to only one room; outputs with no room assignment appear under `Unassigned`.
  - Room pages control output placement and background image only; they do not save routing.
  - Preset editing stages routing changes in the UI and only persists them when the user clicks Save Preset.
- Input filter semantics:
  - Filtered inputs are global and stored in `videohub_rooms.json`.
  - Input selection defaults to the filtered list and can toggle to show all inputs.

## Companion Surface Embeds
- Surface definitions live in `companion_surfaces.json`.
  - Use the object format with `surfaces` and `surface_controls`.
  - `surfaces` is the reusable catalogue; each entry should include `id`, `label`, and `layout`.
  - `layout` is the Companion button-grid size as rows x columns, such as `3x5` or `2x5`. TDeck uses it to calculate the iframe display box.
  - `surface_controls` represents the hardcoded display slots on the standalone `/surface-controls` page; each entry should include `surface_id`, `label`, and `size`.
  - Display `label` values describe where that surface is being shown in TDeck. They are for config/admin clarity and should not be rendered above the surface unless the containing page explicitly wants labels.
  - Do not store crop settings in `companion_surfaces.json`; adjust the surface `layout` and display `size` instead.
  - A surface can appear multiple times in `surface_controls` with different display labels or scale values.
  - `surface_id` maps to the Bitfocus Companion surface ID and is embedded as `/emulator/<surface_id>`.
  - The Companion base URL uses `companion_ip` / `companion_port` from `config.json`; optional `companion_surface_ip` / `companion_surface_port` override keys are supported if a separate endpoint is ever needed.
- Reusable UI lives in `templates/_companion_surface.html`.
  - Import it with `{% from '_companion_surface.html' import companion_surface with context %}` so the macro can access the Companion URL helper.
  - Render with `{{ companion_surface(surface, can_click=can_click_companion_surface(surface.id)) }}` or pass per-display overrides such as `width`, `height`, and `size`.
  - The macro renders only the surface iframe/blocker; page labels/layout belong in the containing template.
- `/surface-controls` is the test page and renders every configured surface.
- `/config/companion-surfaces` is the TDeck editor for `companion_surfaces.json`.
  - It is protected by the normal Config page permission.
  - The backing API is `/api/companion-surfaces-config`.
  - The editor auto-saves shortly after changes and only shows visible status for validation/save errors; `/surface-controls` picks up layout changes on refresh.
  - The editor can add/remove surfaces from the catalogue, but should not add/remove/reorder `/surface-controls` display slots. Those slots are page-owned and hardcoded by the display entries already in `companion_surfaces.json`.
- Group click permissions live on `groups.companion_click_surfaces`.
  - Blank/NULL/`[]` means the group can click all configured surfaces.
  - A non-empty JSON list restricts clicking to those surface IDs.
  - Admin can always click every surface.
  - These permissions only block pointer/touch/keyboard interaction in TDeck's iframe UI. They do not secure Companion directly if a user opens Companion outside TDeck.
- Viewing a surface is controlled by the containing page's normal page permission. The surface click check should not be used as a view permission.

## DiGiCo Personal Mixes
- `digico.py` owns the one process-wide UDP socket, OSC codec, desk discovery/cache, heartbeat and optional external-device relay.
- DiGiCo can send address-only OSC packets without a type-tag string; the decoder must continue accepting these packets.
- `/personal-mixes` uses the `page:digico_mixer` page permission; `/config/digico` uses the normal Config permission.
- `groups.digico_allowed_auxes` stores JSON string IDs. Blank/NULL/`[]` means all enabled AUXes; otherwise multiple group lists are unioned, with any unrestricted applicable group granting all.
- Never rely on hidden browser controls for AUX authorization. Enforce the scope on all read and write mixer endpoints.
- Config lives in the main `config.json`: connection/listen/timing scalar keys plus `digico_auxes`, `digico_channels`, and `digico_external_devices` arrays. It is already included in normal config transport.
- Native relay input is accepted only from configured external device IPs. Disabled devices must not relay; loopback is off by default.
- Keep browser polling non-blocking and bounded. The threaded Flask server plus one backend cache is intentional so many phones do not multiply desk traffic.
- Log setup, restart, discovery, permission and final mixer changes with `log_event(...)`; do not log every intermediate fader drag.
