"""Simple event loader and polling monitor."""

import json
import time as t  # For sleep functionality
from enum import Enum
from datetime import date, datetime, time
from typing import List

EVENTS_FILE = 'events.json'

class TypeofTime(Enum):
    BEFORE = 0
    AT = 1
    AFTER = 2

class WeekDay(Enum):
    Monday = 1
    Tuesday = 2
    Wednesday = 3
    Thursday = 4
    Friday = 5
    Saturday = 6
    Sunday = 7

class Event:  # Create Event, Add Times
    def __init__(
        self,
        name: str,
        day: WeekDay,
        event_date: date,
        event_time: time,
        repeating: bool,
        times: List["TimeOfTrigger"],
    ) -> None:
        self.name = name
        self.day = day
        self.date = event_date  # New date variable
        self.time = event_time  # Ensure time is a datetime.time object
        self.repeating = repeating
        self.times = times

        self.times.sort()

    def __str__(self) -> str:
        return f"   {self.name}   \n" + (len(self.name) + 6) * "-" + "\n"



class TimeOfTrigger:
    def __init__(self, minutes: int, typeOfTrigger: TypeofTime, buttonURL: str) -> None:
        self.minutes = minutes
        self.typeOfTrigger = typeOfTrigger
        self.buttonURL = buttonURL

        if typeOfTrigger == TypeofTime.BEFORE:
            self.timer = -minutes
        elif typeOfTrigger == TypeofTime.AT:
            self.timer = 0
        elif typeOfTrigger == TypeofTime.AFTER:
            self.timer = minutes
        else:
            raise ValueError("Impossible Selection")

    def __lt__(self, other: "TimeOfTrigger") -> bool:
        return self.timer < other.timer
    
    def __str__(self) -> str:
        return str(self.timer)


events: List[Event] = []


def load_events() -> List[Event]:
    """Load events from the JSON file on disk."""
    try:
        with open(EVENTS_FILE, 'r') as file:
            events_data = json.load(file)
            loaded_events = []
            for event in events_data:
                times = [
                    TimeOfTrigger(
                        t["minutes"],
                        TypeofTime[t["typeOfTrigger"]],
                        t.get("buttonURL", "")  # Import buttonURL or default to an empty string
                    )
                    for t in event["times"]
                ]
                loaded_events.append(
                    Event(
                        event["name"],
                        WeekDay[event["day"]],
                        datetime.strptime(event["date"], "%Y-%m-%d").date(),  # Parse date
                        datetime.strptime(event["time"], "%H:%M:%S").time(),  # Parse time
                        event["repeating"],
                        times
                    )
                )
            return loaded_events
    except FileNotFoundError:
        return []

def save_events() -> None:
    """Persist current in-memory events to disk."""
    with open(EVENTS_FILE, 'w') as file:
        events_data = []
        for event in events:
            event_dict = {
                "name": event.name,
                "day": event.day.name,
                "date": event.date.strftime("%Y-%m-%d"),  # Format date as string
                "time": event.time.strftime("%H:%M:%S"),  # Format time as string
                "repeating": event.repeating,
                "times": [{"minutes": t.minutes, "typeOfTrigger": t.typeOfTrigger.name} for t in event.times]
            }
            events_data.append(event_dict)
        json.dump(events_data, file)

events = load_events()

# Variable to keep track of the current local time
current_local_time = datetime.now()

### FOR TESTING THINGS
def TestingGround():
    global events
    for i in events:
        print(i)

def monitor_events():
    """
    Efficiently checks the current time and triggers actions at the exact second of an event.
    """
    while True:
        current_time = datetime.now()
        next_event_time = None
        
        for event in events:
            # Check if the event is today or if it's repeating
            if event.date == current_time.date() or event.repeating:
                event_datetime = datetime.combine(event.date, event.time)
                if event_datetime > current_time:
                    # Find the next event time
                    if next_event_time is None or event_datetime < next_event_time:
                        next_event_time = event_datetime

        if next_event_time:
            # Calculate the time difference and sleep until the next event
            time_to_sleep = (next_event_time - current_time).total_seconds()
            print(f"Next event: {next_event_time}, sleeping for {time_to_sleep} seconds")
            t.sleep(time_to_sleep)

            # Trigger the event
            print(f"Event triggered at {next_event_time}")
            # Placeholder for future actions
            # Example: Trigger a notification, execute a task, etc.
        else:
            # No upcoming events, sleep for a default duration
            print("No upcoming events, sleeping for 60 seconds")
            t.sleep(60)

if __name__ == "__main__":
    # Uncomment the following line to start monitoring events
    monitor_events()
    # TestingGround()