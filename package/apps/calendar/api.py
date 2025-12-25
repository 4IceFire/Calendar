from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime

from package.apps.calendar import storage, utils
from package.apps.calendar.models import TimeOfTrigger, TypeofTime, WeekDay, Event
import os

app = FastAPI(title="Calendar API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TriggerSpec(BaseModel):
    minutes: int
    typeOfTrigger: str
    buttonURL: str = ""


class EventSpec(BaseModel):
    name: str
    day: str
    date: str
    time: str
    repeating: bool = False
    active: bool = True
    times: List[TriggerSpec] = []


def event_to_dict(ev: Event) -> Dict[str, Any]:
    return {
        "id": getattr(ev, "id", None),
        "name": ev.name,
        "day": ev.day.name,
        "date": ev.date.strftime("%Y-%m-%d"),
        "time": ev.time.strftime("%H:%M:%S"),
        "repeating": ev.repeating,
        "active": getattr(ev, "active", True),
        "times": [
            {"minutes": t.minutes, "typeOfTrigger": t.typeOfTrigger.name, "buttonURL": t.buttonURL}
            for t in ev.times
        ],
    }


def parse_event_spec(spec: EventSpec) -> Event:
    # convert TriggerSpec -> TimeOfTrigger and strings -> enums / date/time
    times = []
    for t in spec.times:
        if t.typeOfTrigger not in TypeofTime.__members__:
            raise ValueError(f"Invalid trigger type: {t.typeOfTrigger}")
        typ = TypeofTime[t.typeOfTrigger]
        times.append(TimeOfTrigger(int(t.minutes), typ, t.buttonURL))

    if spec.day not in WeekDay.__members__:
        raise ValueError(f"Invalid weekday: {spec.day}")

    date_obj = datetime.strptime(spec.date, "%Y-%m-%d").date()
    time_obj = datetime.strptime(spec.time, "%H:%M:%S").time()

    # id will be assigned by the caller when persisting
    return Event(spec.name, None, WeekDay[spec.day], date_obj, time_obj, bool(spec.repeating), times, bool(spec.active))


@app.get("/api/events", response_model=List[Dict[str, Any]])
def list_events():
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    return [event_to_dict(e) for e in events]


@app.post("/api/events", response_model=Dict[str, Any])
def create_event(spec: EventSpec):
    try:
        ev = parse_event_spec(spec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    # determine new unique id
    max_id = 0
    for e in events:
        if isinstance(getattr(e, "id", None), int) and e.id > max_id:
            max_id = e.id

    new_id = max_id + 1
    ev.id = new_id
    events.append(ev)
    storage.save_events(events, events_file)
    return event_to_dict(ev)


@app.put("/api/events/{ident}", response_model=Dict[str, Any])
def update_event(ident: int, spec: EventSpec):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    # find by primary key id
    matching = [e for e in events if getattr(e, "id", None) == ident]
    if not matching:
        raise HTTPException(status_code=404, detail="Event not found")
    idx = events.index(matching[0])
    try:
        ev = parse_event_spec(spec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # preserve primary key
    ev.id = ident
    events[idx] = ev
    storage.save_events(events, events_file)
    return event_to_dict(ev)


@app.delete("/api/events/{ident}")
def delete_event(ident: int):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    matching = [e for e in events if getattr(e, "id", None) == ident]
    if not matching:
        raise HTTPException(status_code=404, detail="Event not found")
    ev = matching[0]
    events.remove(ev)
    storage.save_events(events, events_file)
    return {"removed": True, "name": ev.name}


class TriggerBody(BaseModel):
    which: Optional[int] = 1


@app.post("/api/events/{ident}/trigger")
def trigger_event(ident: int, body: TriggerBody):
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    events = storage.load_events(events_file)
    matching = [e for e in events if getattr(e, "id", None) == ident]
    if not matching:
        raise HTTPException(status_code=404, detail="Event not found")
    ev = matching[0]
    which = (body.which - 1) if body.which and body.which > 0 else 0
    if which < 0 or which >= len(ev.times):
        raise HTTPException(status_code=400, detail="Invalid trigger index")

    trig = ev.times[which]
    c = utils.get_companion()
    if c and getattr(c, "connected", False):
        ok = c.post_command(trig.buttonURL)
        return {"ok": ok}
    else:
        # Log that companion is unavailable and return 503
        utils.get_logger().warning(f"API manual trigger: Companion not connected; would POST {trig.buttonURL} | event='{ev.name}'")
        raise HTTPException(status_code=503, detail="Companion not connected")


@app.post("/api/control/start")
def api_start(background: Optional[bool] = False):
    # Start scheduler in-process non-blocking
    from package.core import get_app

    app_inst = get_app("calendar")
    try:
        app_inst.start(blocking=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"started": True}


@app.post("/api/control/stop")
def api_stop():
    # Try stopping an in-process scheduler by creating an app and stopping it.
    from package.core import get_app

    app_inst = get_app("calendar")
    try:
        app_inst.stop()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"stopped": True}


@app.get("/api/status")
def api_status():
    cfg = utils.get_config()
    events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
    # simple status: whether events file exists and debug flag
    import os

    info = {
        "events_file": events_file,
        "events_file_exists": os.path.exists(events_file),
        "debug": utils.get_debug(),
    }
    return info


# Note: static UI serving has been removed. Serve a frontend separately if needed.
