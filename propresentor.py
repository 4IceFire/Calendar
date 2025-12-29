from typing import Optional, Any, Dict
import requests
from datetime import datetime, time

class ProPresentor():
    def __init__(self, ip, port, timeout: float = 2.0, verify_on_init: bool = False):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{ip}:{port}"
        self.session = requests.Session()

    def _build_api_url(self, url: str) -> str:
        return f"{self.base_url}/v1/{url.lstrip('/')}"
    
    def CreateTimer(self, index):
        pass

    def get_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        full_url = self._build_api_url(url)
        try:
            resp = self.session.get(full_url, params=params, timeout=timeout or self.timeout)
            if 200 <= resp.status_code < 300:
                return resp.text
            return None
        except requests.RequestException:
            return None

    def post_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """POST to the ProPresenter API. Use `json=` to send a JSON body."""
        full_url = self._build_api_url(url)
        try:
            resp = self.session.post(full_url, params=params, json=json, timeout=timeout or self.timeout)
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            return False

    def put_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """PUT to the ProPresenter API. Use `json=` to send a JSON body."""
        full_url = self._build_api_url(url)
        try:
            resp = self.session.put(full_url, params=params, json=json, timeout=timeout or self.timeout)
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            return False
        
    def SetTimer(self, index, time:time) -> bool:
        name = requests.get(self._build_api_url(f"timer/{index}")).json()["id"]["name"]
        
        time_of_day = ""
        seconds = 0
        
        hour = time.hour
        minute = time.minute 

        if time.hour < 12 and time.hour >= 0:
            time_of_day = "am"
            seconds = ((hour * 60) + minute)  * 60
        elif(time.hour >= 12 and time.hour <= 24):
            time_of_day = "pm"
            seconds = (((hour - 12) * 60) + minute)  * 60
        else:
            return False

        data = {
        "id": {
            "name": name,
            "index": index
        },
        "allows_overrun": True,
        "count_down_to_time": {
            "time_of_day": seconds,
            "period": time_of_day
        }
        }

        return self.put_command(f"timer/{index}", json=data)

