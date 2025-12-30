"""Top-level launcher for registered apps.

Minimal CLI: list apps, start an app.
"""
import argparse
import sys
import subprocess
import os
import signal
import time
import json
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Optional

from package.core import list_apps, get_app
from package.apps.calendar import storage, utils
logger = utils.get_logger()

PID_FILE = "calendar.pid"


def _validate_time_hhmm(s: str) -> bool:
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except Exception:
        return False


def _get_timer_presets(cfg: dict) -> list[dict]:
    try:
        if hasattr(utils, "load_timer_presets"):
            return list(utils.load_timer_presets())
    except Exception:
        pass
    return []


def _save_timer_presets(presets: list[dict]) -> None:
    try:
        if hasattr(utils, "save_timer_presets"):
            utils.save_timer_presets(presets)
            return
    except Exception:
        pass
    # fallback
    try:
        with open("timer_presets.json", "w", encoding="utf-8") as f:
            json.dump(list(presets), f, indent=2)
    except Exception:
        pass


def _save_cfg(cfg: dict) -> None:
    try:
        utils.save_config(cfg)
        utils.reload_config(force=True)
    except Exception:
        # fall back to best-effort write if utils methods change
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def cmd_timers_list(args) -> int:
    cfg = utils.get_config()
    presets = _get_timer_presets(cfg)
    if not presets:
        print("No timer presets configured")
        return 0
    for i, t in enumerate(presets):
        if isinstance(t, dict):
            name = str(t.get("name", "")).strip()
            time_str = str(t.get("time", "")).strip()
            if name and name != time_str:
                print(f"{i + 1}: {name} ({time_str})")
            else:
                print(f"{i + 1}: {time_str}")
        else:
            print(f"{i + 1}: {t}")
    return 0


def cmd_timers_add(args) -> int:
    t = str(args.time).strip()
    if not _validate_time_hhmm(t):
        print("Invalid time format. Use HH:MM")
        return 2

    cfg = utils.get_config()
    presets = _get_timer_presets(cfg)

    item = {"time": t, "name": t}
    if args.at is None:
        presets.append(item)
    else:
        try:
            idx = int(args.at)
        except Exception:
            print("--at must be an integer")
            return 2
        if idx < 0 or idx > len(presets):
            print(f"--at out of range (0..{len(presets)})")
            return 2
        presets.insert(idx, item)

    _save_timer_presets(presets)
    print(f"Added preset: {t}")
    return 0


def cmd_timers_remove(args) -> int:
    cfg = utils.get_config()
    presets = _get_timer_presets(cfg)
    if not presets:
        print("No timer presets configured")
        return 1
    try:
        idx = int(args.index)
    except Exception:
        print("index must be an integer")
        return 2
    if idx < 0 or idx >= len(presets):
        print(f"index out of range (0..{len(presets)-1})")
        return 2
    removed = presets.pop(idx)
    _save_timer_presets(presets)
    if isinstance(removed, dict):
        print(f"Removed preset {idx}: {removed.get('name', '')} ({removed.get('time', '')})")
    else:
        print(f"Removed preset {idx}: {removed}")
    return 0


def cmd_timers_move(args) -> int:
    cfg = utils.get_config()
    presets = _get_timer_presets(cfg)
    if len(presets) < 2:
        print("Not enough presets to move")
        return 1
    try:
        src = int(args.src)
        dst = int(args.dst)
    except Exception:
        print("src and dst must be integers")
        return 2
    if src < 0 or src >= len(presets):
        print(f"src out of range (0..{len(presets)-1})")
        return 2
    if dst < 0 or dst >= len(presets):
        print(f"dst out of range (0..{len(presets)-1})")
        return 2
    item = presets.pop(src)
    presets.insert(dst, item)
    _save_timer_presets(presets)
    print(f"Moved preset {src} -> {dst}")
    return 0


