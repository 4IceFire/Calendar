# Calendar Companion Scheduler

A small Python app that schedules calendar events and fires Bitfocus Companion button presses at configured times.

It supports:
- A **scheduler** that watches an events JSON file and executes triggers.
- A **CLI** (`cli.py`) for starting/stopping the scheduler and managing events.
- A **Web UI** (`webui.py`) for editing events and templates in a browser.

## What this app does

- You define events in an `events.json`-style file (configurable via `config.json`).
- Each event has one or more **triggers**:
  - `BEFORE` (N minutes before the event)
  - `AT` (at the event time)
  - `AFTER` (N minutes after the event)
- When a trigger is due, the scheduler sends an HTTP POST to Bitfocus Companion’s HTTP API for a button press.

Internally, triggers are stored like `location/<page>/<row>/<column>/press`.

Important: store **paths**, not full URLs. The scheduler automatically prefixes Companion’s `/api/` base.

The Companion client posts to:

`http://<companion_ip>:<companion_port>/api/location/<page>/<row>/<column>/press`

## Requirements

- Windows/macOS/Linux
- **Python 3.10+** (this repo uses modern Python typing like `X | Y`)
- Bitfocus Companion running on the network (optional for testing, required for real trigger execution)

## Installation

1) Create and activate a virtual environment (recommended)

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

2) Install dependencies

```powershell
pip install flask werkzeug requests python-dotenv
```

That’s it—there is no separate build step.

## Configuration

The app reads `config.json` from the repo root.

Common keys:
- `EVENTS_FILE`: which events file to use (default: `events.json`)
- `companion_ip`: IP/hostname for Companion (default: `127.0.0.1`)
- `companion_port`: Companion HTTP port (default in code: `8000`)
- `webserver_port`: Web UI port (default: `5000`)
- `poll_interval`: seconds between file-change checks (default: `1.0`)
- `debug`: enables more verbose logging/output

## Run (Web UI)

Start the Web UI server:

```powershell
python webui.py
```

It reads `webserver_port` from `config.json` and prints the URL at startup.

The Web UI also has controls to start/stop registered apps (including the calendar scheduler) from the browser.

## Run (CLI)

List available apps:

```powershell
python cli.py apps
```

Start the calendar scheduler in the foreground:

```powershell
python cli.py start calendar
```

Start the calendar scheduler in the background (writes `calendar.pid`):

```powershell
python cli.py start calendar --background
```

Stop a background scheduler:

```powershell
python cli.py stop
```

## Managing events (CLI)

List events:

```powershell
python cli.py list
```

Show an event:

```powershell
python cli.py show 1
```

Add an event with triggers (examples):

```powershell
python cli.py add --name "Sunday Service" --day Sunday --date 2025-12-28 --time 10:00:00 --repeating \
  --trigger 10,BEFORE,location/1/0/1/press \
  --trigger 0,AT,location/1/0/2/press
```

Enable/disable an event:

```powershell
python cli.py disable 1
python cli.py enable 1
```

Manually fire a trigger immediately:

```powershell
python cli.py trigger 1 --which 1
```

For the full CLI reference, see `CLI_REFERENCE.md`.

Tip: for button presses, use `location/<page>/<row>/<column>/press` (or in the Web UI you can enter a short form like `1/0/1`, which it converts to `location/1/0/1/press`).

## Events file format

By default, events are stored in `events.json` (or whatever `EVENTS_FILE` points to). Each entry looks like:

```json
{
  "id": 1,
  "name": "Sunday Service",
  "day": "Sunday",
  "date": "2025-12-28",
  "time": "10:00:00",
  "repeating": true,
  "active": true,
  "times": [
    {"minutes": 10, "typeOfTrigger": "BEFORE", "buttonURL": "location/1/0/1/press"},
    {"minutes": 0,  "typeOfTrigger": "AT",     "buttonURL": "location/1/0/2/press"}
  ]
}
```

Notes:
- `minutes` is always a non-negative integer. The `BEFORE`/`AFTER` meaning comes from `typeOfTrigger`.
- If `active` or `id` are missing, the loader will fill reasonable defaults.

## Outputs and logs

- `calendar.log`: rolling log file (POST successes/failures and connectivity notes)
- `calendar_triggers.json`: snapshot of upcoming scheduled triggers (written by the scheduler)
- `calendar.pid`: pidfile used by `python cli.py stop` when running in background

## Troubleshooting

- If triggers are not firing, confirm the scheduler is running and the event is `active=true`.
- If you see Companion connectivity errors:
  - verify `companion_ip` and `companion_port` in `config.json`
  - ensure Companion’s HTTP API is enabled/reachable
  - check `calendar.log` for POST results

## ProPresenter timers (optional)

This repo also includes a small ProPresenter HTTP API client focused on timer control.

Example:

```python
from propresentor import ProPresenter

pp = ProPresenter(host="127.0.0.1", port=50001)

# List configured timers
timers = pp.list_timers()

# Start/stop/reset by name, UUID, or index
pp.start_timer("Countdown Timer")
pp.increment_timer("Countdown Timer", -10)  # subtract 10s
pp.stop_timer("Countdown Timer")
pp.reset_timer("Countdown Timer")

# Read current timer values/states
current = pp.get_current_timer_times()
```
