from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import List


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


class Event:
    def __init__(
        self,
        name: str,
        id: int | None,
        day: WeekDay,
        event_date: date,
        event_time: time,
        repeating: bool,
        times: List["TimeOfTrigger"],
        active: bool = True,
    ) -> None:
        self.name = name
        # primary key id (may be None for older records until storage assigns one)
        self.id = id
        self.day = day
        self.date = event_date
        self.time = event_time
        self.repeating = repeating
        self.active = active
        self.times = times
        self.times.sort()

    def __str__(self) -> str:
        idpart = f"#{self.id} " if getattr(self, "id", None) is not None else ""
        return f"   {idpart}{self.name}   \n" + (len(self.name) + 6) * "-" + "\n"


@dataclass(order=True)
class TriggerJob:
    due: datetime
    event: Event = field(compare=False)
    occurrence: datetime = field(compare=False)
    trigger_index: int = field(compare=False)
    trigger: TimeOfTrigger = field(compare=False)