def cmd_timers_set(args) -> int:
    times = [str(t).strip() for t in (args.times or [])]
    times = [t for t in times if t]
    if not times:
        print("Provide one or more HH:MM times")
        return 2
    for t in times:
        if not _validate_time_hhmm(t):
            print(f"Invalid time format: {t}. Use HH:MM")
            return 2
    presets = [{"time": t, "name": t} for t in times]
    _save_timer_presets(presets)
    print(f"Replaced presets ({len(presets)} entries)")
    return 0


def cmd_timers_apply(args) -> int:
    """Mimic a Companion preset push by calling the web UI endpoint."""
    try:
        value = int(args.value)
    except Exception:
        print("value must be an integer")
        return 2

    cfg = utils.get_config()
    port = int(cfg.get("webserver_port", cfg.get("server_port", 5000)))
    base = args.webui.strip() if args.webui else f"http://127.0.0.1:{port}"
    url = base.rstrip("/") + "/api/timers/apply"

    payload = {"preset": value}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(body)
            return 0
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        print(f"HTTP error calling {url}: {e.code}\n{err_body}")
        return 2
    except Exception as e:
        print(f"Failed to call {url}: {e}")
        print("Is the web UI running? Start it with: python webui.py")
        return 2


def write_pid(pid: int) -> None:
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def read_pid() -> Optional[int]:
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def remove_pidfile() -> None:
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


def spawn_background(child_args: List[str]) -> int:
    """Spawn a detached background Python process and return its PID."""
    python = sys.executable
    script = os.path.abspath(__file__)
    cmd = [python, script] + child_args
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP, close_fds=True)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True, close_fds=True)
    return proc.pid


def kill_pid(pid: int) -> bool:
    # Try a graceful termination first, then escalate to forceful kill.
    try:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            # On Windows os.kill may not support SIGTERM well; ignore and try taskkill
            pass

        time.sleep(0.5)
        # Check if process still exists
        try:
            os.kill(pid, 0)
            still_alive = True
        except Exception:
            still_alive = False

        if not still_alive:
            return True

        # Escalate to forceful kill
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                return False
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
                return False
            except Exception:
                return True
    except Exception:
        return False


def _find_event(events: List, ident: str):
    # numeric id or numeric index (id preferred)
    try:
        num = int(ident)
        # try match by id first
        for e in events:
            if getattr(e, "id", None) == num:
                return e
        # fallback: treat as 1-based index
        idx = num - 1
        if 0 <= idx < len(events):
            return events[idx]
        return None
    except Exception:
        pass

    # match by name substring (case-insensitive)
    matches = [e for e in events if ident.lower() in e.name.lower()]
    if len(matches) >= 1:
        return matches[0]
    return None


def cmd_list_events(args):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    if not events:
        print("No events")
        return
    for e in events:
        when = f"{e.day.name} {e.date.strftime('%Y-%m-%d')} {e.time.strftime('%H:%M:%S')}"
        print(f"{getattr(e,'id',0):3d}: {e.name} | {when} | repeating={e.repeating} | active={getattr(e,'active',True)}")


def cmd_show(args):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    ev = _find_event(events, args.ident)
    if not ev:
        print("Event not found")
        return
    print({
        "id": getattr(ev, "id", None),
        "name": ev.name,
        "day": ev.day.name,
        "date": ev.date.strftime("%Y-%m-%d"),
        "time": ev.time.strftime("%H:%M:%S"),
        "repeating": ev.repeating,
        "active": getattr(ev, "active", True),
        "times": [
            {"minutes": t.minutes, "type": t.typeOfTrigger.name, "buttonURL": t.buttonURL}
            for t in ev.times
        ],
    })


def _persist_and_touch(events):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    storage.save_events(events, events_file)


def _set_active(ident: str, value: bool):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    ev = _find_event(events, ident)
    if not ev:
        print("Event not found")
        return
    ev.active = value
    _persist_and_touch(events)
    print(f"Set active={value} for '{ev.name}'")


def cmd_enable(args):
    _set_active(args.ident, True)


def cmd_disable(args):
    _set_active(args.ident, False)


