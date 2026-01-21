**CLI Reference**

This document describes the `calendarctl` CLI available in this workspace and examples for using each command.

**Usage**
- Run: `python cli.py <command> [args]`
- Config: `config.json` controls `EVENTS_FILE`, `poll_interval`, and `debug`.
- Log file: `calendar.log` is created at runtime (and is gitignored) and contains info/warnings about Companion connectivity and POST results.

**Commands**

- **apps**: List registered apps
  - Example: `python cli.py apps`

- **start <app> [--background]**: Start an app. Use `--background` to detach and write a pidfile (`calendar.pid`).
  - Example (foreground): `python cli.py start calendar`
  - Example (background): `python cli.py start calendar --background`

- **stop**: Stop a background app by reading `calendar.pid` and signalling that process. Tries graceful stop first.
  - Example: `python cli.py stop`

- **list**: List events from the configured `EVENTS_FILE`.
  - Example: `python cli.py list`

- **show <ident>**: Show event details. `ident` is a 1-based index from `list` or a name substring.
  - Example: `python cli.py show 1` or `python cli.py show "Sunday Service"`

- **add --name NAME --day DAY --date YYYY-MM-DD --time HH:MM:SS [--repeating] [--active] --trigger M,TYPE,URL ...**
  - Add a new event. `--trigger` may be repeated to add multiple triggers.
  - `TYPE` must be one of `BEFORE`, `AT`, `AFTER`. `M` is minutes (integer).
  - Example: `python cli.py add --name "My Event" --day Monday --date 2025-12-28 --time 09:00:00 --trigger 10,BEFORE,http://host/button1 --trigger 0,AT,http://host/button2`

- **remove <ident>**: Remove an event by index or name substring.
  - Example: `python cli.py remove 2`

- **edit <ident> [--name NAME] [--day DAY] [--date YYYY-MM-DD] [--time HH:MM:SS] [--repeating true|false] [--active true|false] [--trigger M,TYPE,URL ...]**
  - Edit an existing event. When `--trigger` is provided, it replaces the event's triggers.
  - Example (replace triggers): `python cli.py edit 1 --trigger 15,BEFORE,http://host/new1 --trigger 0,AT,http://host/new2`

- **enable <ident> / disable <ident>**: Toggle the `active` flag for an event and save it to the configured `EVENTS_FILE`.
  - Example: `python cli.py enable 1`

- **trigger <ident> [--which N]**: Manually POST the trigger's URL for the event. `--which` selects which trigger (1-based).
  - Example: `python cli.py trigger 1 --which 2`

- **debug show|on|off**: Query or set runtime debug mode. Setting persists to `config.json` and runs `reload_config()`.
  - Example: `python cli.py debug off`

- **timers ...**: Manage timer presets (stored in `timer_presets.json`) and simulate a Companion preset push.
  - `timers list`: View the preset list (displayed starting at 1). Each preset includes a saved name.
    - Example: `python cli.py timers list`
  - `timers add HH:MM [--at N]`: Add a preset time (optionally insert at an index)
    - Example: `python cli.py timers add 08:45`
    - Example: `python cli.py timers add 09:05 --at 1`
  - `timers remove INDEX`: Remove a preset by index (0-based)
    - Note: `timers list` displays 1-based numbers, but `remove` uses 0-based indices (`0` is the first preset).
    - Example: `python cli.py timers remove 0`
  - `timers move SRC DST`: Reorder presets (0-based indices)
    - Note: `timers list` displays 1-based numbers, but `move` uses 0-based indices.
    - Example: `python cli.py timers move 3 1`
  - `timers set HH:MM HH:MM ...`: Replace the entire preset list
    - Example: `python cli.py timers set 08:15 08:30 09:10 09:30`
  - `timers apply VALUE [--webui URL]`: Mimic a Companion button/preset push by calling the web UI endpoint `/api/timers/apply`.
    - NOTE: VALUE is always 1-based (1 selects the first preset).
    - Example: `python cli.py timers apply 1`
    - Example (override webui): `python cli.py timers apply 2 --webui http://127.0.0.1:5000`

- **videohub ...**: Control a Blackmagic VideoHub via direct TCP (default port 9990).
  - Config keys (in `config.json`):
    - `videohub_ip` (or `videohub_host`): IP/hostname of the VideoHub
    - `videohub_port` (optional, default 9990)
    - `videohub_timeout` (optional, seconds)
  - You can also override host/port per-command:
    - `--host <ip-or-hostname>`
    - `--port <port>`
  - Indexing note:
    - The VideoHub protocol is **0-based**.
    - This CLI defaults to **1-based** `--output/--input` for convenience.
    - Use `--zero-based` to treat `--output/--input` as 0-based.
  - `videohub ping`: Best-effort ping (just sends `PING` block)
    - Example (from config.json): `python cli.py videohub ping`
    - Example (override host): `python cli.py videohub ping --host 192.168.1.50`
  - `videohub route --output N --input N [--monitor] [--zero-based]`: Route an input to an output
    - Example (human 1-based): `python cli.py videohub route --output 1 --input 4`
    - Example (0-based): `python cli.py videohub route --output 0 --input 3 --zero-based`
    - Example (monitoring output): `python cli.py videohub route --output 1 --input 4 --monitor`


**Notes & Details**
- The CLI reads/writes the `EVENTS_FILE` from `config.json`. To confirm which file is active:
  - `python -c "from package.apps.calendar import utils; print(utils.get_config()['EVENTS_FILE'])"`
- Trigger format summary: `minutes,TYPE,buttonURL` where
  - `minutes`: integer (e.g. `10`)
  - `TYPE`: `BEFORE` (fires at event time minus minutes), `AT` (fires at event time), `AFTER` (fires event time plus minutes)
  - `buttonURL`: the Companion endpoint path or URL to POST
- `add` and `edit` commands write to the configured `EVENTS_FILE` so the scheduler (which watches file mtime) will reload automatically.
- Background start writes a pid to `calendar.pid` in the working directory; `stop` reads that file to terminate the process.
- Companion connectivity problems and POST results are written to `calendar.log`. Use:
  - PowerShell: `Get-Content calendar.log -Tail 100`
  - cmd: `type calendar.log`

**Examples (one-line)**
- Add event with two triggers (bash):
```
python cli.py add --name "Example" --day Sunday --date 2025-12-28 --time 10:00:00 --trigger 10,BEFORE,http://127.0.0.1/button1 --trigger 0,AT,http://127.0.0.1/button2
```
- Edit event triggers (PowerShell):
```
python cli.py edit 1 --trigger 15,BEFORE,http://127.0.0.1/new --trigger 0,AT,http://127.0.0.1/now
```

If you add new CLI commands, update this file with the command name, short description, and an example.
