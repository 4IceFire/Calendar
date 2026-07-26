"""TDeck persistence and authorization projection for the CCB integration."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from ccb import GRANTING_STATUSES


def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CCBStoreError(RuntimeError):
    pass


class CCBTDeckStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def setting(self, key: str, default: Any = None) -> Any:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value_json FROM ccb_settings WHERE key=?", (str(key),)).fetchone()
        finally:
            conn.close()
        if not row:
            return default
        try:
            return json.loads(str(row["value_json"] or "null"))
        except (ValueError, TypeError):
            return default

    def set_setting(self, key: str, value: Any, *, conn: sqlite3.Connection | None = None) -> None:
        owned = conn is None
        db = conn or self.connect()
        try:
            db.execute(
                "INSERT INTO ccb_settings(key,value_json) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (str(key), json.dumps(value, ensure_ascii=False)),
            )
            if owned:
                db.commit()
        finally:
            if owned:
                db.close()

    def roles(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            where = "" if include_inactive else "WHERE is_active=1"
            rows = conn.execute(
                f"SELECT role_key,name,display_category,sort_order,allocation_pool,is_active "
                f"FROM ccb_roles {where} ORDER BY sort_order,lower(name)"
            ).fetchall()
        finally:
            conn.close()
        return [_row_dict(row) for row in rows]

    def save_roles(self, roles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(roles or []):
            key = _slug(item.get("role_key") or item.get("name"))
            name = str(item.get("name") or "").strip()
            category = str(item.get("display_category") or "Production").strip()
            if not key or not name or key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "role_key": key,
                    "name": name,
                    "display_category": category,
                    "sort_order": _int(item.get("sort_order"), (index + 1) * 10),
                    "allocation_pool": str(item.get("allocation_pool") or "").strip() or None,
                    "is_active": bool(item.get("is_active", True)),
                }
            )
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = {str(row["role_key"]) for row in conn.execute("SELECT role_key FROM ccb_roles").fetchall()}
            for item in normalized:
                conn.execute(
                    """
                    INSERT INTO ccb_roles(role_key,name,display_category,sort_order,allocation_pool,is_active)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(role_key) DO UPDATE SET
                      name=excluded.name,display_category=excluded.display_category,
                      sort_order=excluded.sort_order,allocation_pool=excluded.allocation_pool,
                      is_active=excluded.is_active
                    """,
                    (
                        item["role_key"], item["name"], item["display_category"],
                        item["sort_order"], item["allocation_pool"], 1 if item["is_active"] else 0,
                    ),
                )
            for missing in existing - seen:
                conn.execute("UPDATE ccb_roles SET is_active=0 WHERE role_key=?", (missing,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.roles(include_inactive=True)

    def positions(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT id,ccb_position_id,name,mapping_mode,tdeck_role_key,pool_name,last_seen_at
                FROM ccb_positions
                ORDER BY lower(name)
                """
            ).fetchall()
        finally:
            conn.close()
        return [_row_dict(row) for row in rows]

    def save_positions(self, positions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item in positions or []:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                mode = str(item.get("mapping_mode") or "display").strip().lower()
                if mode not in ("direct", "pool", "display"):
                    mode = "display"
                role_key = str(item.get("tdeck_role_key") or "").strip() or None
                pool_name = str(item.get("pool_name") or "").strip() or None
                if mode != "direct":
                    role_key = None
                if mode != "pool":
                    pool_name = None
                conn.execute(
                    """
                    INSERT INTO ccb_positions(ccb_position_id,name,mapping_mode,tdeck_role_key,pool_name)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(name) DO UPDATE SET
                      ccb_position_id=COALESCE(excluded.ccb_position_id,ccb_positions.ccb_position_id),
                      mapping_mode=excluded.mapping_mode,
                      tdeck_role_key=excluded.tdeck_role_key,
                      pool_name=excluded.pool_name
                    """,
                    (_nullable_int(item.get("ccb_position_id")), name, mode, role_key, pool_name),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.positions()

    def cache_services(self, services: Iterable[dict[str, Any]]) -> int:
        count = 0
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item in services or []:
                event_id = _int(item.get("event_id") or item.get("id"))
                schedule_id = _int(item.get("schedule_id"))
                category_id = _int(item.get("category_id"))
                if not event_id or not schedule_id or not category_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO ccb_services(
                      event_id,schedule_id,category_id,name,schedule_name,start_at,end_at,
                      service_plan_id,raw_json,discovered_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                      schedule_id=excluded.schedule_id,category_id=excluded.category_id,
                      name=excluded.name,schedule_name=excluded.schedule_name,
                      start_at=excluded.start_at,end_at=excluded.end_at,
                      service_plan_id=excluded.service_plan_id,raw_json=excluded.raw_json,
                      discovered_at=excluded.discovered_at
                    """,
                    (
                        event_id, schedule_id, category_id,
                        str(item.get("name") or f"Service {event_id}"),
                        str(item.get("schedule_name") or ""),
                        str(item.get("start") or item.get("start_at") or ""),
                        str(item.get("end") or item.get("end_at") or ""),
                        _nullable_int(item.get("service_plan_id")),
                        _json(item.get("raw") or {}), now_string(),
                    ),
                )
                count += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return count

    def services(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT event_id,schedule_id,category_id,name,schedule_name,start_at,end_at,
                       service_plan_id,discovered_at
                FROM ccb_services
                ORDER BY start_at,lower(name)
                """
            ).fetchall()
            active = conn.execute("SELECT selected_event_id FROM ccb_active_service WHERE singleton_id=1").fetchone()
        finally:
            conn.close()
        active_id = int(active["selected_event_id"]) if active else None
        result = []
        for row in rows:
            item = _service_row(row)
            item["active"] = item["event_id"] == active_id
            result.append(item)
        return result

    def service(self, event_id: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM ccb_services WHERE event_id=?", (int(event_id),)).fetchone()
        finally:
            conn.close()
        if not row:
            raise CCBStoreError(f"CCB service {event_id} is not in the local service cache.")
        return _service_row(row)

    def set_sources(self, selected_event_id: int, sources: dict[str, int]) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for branch, source_event_id in (sources or {}).items():
                if not source_event_id:
                    continue
                exists = conn.execute("SELECT 1 FROM ccb_services WHERE event_id=?", (int(source_event_id),)).fetchone()
                if not exists:
                    raise CCBStoreError(f"Source service {source_event_id} is not cached.")
                conn.execute(
                    """
                    INSERT INTO ccb_service_sources(selected_event_id,branch_name,source_event_id)
                    VALUES (?,?,?)
                    ON CONFLICT(selected_event_id,branch_name)
                    DO UPDATE SET source_event_id=excluded.source_event_id
                    """,
                    (int(selected_event_id), str(branch), int(source_event_id)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sources(self, selected_event_id: int, branches: Iterable[str]) -> dict[str, dict[str, Any]]:
        selected = self.service(selected_event_id)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT branch_name,source_event_id FROM ccb_service_sources WHERE selected_event_id=?",
                (int(selected_event_id),),
            ).fetchall()
        finally:
            conn.close()
        source_ids = {str(row["branch_name"]): int(row["source_event_id"]) for row in rows}
        return {str(branch): self.service(source_ids.get(str(branch), int(selected_event_id))) for branch in branches}

    def start_workflow_run(self, selected_event_id: int, branches: Iterable[str], trigger_source: str) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO ccb_workflow_runs(
                  selected_event_id,trigger_source,requested_branches_json,started_at,status
                ) VALUES (?,?,?,?,?)
                """,
                (int(selected_event_id), str(trigger_source), _json(list(branches)), now_string(), "running"),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def store_workflow_result(
        self,
        run_id: int,
        selected_event_id: int,
        result: dict[str, Any],
        sources: dict[str, dict[str, Any]],
        *,
        actor_user_id: int | None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for branch_name, branch in (result.get("branches") or {}).items():
                source_event_id = _int((sources.get(branch_name) or {}).get("event_id")) or int(selected_event_id)
                status = "success" if branch.get("ok") else "failure"
                data = branch.get("data") if isinstance(branch.get("data"), dict) else None
                warnings = list(branch.get("warnings") or [])
                conn.execute(
                    """
                    INSERT INTO ccb_service_branches(
                      selected_event_id,branch_name,source_event_id,status,pulled_at,
                      data_json,warnings_json,error
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(selected_event_id,branch_name) DO UPDATE SET
                      source_event_id=excluded.source_event_id,status=excluded.status,
                      pulled_at=excluded.pulled_at,
                      data_json=COALESCE(excluded.data_json,ccb_service_branches.data_json),
                      warnings_json=excluded.warnings_json,error=excluded.error
                    """,
                    (
                        int(selected_event_id), str(branch_name), source_event_id, status,
                        now_string(), _json(data) if data is not None else None,
                        _json(warnings), str(branch.get("error") or "") or None,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ccb_workflow_branch_runs(
                      workflow_run_id,branch_name,source_event_id,status,warnings_json,error
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (int(run_id), str(branch_name), source_event_id, status, _json(warnings), str(branch.get("error") or "") or None),
                )
                if branch.get("ok") and branch_name == "roster" and data is not None:
                    self._store_roster(conn, int(selected_event_id), data, actor_user_id=actor_user_id)
                if branch.get("ok") and branch_name == "runsheet" and data is not None:
                    self._store_runsheet(conn, int(selected_event_id), source_event_id, data)
            overall = "success" if result.get("ok") else (
                "partial" if any(bool(item.get("ok")) for item in (result.get("branches") or {}).values()) else "failure"
            )
            conn.execute(
                "UPDATE ccb_workflow_runs SET completed_at=?,status=?,summary_json=? WHERE id=?",
                (now_string(), overall, _json(result), int(run_id)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_workflow_run(self, run_id: int, error: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE ccb_workflow_runs SET completed_at=?,status='failure',summary_json=? WHERE id=?",
                (now_string(), _json({"error": str(error)}), int(run_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def _store_roster(
        self,
        conn: sqlite3.Connection,
        selected_event_id: int,
        data: dict[str, Any],
        *,
        actor_user_id: int | None,
    ) -> None:
        current_time = now_string()
        positions_by_name = {
            str(row["name"]).casefold(): row
            for row in conn.execute("SELECT * FROM ccb_positions").fetchall()
        }
        status_by_person: dict[str, str] = {}
        valid_people: set[str] = set()
        auto_by_role: dict[str, list[tuple[str, str, str]]] = {}
        all_people: dict[str, dict[str, Any]] = {}
        for person in list(data.get("people") or []) + list(data.get("eligible_people") or []):
            if isinstance(person, dict) and _int(person.get("id")):
                all_people[str(_int(person.get("id")))] = person
        for person in data.get("eligible_people") or []:
            pid = str(_int(person.get("id")))
            if pid != "0":
                valid_people.add(pid)
                status_by_person.setdefault(pid, "ELIGIBLE")

        for position in data.get("positions") or []:
            if not isinstance(position, dict):
                continue
            name = str(position.get("position_name") or "").strip()
            if not name:
                continue
            raw_position = position.get("raw") if isinstance(position.get("raw"), dict) else {}
            conn.execute(
                """
                INSERT INTO ccb_positions(ccb_position_id,name,last_seen_at,raw_json)
                VALUES (?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  ccb_position_id=COALESCE(excluded.ccb_position_id,ccb_positions.ccb_position_id),
                  last_seen_at=excluded.last_seen_at,raw_json=excluded.raw_json
                """,
                (_nullable_int(position.get("position_id")), name, current_time, _json(raw_position)),
            )
            mapping = positions_by_name.get(name.casefold())
            if mapping is None:
                mapping = conn.execute("SELECT * FROM ccb_positions WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                positions_by_name[name.casefold()] = mapping
            for assignment in position.get("assignments") or []:
                if not isinstance(assignment, dict):
                    continue
                pid = str(_int(assignment.get("individual_id")))
                if pid == "0":
                    continue
                status = str(assignment.get("status") or "PENDING").upper()
                status_by_person[pid] = status
                if status in GRANTING_STATUSES:
                    valid_people.add(pid)
                if mapping and str(mapping["mapping_mode"] or "") == "direct" and mapping["tdeck_role_key"] and status in GRANTING_STATUSES:
                    auto_by_role.setdefault(str(mapping["tdeck_role_key"]), []).append((pid, name, status))

        for pid, person in all_people.items():
            existing = conn.execute(
                "SELECT id FROM external_user_identities WHERE provider='ccb' AND external_id=?",
                (pid,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE external_user_identities
                    SET display_name=?,email=?,last_seen_at=?,raw_json=?
                    WHERE provider='ccb' AND external_id=?
                    """,
                    (
                        str(person.get("name") or ""), str(person.get("email") or ""),
                        current_time, _json(person.get("raw") or person), pid,
                    ),
                )

        local_vocalists = {
            str(row["external_id"])
            for row in conn.execute(
                "SELECT external_id FROM external_user_identities WHERE provider='ccb' AND is_vocalist=1"
            ).fetchall()
        }
        valid_people.update(local_vocalists)

        allocations = conn.execute(
            "SELECT role_key,ccb_individual_id,source FROM ccb_role_allocations WHERE selected_event_id=?",
            (int(selected_event_id),),
        ).fetchall()
        for allocation in allocations:
            pid = str(allocation["ccb_individual_id"])
            status = status_by_person.get(pid)
            if pid not in valid_people or status == "DECLINED":
                conn.execute(
                    "DELETE FROM ccb_role_allocations WHERE selected_event_id=? AND role_key=? AND ccb_individual_id=?",
                    (int(selected_event_id), str(allocation["role_key"]), pid),
                )

        conn.execute("DELETE FROM ccb_role_allocations WHERE selected_event_id=? AND source='auto'", (int(selected_event_id),))
        for role_key, assignments in auto_by_role.items():
            manual_exists = conn.execute(
                "SELECT 1 FROM ccb_role_allocations WHERE selected_event_id=? AND role_key=? AND source='manual' LIMIT 1",
                (int(selected_event_id), role_key),
            ).fetchone()
            if manual_exists:
                continue
            for pid, position_name, status in assignments:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ccb_role_allocations(
                      selected_event_id,role_key,ccb_individual_id,source,
                      ccb_position_name,assignment_status,updated_at,updated_by
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (int(selected_event_id), role_key, pid, "auto", position_name, status, current_time, actor_user_id),
                )

    def _store_runsheet(
        self,
        conn: sqlite3.Connection,
        selected_event_id: int,
        source_event_id: int,
        data: dict[str, Any],
    ) -> None:
        conn.execute("DELETE FROM ccb_runsheet_items WHERE selected_event_id=?", (int(selected_event_id),))
        for index, item in enumerate(data.get("items") or []):
            if not isinstance(item, dict):
                continue
            conn.execute(
                """
                INSERT INTO ccb_runsheet_items(
                  selected_event_id,source_event_id,plan_id,item_id,item_order,item_index,
                  item_type,name,description,duration_seconds,starts_at,ends_at,
                  links_json,files_json,raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(selected_event_id), int(source_event_id), _nullable_int(data.get("plan_id")),
                    _nullable_int(item.get("id")), _int(item.get("order"), index), index,
                    str(item.get("item_type") or "ITEM"), str(item.get("name") or ""),
                    str(item.get("description") or ""), max(0, _int(item.get("duration_seconds"))),
                    item.get("start"), item.get("end"), _json(item.get("links") or []),
                    _json(item.get("files") or []), _json(item.get("raw") or {}),
                ),
            )

    def service_state(self, selected_event_id: int) -> dict[str, Any]:
        service = self.service(selected_event_id)
        conn = self.connect()
        try:
            branches = conn.execute(
                "SELECT * FROM ccb_service_branches WHERE selected_event_id=? ORDER BY branch_name",
                (int(selected_event_id),),
            ).fetchall()
            allocations = conn.execute(
                "SELECT * FROM ccb_role_allocations WHERE selected_event_id=? ORDER BY role_key,ccb_individual_id",
                (int(selected_event_id),),
            ).fetchall()
            roles = conn.execute(
                """
                SELECT role_key,name,display_category,sort_order,allocation_pool
                FROM ccb_roles WHERE is_active=1 ORDER BY sort_order,lower(name)
                """
            ).fetchall()
            group_roles = conn.execute(
                """
                SELECT cgr.role_key,g.id AS group_id,g.name AS group_name
                FROM ccb_group_roles cgr JOIN groups g ON g.id=cgr.group_id
                ORDER BY lower(g.name)
                """
            ).fetchall()
            identities = conn.execute(
                """
                SELECT e.external_id,e.user_id,e.display_name,e.email,e.is_vocalist,
                       u.username,u.full_name,u.is_active,u.is_locked
                FROM external_user_identities e
                JOIN users u ON u.id=e.user_id
                WHERE e.provider='ccb'
                """
            ).fetchall()
            users = conn.execute(
                "SELECT id,username,full_name,email,is_active,is_locked FROM users ORDER BY lower(username)"
            ).fetchall()
            active = conn.execute("SELECT * FROM ccb_active_service WHERE singleton_id=1").fetchone()
            runsheet_count = conn.execute(
                "SELECT count(*) AS c FROM ccb_runsheet_items WHERE selected_event_id=?",
                (int(selected_event_id),),
            ).fetchone()
        finally:
            conn.close()

        branch_payload: dict[str, Any] = {}
        roster: dict[str, Any] = {}
        for row in branches:
            item = _row_dict(row)
            item["warnings"] = _loads(item.pop("warnings_json", None), [])
            item["data"] = _loads(item.pop("data_json", None), None)
            branch_payload[str(row["branch_name"])] = item
            if row["branch_name"] == "roster" and isinstance(item.get("data"), dict):
                roster = item["data"]

        identity_by_external = {str(row["external_id"]): _row_dict(row) for row in identities}
        email_users: dict[str, list[dict[str, Any]]] = {}
        for row in users:
            user = _row_dict(row)
            email = str(user.get("email") or "").strip().casefold()
            if email:
                email_users.setdefault(email, []).append(user)

        status_by_person: dict[str, str] = {}
        pools_by_person: dict[str, set[str]] = {}
        position_mappings = {str(row["name"]).casefold(): row for row in self._position_rows()}
        people_by_id: dict[str, dict[str, Any]] = {}
        for person in list(roster.get("people") or []) + list(roster.get("eligible_people") or []):
            if not isinstance(person, dict) or not _int(person.get("id")):
                continue
            pid = str(_int(person.get("id")))
            people_by_id[pid] = dict(person)
        eligible_ids = {str(_int(person.get("id"))) for person in roster.get("eligible_people") or [] if _int(person.get("id"))}
        for person in roster.get("eligible_people") or []:
            pid = str(_int(person.get("id")))
            if pid == "0":
                continue
            pools = {str(pool) for pool in (person.get("pools") or []) if str(pool)} or {"vocals"}
            pools_by_person.setdefault(pid, set()).update(pools)
            status_by_person.setdefault(pid, "ELIGIBLE")
        for position in roster.get("positions") or []:
            if not isinstance(position, dict):
                continue
            mapping = position_mappings.get(str(position.get("position_name") or "").casefold())
            pool_name = str(mapping["pool_name"] or "") if mapping and str(mapping["mapping_mode"] or "") == "pool" else ""
            for assignment in position.get("assignments") or []:
                pid = str(_int(assignment.get("individual_id")))
                if pid == "0":
                    continue
                status_by_person[pid] = str(assignment.get("status") or "PENDING").upper()
                if pool_name:
                    pools_by_person.setdefault(pid, set()).add(pool_name)

        for pid, identity in identity_by_external.items():
            if bool(identity.get("is_vocalist")):
                pools_by_person.setdefault(pid, set()).add("vocals")
                if pid not in people_by_id:
                    people_by_id[pid] = {
                        "id": _int(pid),
                        "name": str(identity.get("display_name") or identity.get("full_name") or identity.get("username") or f"CCB person {pid}"),
                        "email": str(identity.get("email") or ""),
                        "raw": {},
                    }
                    status_by_person.setdefault(pid, "MANUAL")

        people: list[dict[str, Any]] = []
        for pid, person in people_by_id.items():
            item = dict(person)
            item["id"] = _int(pid)
            item["status"] = status_by_person.get(pid, "ELIGIBLE" if pid in eligible_ids else "PENDING")
            item["pools"] = sorted(pools_by_person.get(pid, set()))
            identity = identity_by_external.get(pid)
            item["linked_user"] = identity
            suggestion = None
            if not identity:
                candidates = email_users.get(str(item.get("email") or "").strip().casefold(), [])
                if len(candidates) == 1:
                    suggestion = candidates[0]
            item["suggested_user"] = suggestion
            people.append(item)
        people.sort(key=lambda item: (str(item.get("name") or "").casefold(), _int(item.get("id"))))

        allocations_by_role: dict[str, list[dict[str, Any]]] = {}
        for row in allocations:
            allocations_by_role.setdefault(str(row["role_key"]), []).append(_row_dict(row))
        groups_by_role: dict[str, list[dict[str, Any]]] = {}
        for row in group_roles:
            groups_by_role.setdefault(str(row["role_key"]), []).append(
                {"id": int(row["group_id"]), "name": str(row["group_name"])}
            )
        role_payload = []
        for row in roles:
            role = _row_dict(row)
            role["allocations"] = allocations_by_role.get(str(row["role_key"]), [])
            role["groups"] = groups_by_role.get(str(row["role_key"]), [])
            role_payload.append(role)

        return {
            "service": service,
            "sources": {name: source for name, source in self._source_ids(selected_event_id).items()},
            "branches": branch_payload,
            "positions": list(roster.get("positions") or []),
            "people": people,
            "roles": role_payload,
            "users": [_row_dict(row) for row in users],
            "unmatched_people": [item for item in people if not item.get("linked_user")],
            "active_service": _row_dict(active) if active else None,
            "runsheet_item_count": int(runsheet_count["c"] or 0) if runsheet_count else 0,
        }

    def _position_rows(self) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute("SELECT * FROM ccb_positions").fetchall()
        finally:
            conn.close()

    def _source_ids(self, selected_event_id: int) -> dict[str, int]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT branch_name,source_event_id FROM ccb_service_sources WHERE selected_event_id=?",
                (int(selected_event_id),),
            ).fetchall()
        finally:
            conn.close()
        return {str(row["branch_name"]): int(row["source_event_id"]) for row in rows}

    def save_allocations(
        self,
        selected_event_id: int,
        allocations: dict[str, list[Any]],
        *,
        actor_user_id: int | None,
    ) -> dict[str, Any]:
        state = self.service_state(selected_event_id)
        valid_roles = {str(role["role_key"]): role for role in state["roles"]}
        people = {str(_int(person.get("id"))): person for person in state["people"]}
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM ccb_role_allocations WHERE selected_event_id=?", (int(selected_event_id),))
            for role_key, person_ids in (allocations or {}).items():
                role = valid_roles.get(str(role_key))
                if not role or not isinstance(person_ids, list):
                    continue
                pool = str(role.get("allocation_pool") or "")
                for raw_id in person_ids:
                    pid = str(_int(raw_id))
                    person = people.get(pid)
                    if pid == "0" or not person:
                        continue
                    if str(person.get("status") or "").upper() == "DECLINED":
                        continue
                    if pool and pool not in set(person.get("pools") or []):
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ccb_role_allocations(
                          selected_event_id,role_key,ccb_individual_id,source,
                          assignment_status,updated_at,updated_by
                        ) VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            int(selected_event_id), str(role_key), pid, "manual",
                            str(person.get("status") or "MANUAL"), now_string(), actor_user_id,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.service_state(selected_event_id)

    def link_identity(
        self,
        *,
        external_id: str,
        user_id: int,
        display_name: str = "",
        email: str = "",
        is_vocalist: bool = False,
        actor_user_id: int | None = None,
        raw: Any = None,
    ) -> dict[str, Any]:
        external = str(external_id or "").strip()
        if not external:
            raise CCBStoreError("A CCB individual ID is required.")
        conn = self.connect()
        try:
            user = conn.execute("SELECT id,username FROM users WHERE id=?", (int(user_id),)).fetchone()
            if not user:
                raise CCBStoreError("The selected TDeck user does not exist.")
            conflicting_external = conn.execute(
                "SELECT user_id FROM external_user_identities WHERE provider='ccb' AND external_id=?",
                (external,),
            ).fetchone()
            if conflicting_external and int(conflicting_external["user_id"]) != int(user_id):
                raise CCBStoreError("That CCB individual is already linked to another TDeck user.")
            conflicting_user = conn.execute(
                "SELECT external_id FROM external_user_identities WHERE provider='ccb' AND user_id=?",
                (int(user_id),),
            ).fetchone()
            if conflicting_user and str(conflicting_user["external_id"]) != external:
                raise CCBStoreError("That TDeck user is already linked to another CCB individual.")
            conn.execute(
                """
                INSERT INTO external_user_identities(
                  provider,external_id,user_id,display_name,email,is_vocalist,
                  linked_at,linked_by,last_seen_at,raw_json
                ) VALUES ('ccb',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,external_id) DO UPDATE SET
                  user_id=excluded.user_id,display_name=excluded.display_name,
                  email=excluded.email,is_vocalist=excluded.is_vocalist,
                  linked_at=excluded.linked_at,linked_by=excluded.linked_by,
                  last_seen_at=excluded.last_seen_at,raw_json=excluded.raw_json
                """,
                (
                    external, int(user_id), str(display_name or ""), str(email or ""),
                    1 if is_vocalist else 0, now_string(), actor_user_id, now_string(), _json(raw or {}),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM external_user_identities WHERE provider='ccb' AND external_id=?",
                (external,),
            ).fetchone()
            return _row_dict(row)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise CCBStoreError("This CCB or TDeck account is already linked.") from exc
        finally:
            conn.close()

    def unlink_identity(self, *, user_id: int | None = None, external_id: str | None = None) -> bool:
        if user_id is None and not external_id:
            return False
        conn = self.connect()
        try:
            if user_id is not None:
                cur = conn.execute(
                    "DELETE FROM external_user_identities WHERE provider='ccb' AND user_id=?",
                    (int(user_id),),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM external_user_identities WHERE provider='ccb' AND external_id=?",
                    (str(external_id),),
                )
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()

    def identity_for_user(self, user_id: int) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM external_user_identities WHERE provider='ccb' AND user_id=?",
                (int(user_id),),
            ).fetchone()
        finally:
            conn.close()
        return _row_dict(row) if row else None

    def set_identity_vocalist(self, user_id: int, enabled: bool) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE external_user_identities SET is_vocalist=? WHERE provider='ccb' AND user_id=?",
                (1 if enabled else 0, int(user_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def apply(self, selected_event_id: int, *, actor_user_id: int | None, source: str) -> dict[str, Any]:
        service = self.service(selected_event_id)
        warnings: list[str] = []
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT a.role_key,a.ccb_individual_id,a.assignment_status,
                       r.name AS role_name,e.user_id,u.username,u.is_active,u.is_locked,
                       cgr.group_id,g.name AS group_name
                FROM ccb_role_allocations a
                JOIN ccb_roles r ON r.role_key=a.role_key AND r.is_active=1
                LEFT JOIN external_user_identities e
                  ON e.provider='ccb' AND e.external_id=a.ccb_individual_id
                LEFT JOIN users u ON u.id=e.user_id
                LEFT JOIN ccb_group_roles cgr ON cgr.role_key=a.role_key
                LEFT JOIN groups g ON g.id=cgr.group_id
                WHERE a.selected_event_id=?
                ORDER BY r.sort_order,a.ccb_individual_id,g.name
                """,
                (int(selected_event_id),),
            ).fetchall()
            desired: set[tuple[int, int, str, str]] = set()
            warned: set[str] = set()
            for row in rows:
                status = str(row["assignment_status"] or "PENDING").upper()
                if status == "DECLINED":
                    continue
                individual_id = str(row["ccb_individual_id"])
                role_key = str(row["role_key"])
                role_name = str(row["role_name"] or role_key)
                if row["user_id"] is None:
                    key = f"unlinked:{individual_id}"
                    if key not in warned:
                        warnings.append(f"CCB individual {individual_id} is not linked to a TDeck user and was skipped.")
                        warned.add(key)
                    continue
                if not bool(row["is_active"]) or bool(row["is_locked"]):
                    key = f"inactive:{row['user_id']}"
                    if key not in warned:
                        warnings.append(f"TDeck user {row['username']} is inactive or locked and was skipped.")
                        warned.add(key)
                    continue
                if row["group_id"] is None:
                    key = f"group:{role_key}"
                    if key not in warned:
                        warnings.append(f"TDeck role {role_name} has no permission group mapping.")
                        warned.add(key)
                    continue
                desired.add((int(row["user_id"]), int(row["group_id"]), role_key, individual_id))

            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM ccb_user_group_memberships")
            for user_id, group_id, role_key, individual_id in sorted(desired):
                conn.execute(
                    """
                    INSERT INTO ccb_user_group_memberships(
                      user_id,group_id,selected_event_id,role_key,ccb_individual_id,created_at
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (user_id, group_id, int(selected_event_id), role_key, individual_id, now_string()),
                )
            conn.execute(
                """
                INSERT INTO ccb_active_service(
                  singleton_id,selected_event_id,service_name,service_start,applied_at,applied_by,source
                ) VALUES (1,?,?,?,?,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                  selected_event_id=excluded.selected_event_id,service_name=excluded.service_name,
                  service_start=excluded.service_start,applied_at=excluded.applied_at,
                  applied_by=excluded.applied_by,source=excluded.source
                """,
                (
                    int(selected_event_id), str(service.get("name") or ""), str(service.get("start") or ""),
                    now_string(), actor_user_id, str(source or "api"),
                ),
            )
            conn.commit()
            return {
                "ok": True,
                "service": service,
                "membership_count": len(desired),
                "user_count": len({item[0] for item in desired}),
                "warnings": warnings,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def clear(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            active = conn.execute("SELECT * FROM ccb_active_service WHERE singleton_id=1").fetchone()
            count_row = conn.execute("SELECT count(*) AS c FROM ccb_user_group_memberships").fetchone()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM ccb_user_group_memberships")
            conn.execute("DELETE FROM ccb_active_service")
            conn.commit()
            return {
                "ok": True,
                "cleared_memberships": int(count_row["c"] or 0) if count_row else 0,
                "previous_service": _row_dict(active) if active else None,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def runsheet_items(self, selected_event_id: int) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM ccb_runsheet_items
                WHERE selected_event_id=? ORDER BY item_index
                """,
                (int(selected_event_id),),
            ).fetchall()
        finally:
            conn.close()
        output = []
        for row in rows:
            item = _row_dict(row)
            item["links"] = _loads(item.pop("links_json", None), [])
            item["files"] = _loads(item.pop("files_json", None), [])
            item.pop("raw_json", None)
            output.append(item)
        return output


def _service_row(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": int(data["event_id"]),
        "event_id": int(data["event_id"]),
        "schedule_id": int(data["schedule_id"]),
        "category_id": int(data["category_id"]),
        "name": str(data.get("name") or ""),
        "schedule_name": str(data.get("schedule_name") or ""),
        "start": str(data.get("start_at") or ""),
        "end": str(data.get("end_at") or ""),
        "service_plan_id": _nullable_int(data.get("service_plan_id")),
        "discovered_at": str(data.get("discovered_at") or ""),
    }


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row is not None else {}


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return int(default)


def _nullable_int(value: Any) -> int | None:
    number = _int(value)
    return number if number else None


def _slug(value: Any) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