def cmd_trigger(args):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    ev = _find_event(events, args.ident)
    if not ev:
        print("Event not found")
        return

    which = args.which - 1 if args.which and args.which > 0 else 0
    if which < 0 or which >= len(ev.times):
        print("Invalid trigger index")
        return

    trig = ev.times[which]
    c = utils.get_companion()
    if c and getattr(c, "connected", False):
        ok = c.post_command(trig.buttonURL)
        if ok:
            logger.info(f"Manual trigger POST {trig.buttonURL} OK | event='{ev.name}'")
            print(f"Triggered '{ev.name}' -> {trig.buttonURL} -> OK")
        else:
            logger.error(f"Manual trigger POST {trig.buttonURL} FAIL | event='{ev.name}'")
            print(f"Triggered '{ev.name}' -> {trig.buttonURL} -> FAIL")
    else:
        logger.warning(f"Companion not connected; manual trigger would POST {trig.buttonURL} | event='{ev.name}'")
        print(f"Companion not connected; would POST {trig.buttonURL}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="calendarctl")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("apps", help="List registered apps")

    start_p = sub.add_parser("start", help="Start an app")
    start_p.add_argument("app", help="App name to start")
    start_p.add_argument("--background", action="store_true", help="Start app in background")
    sub.add_parser("stop", help="Stop background app (reads calendar.pid)")

    # Event management
    list_p = sub.add_parser("list", help="List events")

    show_p = sub.add_parser("show", help="Show event details")
    show_p.add_argument("ident", help="Event id or name substring")

    enable_p = sub.add_parser("enable", help="Enable an event")
    enable_p.add_argument("ident", help="Event id or name substring")

    disable_p = sub.add_parser("disable", help="Disable an event")
    disable_p.add_argument("ident", help="Event id or name substring")

    trig_p = sub.add_parser("trigger", help="Trigger an event immediately")
    trig_p.add_argument("ident", help="Event id or name substring")
    trig_p.add_argument("--which", type=int, default=1, help="Which trigger (1-based)")

    # Show currently scheduled trigger jobs
    sub.add_parser("triggers", help="List scheduled trigger jobs (if scheduler running)")

    # Event editing commands
    add_p = sub.add_parser("add", help="Add a new event")
    add_p.add_argument("--name", required=True, help="Event name")
    add_p.add_argument("--day", required=True, help="Weekday name (Monday..Sunday)")
    add_p.add_argument("--date", required=True, help="Date YYYY-MM-DD")
    add_p.add_argument("--time", required=True, help="Time HH:MM:SS")
    add_p.add_argument("--repeating", action="store_true", help="Make event repeating")
    add_p.add_argument("--active", action="store_true", help="Set event active (default true)")
    add_p.add_argument("--trigger", action="append", help="Trigger spec minutes,TYPE,buttonURL (repeatable)")

    remove_p = sub.add_parser("remove", help="Remove an event")
    remove_p.add_argument("ident", help="Event id or name substring")

    edit_p = sub.add_parser("edit", help="Edit an event (provide fields to change)")
    edit_p.add_argument("ident", help="Event id or name substring")
    edit_p.add_argument("--name", help="Event name")
    edit_p.add_argument("--day", help="Weekday name (Monday..Sunday)")
    edit_p.add_argument("--date", help="Date YYYY-MM-DD")
    edit_p.add_argument("--time", help="Time HH:MM:SS")
    edit_p.add_argument("--repeating", type=bool, help="Repeating: true/false")
    edit_p.add_argument("--active", type=bool, help="Active: true/false")
    edit_p.add_argument("--trigger", action="append", help="Trigger spec minutes,TYPE,buttonURL (replaces triggers)")

    debug_p = sub.add_parser("debug", help="Show or set debug mode")
    debug_p.add_argument("action", choices=["show", "on", "off"], help="Action: show, on, off")

    # Timers management
    timers_p = sub.add_parser("timers", help="Manage timer presets and simulate Companion preset pushes")
    timers_sub = timers_p.add_subparsers(dest="timers_cmd")

    timers_sub.add_parser("list", help="List configured timer presets")

    t_add = timers_sub.add_parser("add", help="Add a preset time (HH:MM)")
    t_add.add_argument("time", help="Time in HH:MM")
    t_add.add_argument("--at", type=int, help="Insert at index (0..len)")

    t_rm = timers_sub.add_parser("remove", help="Remove a preset by index")
    t_rm.add_argument("index", help="Preset index (0-based)")

    t_mv = timers_sub.add_parser("move", help="Move/reorder a preset")
    t_mv.add_argument("src", help="Source index (0-based)")
    t_mv.add_argument("dst", help="Destination index (0-based)")

    t_set = timers_sub.add_parser("set", help="Replace the preset list with provided times")
    t_set.add_argument("times", nargs="+", help="One or more times in HH:MM")

    t_apply = timers_sub.add_parser("apply", help="Mimic a Companion preset push (calls the web UI)")
    t_apply.add_argument("value", help="The integer value Companion would send")
    t_apply.add_argument("--webui", help="Override web UI base URL (default http://127.0.0.1:<webserver_port>)")

    args = parser.parse_args(argv)

    if args.cmd == "apps":
        apps = list_apps()
        for name in apps:
            print(name)
        return 0

    if args.cmd == "start":
        app = get_app(args.app)
        if not app:
            print(f"Unknown app: {args.app}")
            return 2
        if args.background:
            # Spawn a child that runs the same script without --background
            child_args = ["start", args.app]
            pid = spawn_background(child_args)
            write_pid(pid)
            print(f"Started {args.app} in background (pid={pid}), pidfile={PID_FILE}")
            return 0

        try:
            app.start(blocking=not args.background)
        except KeyboardInterrupt:
            app.stop()
        return 0

    if args.cmd == "stop":
        pid = read_pid()
        if not pid:
            print("No pidfile found or invalid PID.")
            return 1
        # Try graceful stop first by sending SIGTERM; kill_pid will escalate if needed
        ok = kill_pid(pid)
        if ok:
            remove_pidfile()
            print(f"Stopped process {pid}.")
            return 0
        else:
            print(f"Failed to stop process {pid}. You may need to kill it manually.")
            return 2

    if args.cmd == "list":
        cmd_list_events(args)
        return 0

    if args.cmd == "triggers":
        app_inst = get_app("calendar")
        # Access the scheduler instance if running
        sched = getattr(app_inst, "_scheduler", None)
        if sched is not None:
            # Copy heap under the scheduler condition to avoid races
            try:
                with sched._cv:
                    heap_copy = list(sched._heap)
            except Exception:
                heap_copy = list(getattr(sched, "_heap", []))

            if not heap_copy:
                print("No scheduled triggers")
                return 0

            # Sort by due time
            heap_copy.sort()
            from datetime import datetime

            for i, job in enumerate(heap_copy, start=1):
                due = job.due.strftime("%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                secs = int((job.due - now).total_seconds())
                print(f"{i:3d}: due={due} (+{secs}s) | event=#{getattr(job.event,'id',0)} '{job.event.name}' | trigger={job.trigger_index+1}/{len(job.event.times)} | offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'")
            return 0

        # Fall back to reading the persistent snapshot written by the
        # background scheduler process (if available).
        try:
            import json
            from pathlib import Path

            path = Path.cwd() / "calendar_triggers.json"
            if not path.exists():
                print("Scheduler is not running. No scheduled triggers available.")
                return 0

            data = json.loads(path.read_text(encoding="utf-8"))
            if not data:
                print("No scheduled triggers")
                return 0

            for i, j in enumerate(data, start=1):
                # snapshot format may include event_id as 'event_id' or include id in event string
                event_id = j.get("event_id") or j.get("id") or None
                eid = f"#{event_id} " if event_id is not None else ""
                print(f"{i:3d}: due={j['due']} (+{j['seconds_until']}s) | event={eid}'{j['event']}' | trigger_index={j['trigger_index']+1} | offset={j['offset_min']}min | url='{j['url']}'")
            return 0
        except Exception:
            print("Scheduler is not running. No scheduled triggers available.")
            return 0

    if args.cmd == "add":
        cfg = utils.get_config()
        events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        from package.apps.calendar.models import TimeOfTrigger, TypeofTime, WeekDay
        try:
            times = []
            if args.trigger:
                for spec in args.trigger:
                    parts = spec.split(",", 2)
                    minutes = int(parts[0])
                    typ = TypeofTime[parts[1]]
                    url = parts[2] if len(parts) > 2 else ""
                    times.append(TimeOfTrigger(minutes, typ, url))
            from datetime import datetime
            # determine new unique id
            max_id = 0
            for ev in events:
                if isinstance(getattr(ev, "id", None), int) and ev.id > max_id:
                    max_id = ev.id

            new_id = max_id + 1

            event = __import__("package.apps.calendar.models", fromlist=["Event"]).Event(
                args.name,
                new_id,
                WeekDay[args.day],
                datetime.strptime(args.date, "%Y-%m-%d").date(),
                datetime.strptime(args.time, "%H:%M:%S").time(),
                bool(args.repeating),
                times,
                bool(args.active),
            )
            events.append(event)
            storage.save_events(events, events_file)
            print(f"Added event '{args.name}'")
        except Exception as e:
            print(f"Failed to add event: {e}")
        return 0

    if args.cmd == "remove":
        cfg = utils.get_config()
        events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        ev = _find_event(events, args.ident)
        if not ev:
            print("Event not found")
            return 1
        events.remove(ev)
        storage.save_events(events, events_file)
        print(f"Removed '{ev.name}'")
        return 0

    if args.cmd == "edit":
        cfg = utils.get_config()
        events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        ev = _find_event(events, args.ident)
        if not ev:
            print("Event not found")
            return 1
        from package.apps.calendar.models import TimeOfTrigger, TypeofTime, WeekDay
        from datetime import datetime
        try:
            if args.name:
                ev.name = args.name
            if args.day:
                ev.day = WeekDay[args.day]
            if args.date:
                ev.date = datetime.strptime(args.date, "%Y-%m-%d").date()
            if args.time:
                ev.time = datetime.strptime(args.time, "%H:%M:%S").time()
            if args.repeating is not None:
                ev.repeating = bool(args.repeating)
            if args.active is not None:
                ev.active = bool(args.active)
            if args.trigger is not None:
                times = []
                for spec in args.trigger:
                    parts = spec.split(",", 2)
                    minutes = int(parts[0])
                    typ = TypeofTime[parts[1]]
                    url = parts[2] if len(parts) > 2 else ""
                    times.append(TimeOfTrigger(minutes, typ, url))
                ev.times = times
            storage.save_events(events, events_file)
            print(f"Updated '{ev.name}'")
        except Exception as e:
            print(f"Failed to update event: {e}")
        return 0

    if args.cmd == "show":
        cmd_show(args)
        return 0

    if args.cmd == "enable":
        cmd_enable(args)
        return 0

    if args.cmd == "disable":
        cmd_disable(args)
        return 0

    if args.cmd == "trigger":
        cmd_trigger(args)
        return 0

    if args.cmd == "debug":
        action = args.action
        if action == "show":
            print("debug=", utils.get_debug())
            return 0
        if action == "on":
            utils.set_debug(True, persist=True)
            print("debug set to true")
            return 0
        if action == "off":
            utils.set_debug(False, persist=True)
            print("debug set to false")
            return 0
        print("Unknown debug action")
        return 2

    if args.cmd == "timers":
        if args.timers_cmd == "list":
            return cmd_timers_list(args)
        if args.timers_cmd == "add":
            return cmd_timers_add(args)
        if args.timers_cmd == "remove":
            return cmd_timers_remove(args)
        if args.timers_cmd == "move":
            return cmd_timers_move(args)
        if args.timers_cmd == "set":
            return cmd_timers_set(args)
        if args.timers_cmd == "apply":
            return cmd_timers_apply(args)
        timers_p.print_help()
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
