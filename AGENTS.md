# Repository Guidelines

This repo contains TDeck, a Python app for scheduling service cues and firing Bitfocus Companion button presses. Keep changes focused on the scheduler, CLI, and Web UI flow described in `README.md`.

## Project Structure & Module Organization
- `package/`: Python package code. `package/apps/calendar/` contains scheduler, storage, and utilities.
- Transcription backend: `package/apps/calendar/transcription_service.py` owns live state, audio ingest handling, SSE updates, pause detection, history, and keyword-trigger actions.
- Entry points: `webui.py` (Flask Web UI), `cli.py` (CLI), `companion.py`/`propresentor.py` (external integrations).
- `static/`: front-end JS/CSS assets. `templates/`: HTML templates.
- Sender tooling: `tools/transcription_sender.py` (CLI fallback), `tools/transcription_sender_lib.py` (shared sender logic), and `tools/transcription_sender_ui.py` (local browser-based sender setup UI).
- Sender launchers: `start_transcription_sender.bat`, `stop_transcription_sender.bat`, `start_transcription_sender.sh`, `stop_transcription_sender.sh`.
- Transcription templates: `templates/transcription.html`, `templates/transcription_display.html`, and `templates/transcription_actions.html`.
- Data/config: `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, `videohub_rooms.json`, `transcription_keyword_rules.json`, `auth.db`.
- VideoHub room images: local uploads live in `videohub_room_images/` and should remain ignored by Git.
- Logs/runtime files: `calendar.log`, `calendar_triggers.json`, `calendar.pid`, `transcription_sender_ui.pid`, and the local sender config file `transcription_sender_config.json`.

## Build, Test, and Development Commands
- Create venv and install deps:
  - `python -m venv .venv`
  - `pip install -r requirements.txt`
- Sender setup deps:
  - `pip install -r requirements_sender.txt`
- Run Web UI: `python webui.py` (uses `webserver_port` in `config.json`).
- Run sender setup UI locally on the capture computer:
  - Windows: `start_transcription_sender.bat`
  - macOS/Linux: `./start_transcription_sender.sh`
- Run CLI:
  - `python cli.py apps` (list apps)
  - `python cli.py start calendar --background` (scheduler)
  - `python cli.py stop` (stop background scheduler)
- Docker:
  - `docker build -t tdeck-calendar:latest .`
  - `docker compose up --build`

## Coding Style & Naming Conventions
- Python: 4-space indentation, PEP 8�style naming. Use `snake_case` for functions/vars, `CapWords` for classes, `UPPER_CASE` for constants.
- JavaScript (in `static/`): prefer `camelCase` for variables and functions.
- No formatter or linter is enforced in-repo; keep changes consistent with surrounding files.

## Testing Guidelines
- No automated test framework is configured in this repo.
- For manual checks: start `python webui.py`, load the UI, and run a CLI command like `python cli.py list`.
- If you add tests, place them under `tests/` with `test_*.py` and document the runner in `README.md`.

## Commit & Pull Request Guidelines
- Existing commits use short, sentence-case summaries (e.g., �Updated UI�, �Added authentication to app�). Follow the same style.
- PRs should include: summary of changes, config/data file updates (`config.json`, `events.json`), and screenshots for UI changes.
- Note any migration steps or new dependencies.

## Configuration & Security Tips
- Keep secrets and environment-specific values out of Git; use `config.json` and local overrides.
- If you change `webserver_port`, update Docker port mappings (`docker-compose.yml`) accordingly.
- Keep `videohub_room_images/` out of source control; room backgrounds are local media, not repo assets.
- Treat `transcription_ingest_token` as a secret. It authenticates remote sender uploads to `/api/transcription/audio`.
- Keep `transcription_sender_config.json` local to the sender machine. It stores the saved server URL, ingest token, microphone choice, and source name for the friendly sender UI.

## Auth Model Notes
- UI pages are protected by role-based page access in the Web UI (`require_page` checks).
- API endpoints are intentionally callable without login unless an endpoint is explicitly marked otherwise.
- Transcription operator pages use the existing `page:transcription` access check. Keep `/transcription`, `/transcription/display`, and `/transcription/actions` aligned with that flow unless a future change intentionally splits them out.
- Remote audio ingest is the exception to the usual unauthenticated API pattern: `/api/transcription/audio` is protected by the shared ingest token rather than user login.
- For VideoHub preset visibility, enforce role restrictions in the UI (hide non-allowed preset IDs) and do not add API auth/authorization checks for this behavior.
- VideoHub room metadata is global for all presets and users. Access control applies to who can manage the room layout UI, not to the room data itself.

## Transcription Feature Notes
- Main transcription pages:
  - `/transcription`: operator monitor with separate server/client state, live line, transcript history, and a link to keyword actions.
  - `/transcription/display`: iPad-friendly display that keeps transcript history scrollable and keeps the live in-progress transcription visible at the bottom for fastest reading.
  - `/transcription/actions`: keyword-trigger editor for spoken-action rules.
- Remote capture architecture:
  - The main TDeck server runs the transcription engine.
  - A separate capture computer runs the local sender UI and streams audio to the server over LAN.
  - The sender UI is browser-based so operators can pick the microphone and save setup details without CLI flags.
- Current operator-facing transcription config should stay simplified in the GUI.
  - Keep these advanced settings backend-only unless there is a strong reason to re-expose them: `transcription_chunk_ms`, `transcription_language`, `transcription_model`, `transcription_realtime_model`, `transcription_device`.
  - Legacy GUI-facing fields `transcription_bind_host`, `transcription_sender_input_device`, and the old single `transcription_color_scheme` are no longer part of the intended user-facing setup flow.
- Color customization is now more granular. Prefer per-surface colors rather than a single named scheme:
  - `transcription_color_live_bg`
  - `transcription_color_live_text`
  - `transcription_color_segment_bg`
  - `transcription_color_segment_text`
  - `transcription_color_break_soft_bg`
  - `transcription_color_break_soft_text`
  - `transcription_color_break_hard_bg`
  - `transcription_color_break_hard_text`
- Setup check behavior:
  - The transcription setup check still matters even with the friendly client UI.
  - It should report usable ingest/display URLs plus the split server/client state, not old sender-side command-line instructions.
- Sender UI expectations:
  - The client-facing setup UI should expose server URL, ingest token, source name, and microphone selection.
  - Avoid bringing back legacy advanced sender controls like chunk size, sample rate, channels, or device ids unless there is a clear operational need.

## Transcription Keyword Actions
- Spoken keyword actions are configured outside `config.json` in `transcription_keyword_rules.json`.
- Each rule is a small record with:
  - `id`
  - `enabled`
  - `keyword`
  - `action_id`
  - `label`
- Matching semantics for v1:
  - Run matching against finalized transcript segments only.
  - Use simple case-insensitive inclusion matching.
  - Repeated mentions should retrigger every time; there is no cooldown by default.
- Action system design:
  - Keep actions registry-driven so new functions can be added without redesigning rule storage or the editor page.
  - The first built-in action is `flash_red_twice`, which flashes the display page red twice in a paging-style effect.
  - Display-only action events should flow through the live transcription stream so the iPad view reacts immediately.

## VideoHub Role Controls (Where To Look)
- Storage: role settings live in `auth.db` table `roles` and are migrated/used in `webui.py`.
- Preset storage remains in `videohub_presets.json`; global room metadata is stored separately in `videohub_rooms.json`.
- Room background uploads are served from `/media/videohub_room_images/<filename>` and stored in `videohub_room_images/`.
- Routing page allow-lists (per role):
  - Columns: `videohub_allowed_outputs`, `videohub_allowed_inputs`
  - Semantics: blank/NULL/"all" => allow all; otherwise JSON list or CSV of 1-based port numbers.
  - UI: configured on Access Levels page; enforced on `/routing` page.
- VideoHub presets visibility (per role, UI-only):
  - Column: `videohub_allowed_presets`
  - Semantics: blank/NULL/"all" => all presets visible; otherwise JSON list or CSV of 1-based preset IDs.
  - UI config: `templates/admin_roles.html` + autosave payload in `static/app.js`.
  - Enforcement: only in the VideoHub page UI (the `/api/videohub/presets*` endpoints remain unauthenticated by design).
- VideoHub preset editing toggle (per role, UI-only):
  - Column: `videohub_can_edit_presets` (INTEGER, default allow when NULL for backward compatibility).
  - Meaning: when off, VideoHub page allows viewing/applying presets but disables create/save/delete/lock and room-based routing edits.
  - UI config: checkbox on Access Levels page; autosave in `static/app.js`.
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
