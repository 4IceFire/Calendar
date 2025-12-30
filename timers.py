from propresentor import ProPresentor
from datetime import time, datetime
from companion import Companion

pp = ProPresentor("127.0.0.1", port=1025)
c = Companion("127.0.0.1", port=8100)

class Time():
    def __init__(self, time):
        self.time = time

timers = [Time("8:15"), Time("8:30"), Time("9:10"), Time("9:30")]

TimerIndex = 0
timer_index = 1

pp.SetCountdownToTime(timer_index, timers[TimerIndex].time)
#print(c.GetVariable("testing"))
c.SetVariable("testing", 10)