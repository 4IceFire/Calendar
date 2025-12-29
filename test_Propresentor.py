from propresentor import ProPresentor
from datetime import time, datetime
from companion import Companion

pp = ProPresentor("127.0.0.1", port=1025)
c = Companion("127.0.0.1", port=8100)

timer_index = 1
timer_time = datetime.strptime("10:00", "%H:%M")

pp.SetTimer(timer_index, timer_time)
print(c.GetVariable("testing"))