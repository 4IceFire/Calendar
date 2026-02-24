# Repository Guidelines

This repo contains TDeck, a Python app for scheduling service cues and firing Bitfocus Companion button presses. Keep changes focused on the scheduler, CLI, and Web UI flow described in `README.md`.

## Project Structure & Module Organization
- `package/`: Python package code. `package/apps/calendar/` contains scheduler, storage, and utilities.
- Entry points: `webui.py` (Flask Web UI), `cli.py` (CLI), `companion.py`/`propresentor.py` (external integrations).
- `static/`: front-end JS/CSS assets. `templates/`: HTML templates.
- Data/config: `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, `auth.db`.
- Logs/runtime files: `calendar.log`, `calendar_triggers.json`, `calendar.pid`.

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

## Auth Model Notes
- UI pages are protected by role-based page access in the Web UI (`require_page` checks).
- API endpoints are intentionally callable without login unless an endpoint is explicitly marked otherwise.
- For VideoHub preset visibility, enforce role restrictions in the UI (hide non-allowed preset IDs) and do not add API auth/authorization checks for this behavior.

## VideoHub Role Controls (Where To Look)
- Storage: role settings live in `auth.db` table `roles` and are migrated/used in `webui.py`.
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
  - Meaning: when off, VideoHub page allows applying presets but disables create/save/delete/lock and grid/name edits.
  - UI config: checkbox on Access Levels page; autosave in `static/app.js`.
  - Enforcement: `webui.py` passes `can_edit_presets` into `templates/videohub.html` via `data-can-edit-presets`.
