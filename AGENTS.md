# Repository Guidelines

This repo contains TDeck, a Python app for scheduling service cues and firing Bitfocus Companion button presses. Keep changes focused on the scheduler, CLI, Web UI, and VideoHub flow described in `README.md`.

## Project Structure & Module Organization
- `package/`: Python package code. `package/apps/calendar/` contains scheduler, storage, and utilities.
- Entry points: `webui.py` (Flask Web UI), `cli.py` (CLI), `companion.py`/`propresentor.py` (external integrations).
- `static/`: front-end JS/CSS assets. `templates/`: HTML templates.
- Data/config: `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, `videohub_rooms.json`, `auth.db`.
- VideoHub room images: local uploads live in `videohub_room_images/` and should remain ignored by Git.
- Logs/runtime files: `calendar.log`, `calendar_triggers.json`, and `calendar.pid`.

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
- No automated test framework is configured in this repo.
- For manual checks: start `python webui.py`, load the UI, and run a CLI command like `python cli.py list`.
- If you add tests, place them under `tests/` with `test_*.py` and document the runner in `README.md`.

## Commit & Pull Request Guidelines
- Existing commits use short, sentence-case summaries (e.g., `Updated UI`, `Added authentication to app`). Follow the same style.
- PRs should include: summary of changes, config/data file updates (`config.json`, `events.json`), and screenshots for UI changes.
- Note any migration steps or new dependencies.

## Configuration & Security Tips
- Keep secrets and environment-specific values out of Git; use `config.json` and local overrides.
- If you change `webserver_port`, update Docker port mappings (`docker-compose.yml`) accordingly.
- Keep `videohub_room_images/` out of source control; room backgrounds are local media, not repo assets.

## Auth Model Notes
- UI pages are protected by group-based page access in the Web UI (`require_page` checks).
- Users can belong to multiple groups. A user's effective page permissions are the union of all non-admin groups they belong to.
- The `Admin` group is the only protected full-access group. It grants every page and management permission and should remain non-deletable.
- Legacy `roles` / `role_pages` data may still exist in `auth.db` only as a migration source. New permissions work should use `groups`, `group_pages`, and `user_groups`.
- User management lives on `/admin/permissions` for browsing/creating users and groups, and `/admin/users/<id>` for per-user profile, access, security, sessions, and activity management.
- Account security state lives on the `users` table: email/full name, active/locked status, failed login count, force-password-change flag, password timestamps, and session version.
- Logged-in sessions are tracked in `user_sessions`; revoking sessions or forcing password changes should use that table/session-version flow.
- API endpoints are intentionally callable without login unless an endpoint is explicitly marked otherwise.
- For VideoHub preset visibility, enforce group restrictions in the UI (hide non-allowed preset IDs) and do not add API auth/authorization checks for this behavior.
- VideoHub room metadata is global for all presets and users. Access control applies to who can manage the room layout UI, not to the room data itself.

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
