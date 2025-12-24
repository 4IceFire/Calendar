"""Top-level launcher for registered apps.

Minimal CLI: list apps, start an app.
"""
import argparse
import sys
import subprocess
import os
import signal
import time
from typing import List, Optional

from package.core import list_apps, get_app
from package.apps.calendar import storage, utils
logger = utils.get_logger()

PID_FILE = "calendar.pid"


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
    # numeric index (1-based)
    try:
        idx = int(ident) - 1
        if 0 <= idx < len(events):
            return events[idx]
        return None
    except Exception:
        pass

    # match by name substring (case-insensitive)
    matches = [e for e in events if ident.lower() in e.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches[0]
    return None


def cmd_list_events(args):
    events = storage.load_events()
    if not events:
        print("No events")
        return
    for i, e in enumerate(events, start=1):
        when = f"{e.day.name} {e.date.strftime('%Y-%m-%d')} {e.time.strftime('%H:%M:%S')}"
        print(f"{i:2d}: {e.name} | {when} | repeating={e.repeating} | active={getattr(e,'active',True)}")


def cmd_show(args):
    events = storage.load_events()
    ev = _find_event(events, args.ident)
    if not ev:
        print("Event not found")
        return
    print({
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
    events = storage.load_events()
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
    events = storage.load_events()
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
            event = __import__("package.apps.calendar.models", fromlist=["Event"]).Event(
                args.name,
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
