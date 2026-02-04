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
