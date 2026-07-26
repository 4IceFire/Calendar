"""ChurchStaq / CCB v2 API client and reusable service workflow branches.

This module deliberately has no Flask or TDeck authorization dependencies.  It
handles OAuth, HTTP/pagination, service discovery, and normalization.  The Web
UI owns persistence and the permission effects of a reviewed roster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

import requests


CCB_API_BASE = "https://api.ccbchurch.com"
CCB_AUTHORIZE_URL = "https://oauth.ccbchurch.com/oauth/authorize"
CCB_ACCEPT = "application/vnd.ccbchurch.v2+json"
DEFAULT_SCOPES = "read:scheduling read:individuals write:scheduling"
GRANTING_STATUSES = {"PENDING", "ACCEPTED", "CHECKED_IN"}


class CCBError(RuntimeError):
    """A safe, user-displayable CCB integration error."""

    def __init__(self, message: str, *, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class CCBSecretStore:
    """Small local secret store excluded from TDeck config transport and Git.

    Environment variables take precedence, which lets a deployment avoid
    persisting credentials entirely.  The file is still useful for the current
    single-machine/LAN TDeck deployment.
    """

    ENV_KEYS = {
        "client_id": "TDECK_CCB_CLIENT_ID",
        "client_secret": "TDECK_CCB_CLIENT_SECRET",
        "subdomain": "TDECK_CCB_SUBDOMAIN",
        "companion_api_token": "TDECK_CCB_COMPANION_TOKEN",
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                data = payload if isinstance(payload, dict) else {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                data = {}
        for key, env_name in self.ENV_KEYS.items():
            value = str(os.environ.get(env_name) or "").strip()
            if value:
                data[key] = value
        return data

    def save(self, updates: dict[str, Any], *, preserve_blank: bool = True) -> dict[str, Any]:
        with self._lock:
            current = self.load()
            for key, value in (updates or {}).items():
                if preserve_blank and isinstance(value, str) and not value.strip():
                    continue
                if value is None:
                    current.pop(str(key), None)
                else:
                    current[str(key)] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return current

    def clear_tokens(self) -> None:
        self.save(
            {
                "access_token": None,
                "refresh_token": None,
                "expires_at": None,
                "scope": None,
            },
            preserve_blank=False,
        )


class CCBClient:
    """CCB v2 OAuth client with refresh, pagination, and bounded throttling."""

    def __init__(
        self,
        secret_store: CCBSecretStore,
        *,
        base_url: str = CCB_API_BASE,
        timeout: float = 15.0,
        schedule_min_interval: float = 2.05,
        session: requests.Session | None = None,
    ):
        self.secret_store = secret_store
        self.base_url = str(base_url).rstrip("/")
        self.timeout = max(1.0, float(timeout))
        self.schedule_min_interval = max(0.0, float(schedule_min_interval))
        self.session = session or requests.Session()
        self._request_lock = threading.RLock()
        self._last_schedule_request = 0.0

    def credentials_status(self) -> dict[str, Any]:
        data = self.secret_store.load()
        expires_at = _as_float(data.get("expires_at"))
        return {
            "configured": bool(data.get("client_id") and data.get("client_secret") and data.get("subdomain")),
            "connected": bool(data.get("access_token") and expires_at > time.time() + 5),
            "has_refresh_token": bool(data.get("refresh_token")),
            "subdomain": str(data.get("subdomain") or ""),
            "scope": str(data.get("scope") or ""),
            "expires_at": expires_at or None,
        }

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: str = DEFAULT_SCOPES) -> str:
        data = self.secret_store.load()
        client_id = str(data.get("client_id") or "").strip()
        if not client_id:
            raise CCBError("Enter the CCB client ID before connecting.")
        query = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": str(redirect_uri),
            "state": str(state),
            "scope": str(scopes or DEFAULT_SCOPES),
        }
        subdomain = str(data.get("subdomain") or "").strip()
        if subdomain:
            query["subdomain"] = subdomain
        return f"{CCB_AUTHORIZE_URL}?{urlencode(query)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        data = self.secret_store.load()
        payload = {
            "grant_type": "authorization_code",
            "subdomain": str(data.get("subdomain") or "").strip(),
            "client_id": str(data.get("client_id") or "").strip(),
            "client_secret": str(data.get("client_secret") or "").strip(),
            "code": str(code or "").strip(),
            "redirect_uri": str(redirect_uri or "").strip(),
        }
        if not all(payload.values()):
            raise CCBError("CCB OAuth configuration or authorization code is incomplete.")
        return self._token_request(payload)

    def refresh_access_token(self) -> dict[str, Any]:
        data = self.secret_store.load()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            raise CCBError("CCB needs to be connected again; no refresh token is available.")
        payload = {
            "grant_type": "refresh_token",
            "subdomain": str(data.get("subdomain") or "").strip(),
            "client_id": str(data.get("client_id") or "").strip(),
            "client_secret": str(data.get("client_secret") or "").strip(),
            "refresh_token": refresh_token,
        }
        return self._token_request(payload)

    def _token_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/oauth/token",
                json=payload,
                headers={"Accept": CCB_ACCEPT},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CCBError(f"Could not reach CCB OAuth: {exc}") from exc
        body = _response_json(response)
        if not response.ok:
            raise CCBError(
                _api_error_message(body, response.status_code, "CCB rejected the OAuth request"),
                status_code=response.status_code,
                details=body,
            )
        access_token = str(body.get("access_token") or "").strip() if isinstance(body, dict) else ""
        if not access_token:
            raise CCBError("CCB returned no access token.", details=body)
        expires_in = max(60, int(_as_float(body.get("expires_in")) or 7200))
        updates = {
            "access_token": access_token,
            "expires_at": time.time() + expires_in,
            "scope": str(body.get("scope") or ""),
        }
        if body.get("refresh_token"):
            updates["refresh_token"] = str(body.get("refresh_token"))
        self.secret_store.save(updates, preserve_blank=False)
        return {"expires_in": expires_in, "scope": updates["scope"], "has_refresh_token": bool(self.secret_store.load().get("refresh_token"))}

    def _access_token(self) -> str:
        data = self.secret_store.load()
        token = str(data.get("access_token") or "").strip()
        expires_at = _as_float(data.get("expires_at"))
        if token and expires_at > time.time() + 60:
            return token
        if data.get("refresh_token"):
            self.refresh_access_token()
            token = str(self.secret_store.load().get("access_token") or "").strip()
        if not token:
            raise CCBError("CCB is not connected. Connect it from Config > CCB.")
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retry_auth: bool = True,
    ) -> tuple[Any, requests.structures.CaseInsensitiveDict]:
        path_s = "/" + str(path or "").lstrip("/")
        with self._request_lock:
            if path_s.startswith("/scheduling/categories/") and path_s.endswith("/schedules"):
                remaining = self.schedule_min_interval - (time.monotonic() - self._last_schedule_request)
                if remaining > 0:
                    time.sleep(remaining)
            headers = {
                "Accept": CCB_ACCEPT,
                "Authorization": f"Bearer {self._access_token()}",
            }
            try:
                response = self.session.request(
                    str(method or "GET").upper(),
                    f"{self.base_url}{path_s}",
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise CCBError(f"Could not reach CCB: {exc}") from exc
            if path_s.startswith("/scheduling/categories/") and path_s.endswith("/schedules"):
                self._last_schedule_request = time.monotonic()
        if response.status_code == 401 and retry_auth and self.secret_store.load().get("refresh_token"):
            self.refresh_access_token()
            return self.request(method, path_s, params=params, json_body=json_body, retry_auth=False)
        body = _response_json(response)
        if not response.ok:
            raise CCBError(
                _api_error_message(body, response.status_code, "CCB request failed"),
                status_code=response.status_code,
                details=body,
            )
        return body, response.headers

    def get_all(self, path: str, *, params: dict[str, Any] | None = None) -> list[Any]:
        query = dict(params or {})
        query.setdefault("per_page", 100)
        query["page"] = 1
        items: list[Any] = []
        while True:
            body, headers = self.request("GET", path, params=query)
            page_items = body if isinstance(body, list) else _first_list(body)
            items.extend(page_items)
            total_pages = int(_as_float(headers.get("X-Total-Pages")) or 1)
            page = int(_as_float(headers.get("X-Page")) or query["page"])
            if page >= total_pages or not page_items:
                break
            query["page"] = page + 1
        return items

    def categories(self) -> list[dict[str, Any]]:
        body, _ = self.request("GET", "/scheduling/categories")
        return [dict(item) for item in (body if isinstance(body, list) else _first_list(body)) if isinstance(item, dict)]

    def schedules(
        self,
        category_id: int | str,
        *,
        after: str | None = None,
        before: str | None = None,
        schedule_ids: Iterable[int | str] | None = None,
        full: bool = False,
        sort: str = "ASC",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"sort": str(sort).upper(), "getFullSchedules": "true" if full else "false"}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        ids = [str(item) for item in (schedule_ids or []) if str(item).strip()]
        if ids:
            params["schedule_ids"] = ",".join(ids)
        return [dict(item) for item in self.get_all(f"/scheduling/categories/{int(category_id)}/schedules", params=params) if isinstance(item, dict)]

    def service_plans(self, category_id: int | str) -> list[dict[str, Any]]:
        body, _ = self.request("GET", f"/scheduling/categories/{int(category_id)}/service_plans")
        return [dict(item) for item in (body if isinstance(body, list) else _first_list(body)) if isinstance(item, dict)]

    def individuals(self, query: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"include_inactive": "false", "campus_scope": "all"}
        if str(query or "").strip():
            params["name"] = str(query).strip()
        return [dict(item) for item in self.get_all("/individuals", params=params) if isinstance(item, dict)]

    def event_position_candidates(self, event_position_id: int | str) -> list[dict[str, Any]]:
        body, _ = self.request("GET", f"/scheduling/event_positions/{int(event_position_id)}/candidates")
        volunteers = body if isinstance(body, list) else _first_list(body)
        return [dict(item) for item in volunteers if isinstance(item, dict)]

    def event(self, *, category_id: int, schedule_id: int, event_id: int, full: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        schedules = self.schedules(category_id, schedule_ids=[schedule_id], full=full)
        for schedule in schedules:
            for event in _list_value(schedule, "events"):
                if _id(event.get("id")) == int(event_id):
                    return schedule, dict(event)
        raise CCBError(f"CCB service event {event_id} was not found in schedule {schedule_id}.")


@dataclass(frozen=True)
class ServiceRef:
    event_id: int
    schedule_id: int
    category_id: int
    name: str
    start: str
    end: str
    schedule_name: str = ""
    service_plan_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "event_id": self.event_id,
            "schedule_id": self.schedule_id,
            "category_id": self.category_id,
            "name": self.name,
            "schedule_name": self.schedule_name,
            "start": self.start,
            "end": self.end,
            "service_plan_id": self.service_plan_id,
            "raw": self.raw,
        }


def discover_services(schedules: Iterable[dict[str, Any]], category_id: int) -> list[ServiceRef]:
    output: list[ServiceRef] = []
    for schedule in schedules or []:
        schedule_id = _id(schedule.get("id"))
        if not schedule_id:
            continue
        schedule_name = str(schedule.get("name") or "").strip()
        for event in _list_value(schedule, "events"):
            event_id = _id(event.get("id"))
            if not event_id:
                continue
            event_name = str(event.get("name") or schedule_name or f"Service {event_id}").strip()
            output.append(
                ServiceRef(
                    event_id=event_id,
                    schedule_id=schedule_id,
                    category_id=int(category_id),
                    name=event_name,
                    start=str(event.get("start") or schedule.get("start") or ""),
                    end=str(event.get("end") or schedule.get("end") or ""),
                    schedule_name=schedule_name,
                    service_plan_id=_id(event.get("service_plan_id")) or None,
                    raw=dict(event),
                )
            )
    output.sort(key=lambda item: (item.start, item.name.lower(), item.event_id))
    return output


def normalize_roster(
    event: dict[str, Any],
    *,
    candidate_loader: Callable[[int], list[dict[str, Any]]] | None = None,
    candidate_position_names: set[str] | None = None,
    candidate_position_pools: dict[str, str] | None = None,
) -> dict[str, Any]:
    positions: list[dict[str, Any]] = []
    people: dict[int, dict[str, Any]] = {}
    eligible: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    pool_by_position = {
        str(name).strip().casefold(): str(pool).strip()
        for name, pool in (candidate_position_pools or {}).items()
        if str(name).strip() and str(pool).strip()
    }
    candidate_names = {str(name).strip().casefold() for name in (candidate_position_names or set()) if str(name).strip()}
    candidate_names.update(pool_by_position)

    for event_team in _list_value(event, "event_teams"):
        team = event_team.get("team") if isinstance(event_team.get("team"), dict) else {}
        team_name = str(team.get("name") or event_team.get("name") or "").strip()
        team_id = _id(team.get("id")) or _id(event_team.get("team_id"))
        team_position_names: dict[int, str] = {}
        for base_position in _list_value(team, "positions"):
            pid = _id(base_position.get("id"))
            if pid:
                team_position_names[pid] = str(base_position.get("name") or "").strip()

        for event_position in _list_value(event_team, "event_positions"):
            position_obj = event_position.get("position") if isinstance(event_position.get("position"), dict) else {}
            position_id = _id(position_obj.get("id")) or _id(event_position.get("position_id"))
            event_position_id = _id(event_position.get("id"))
            position_name = str(position_obj.get("name") or team_position_names.get(position_id) or f"Position {position_id}").strip()
            assignments: list[dict[str, Any]] = []
            for assignment in _list_value(event_position, "assignments"):
                status = str(assignment.get("status") or "PENDING").upper().strip()
                volunteer = assignment.get("volunteer") if isinstance(assignment.get("volunteer"), dict) else {}
                individual = volunteer.get("individual") if isinstance(volunteer.get("individual"), dict) else {}
                individual_id = _id(individual.get("id"))
                if not individual_id:
                    continue
                person = _normalize_person(individual)
                people[individual_id] = person
                assignments.append(
                    {
                        "assignment_id": _id(assignment.get("id")) or None,
                        "individual_id": individual_id,
                        "name": person["name"],
                        "email": person["email"],
                        "status": status,
                        "grants_access": status in GRANTING_STATUSES,
                        "raw": dict(assignment),
                    }
                )
            position_record = {
                "event_position_id": event_position_id or None,
                "position_id": position_id or None,
                "position_name": position_name,
                "team_id": team_id or None,
                "team_name": team_name,
                "assignments": assignments,
                "raw": dict(event_position),
            }
            positions.append(position_record)

            if candidate_loader and event_position_id and position_name.casefold() in candidate_names:
                try:
                    candidates = candidate_loader(event_position_id)
                    for volunteer in candidates:
                        individual = volunteer.get("individual") if isinstance(volunteer.get("individual"), dict) else volunteer
                        individual_id = _id(individual.get("id"))
                        if individual_id:
                            person = eligible.setdefault(individual_id, _normalize_person(individual))
                            pool_name = pool_by_position.get(position_name.casefold())
                            if pool_name:
                                person["pools"] = sorted(set(person.get("pools") or []) | {pool_name})
                except CCBError as exc:
                    warnings.append(f"Could not load eligible candidates for {position_name}: {exc}")

    return {
        "event_id": _id(event.get("id")) or None,
        "positions": positions,
        "people": sorted(people.values(), key=lambda p: (p["name"].casefold(), p["id"])),
        "eligible_people": sorted(eligible.values(), key=lambda p: (p["name"].casefold(), p["id"])),
        "warnings": warnings,
        "raw": dict(event),
    }


def normalize_service_plan(plan: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    event_start = _parse_datetime(event.get("start"))
    if event_start is None:
        warnings.append("The CCB service has no parseable start time; item start times could not be calculated.")
    offset_seconds = int(_as_float(plan.get("event_starttime_offset")) or 0)
    cursor = event_start + timedelta(seconds=offset_seconds) if event_start else None
    normalized_items: list[dict[str, Any]] = []
    items = [dict(item) for item in _list_value(plan, "items")]
    items.sort(key=lambda item: (int(_as_float(item.get("order_by")) or 0), _id(item.get("id"))))
    for index, item in enumerate(items):
        duration_seconds = max(0, int(_as_float(item.get("duration")) or 0))
        start_at = cursor
        end_at = cursor + timedelta(seconds=duration_seconds) if cursor else None
        normalized_items.append(
            {
                "id": _id(item.get("id")) or None,
                "service_plan_id": _id(item.get("service_plan_id")) or _id(plan.get("id")) or None,
                "order": int(_as_float(item.get("order_by")) or index),
                "index": index,
                "item_type": str(item.get("item_type") or "ITEM").upper(),
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or ""),
                "duration_seconds": duration_seconds,
                "start": _iso(start_at),
                "end": _iso(end_at),
                "links": list(item.get("links") or []) if isinstance(item.get("links"), list) else [],
                "files": list(item.get("files") or []) if isinstance(item.get("files"), list) else [],
                "raw": item,
            }
        )
        if cursor:
            cursor = end_at
    documented_duration = max(0, int(_as_float(plan.get("duration")) or 0))
    calculated_duration = sum(int(item["duration_seconds"]) for item in normalized_items)
    if documented_duration and documented_duration != calculated_duration:
        warnings.append(
            f"CCB plan duration ({documented_duration}s) differs from the item total ({calculated_duration}s)."
        )
    return {
        "plan_id": _id(plan.get("id")) or None,
        "name": str(plan.get("name") or "").strip(),
        "event_id": _id(event.get("id")) or None,
        "event_start": str(event.get("start") or ""),
        "event_start_offset_seconds": offset_seconds,
        "planned_start": _iso(event_start + timedelta(seconds=offset_seconds)) if event_start else None,
        "duration_seconds": documented_duration or calculated_duration,
        "calculated_duration_seconds": calculated_duration,
        "items": normalized_items,
        "warnings": warnings,
        "raw": dict(plan),
    }


@dataclass
class BranchResult:
    name: str
    ok: bool
    data: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "data": self.data,
            "warnings": list(self.warnings),
            "error": self.error,
        }


class CCBWorkflow:
    """Expandable registry for individually runnable CCB service branches."""

    def __init__(self, client: CCBClient):
        self.client = client
        self._branches: dict[str, Callable[..., BranchResult]] = {}
        self._event_cache: dict[tuple[int, int, int], tuple[dict[str, Any], dict[str, Any]]] = {}
        self.register("roster", self._pull_roster)
        self.register("runsheet", self._pull_runsheet)

    @property
    def branch_names(self) -> list[str]:
        return list(self._branches)

    def register(self, name: str, handler: Callable[..., BranchResult]) -> None:
        key = str(name or "").strip().lower()
        if not key:
            raise ValueError("branch name is required")
        self._branches[key] = handler

    def pull(
        self,
        *,
        selected_service: dict[str, Any],
        sources: dict[str, dict[str, Any]],
        branches: Iterable[str] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested = [str(name).strip().lower() for name in (branches or self.branch_names)]
        results: dict[str, dict[str, Any]] = {}
        for name in requested:
            handler = self._branches.get(name)
            if not handler:
                results[name] = BranchResult(name=name, ok=False, error=f"Unknown CCB branch: {name}").as_dict()
                continue
            source = sources.get(name) or selected_service
            try:
                result = handler(service=source, options=options or {})
            except CCBError as exc:
                result = BranchResult(name=name, ok=False, error=str(exc))
            except Exception as exc:
                result = BranchResult(name=name, ok=False, error=f"Unexpected {name} error: {exc}")
            results[name] = result.as_dict()
        return {
            "ok": all(bool(item.get("ok")) for item in results.values()) if results else True,
            "selected_service_id": _id(selected_service.get("event_id") or selected_service.get("id")),
            "branches": results,
        }

    def _event_for_service(self, service: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        key = (
            int(service.get("category_id")),
            int(service.get("schedule_id")),
            int(service.get("event_id") or service.get("id")),
        )
        if key not in self._event_cache:
            self._event_cache[key] = self.client.event(
                category_id=key[0], schedule_id=key[1], event_id=key[2], full=True,
            )
        return self._event_cache[key]

    def _pull_roster(self, *, service: dict[str, Any], options: dict[str, Any]) -> BranchResult:
        schedule, event = self._event_for_service(service)
        candidate_names = {
            str(name).strip() for name in (options.get("candidate_position_names") or []) if str(name).strip()
        }
        candidate_pools = {
            str(name).strip(): str(pool).strip()
            for name, pool in (options.get("candidate_position_pools") or {}).items()
            if str(name).strip() and str(pool).strip()
        }
        data = normalize_roster(
            event,
            candidate_loader=lambda event_position_id: self.client.event_position_candidates(event_position_id),
            candidate_position_names=candidate_names,
            candidate_position_pools=candidate_pools,
        )
        data["schedule"] = schedule
        data["source_service"] = dict(service)
        return BranchResult(name="roster", ok=True, data=data, warnings=list(data.get("warnings") or []))

    def _pull_runsheet(self, *, service: dict[str, Any], options: dict[str, Any]) -> BranchResult:
        _schedule, event = self._event_for_service(service)
        plan_id = _id(event.get("service_plan_id")) or _id(service.get("service_plan_id"))
        if not plan_id:
            raise CCBError("This CCB service has no service plan/runsheet assigned.")
        plans = self.client.service_plans(int(service.get("category_id")))
        plan = next((item for item in plans if _id(item.get("id")) == plan_id), None)
        if not plan:
            raise CCBError(f"CCB service plan {plan_id} was not returned for this category.")
        data = normalize_service_plan(plan, event)
        data["source_service"] = dict(service)
        return BranchResult(name="runsheet", ok=True, data=data, warnings=list(data.get("warnings") or []))


def _response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        text = str(getattr(response, "text", "") or "").strip()
        return {"message": text[:1000]} if text else {}


def _api_error_message(body: Any, status: int, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "error_description", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return f"{value.strip()} (HTTP {status})"
    return f"{fallback} (HTTP {status})"


def _first_list(value: Any) -> list[Any]:
    if isinstance(value, dict):
        for key in ("data", "items", "results", "schedules", "categories", "individuals"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _list_value(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    candidate = value.get(key)
    if not isinstance(candidate, list):
        return []
    return [item for item in candidate if isinstance(item, dict)]


def _normalize_person(individual: dict[str, Any]) -> dict[str, Any]:
    individual_id = _id(individual.get("id"))
    name = str(individual.get("name") or "").strip()
    if not name:
        name = " ".join(
            item for item in (
                str(individual.get("first_name") or "").strip(),
                str(individual.get("last_name") or "").strip(),
            ) if item
        )
    return {
        "id": individual_id,
        "name": name or f"CCB person {individual_id}",
        "email": str(individual.get("email") or "").strip(),
        "active": bool(individual.get("active", True)),
        "raw": dict(individual),
    }


def _id(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
