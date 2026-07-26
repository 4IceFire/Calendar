from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from ccb_store import CCBTDeckStore
import webui


class CCBAccessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(webui, "_AUTH_DB_PATH", Path(self.temp.name) / "auth.db")
        self.db_patch.start()
        webui._init_auth_db()
        self.store = CCBTDeckStore(webui._AUTH_DB_PATH)

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def _seed_user_and_groups(self):
        conn = webui._db()
        try:
            user_id = int(
                conn.execute(
                    "INSERT INTO users(username,email,password_hash,is_active,is_locked) VALUES (?,?,?,?,0)",
                    ("musician", "musician@example.test", generate_password_hash("password"), 1),
                ).lastrowid
            )
            fallback_id = int(
                conn.execute(
                    "INSERT INTO groups(name,digico_allowed_auxes,suspend_while_ccb_active) VALUES (?,?,1)",
                    ("Band Practice", "[]"),
                ).lastrowid
            )
            permanent_id = int(
                conn.execute(
                    "INSERT INTO groups(name,digico_allowed_auxes,suspend_while_ccb_active) VALUES (?,?,0)",
                    ("Manual Permanent", '["99"]'),
                ).lastrowid
            )
            drums_id = int(
                conn.execute(
                    "INSERT INTO groups(name,digico_allowed_auxes,suspend_while_ccb_active) VALUES (?,?,0)",
                    ("Drums AUX", '["4"]'),
                ).lastrowid
            )
            for group_id in (fallback_id, permanent_id, drums_id):
                conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES (?,?)", (group_id, "page:digico_mixer"))
            conn.execute("INSERT INTO user_groups(user_id,group_id) VALUES (?,?)", (user_id, fallback_id))
            conn.execute("INSERT INTO user_groups(user_id,group_id) VALUES (?,?)", (user_id, permanent_id))
            conn.execute("INSERT INTO ccb_group_roles(group_id,role_key) VALUES (?,?)", (drums_id, "drums"))
            conn.commit()
            return user_id, fallback_id, permanent_id, drums_id
        finally:
            conn.close()

    def _cache_service_and_roster(self, user_id: int):
        self.store.cache_services(
            [
                {
                    "event_id": 801,
                    "schedule_id": 50,
                    "category_id": 7,
                    "name": "8am",
                    "start": "2026-08-02T08:00:00+10:00",
                    "end": "2026-08-02T09:30:00+10:00",
                    "service_plan_id": 9,
                }
            ]
        )
        self.store.link_identity(
            external_id="11",
            user_id=user_id,
            display_name="Alex Drummer",
            email="musician@example.test",
        )
        roster = {
            "event_id": 801,
            "people": [{"id": 11, "name": "Alex Drummer", "email": "musician@example.test", "raw": {}}],
            "eligible_people": [],
            "positions": [
                {
                    "position_id": 3,
                    "event_position_id": 99,
                    "position_name": "Drums",
                    "team_name": "Worship",
                    "assignments": [
                        {"individual_id": 11, "name": "Alex Drummer", "email": "musician@example.test", "status": "PENDING", "grants_access": True}
                    ],
                    "raw": {},
                }
            ],
            "warnings": [],
            "raw": {},
        }
        run_id = self.store.start_workflow_run(801, ["roster"], "test")
        self.store.store_workflow_result(
            run_id,
            801,
            {"ok": True, "branches": {"roster": {"ok": True, "data": roster, "warnings": [], "error": None}}},
            {"roster": self.store.service(801)},
            actor_user_id=None,
        )

    def test_apply_suppresses_only_fallback_and_clear_restores_it(self):
        user_id, _fallback_id, _permanent_id, _drums_id = self._seed_user_and_groups()
        self._cache_service_and_roster(user_id)

        before = {str(group["name"]) for group in webui._get_user_groups(user_id)}
        self.assertEqual(before, {"Band Practice", "Manual Permanent"})

        applied = self.store.apply(801, actor_user_id=None, source="test")
        active = {str(group["name"]) for group in webui._get_user_groups(user_id)}

        self.assertEqual(applied["membership_count"], 1)
        self.assertEqual(active, {"Manual Permanent", "Drums AUX"})
        self.assertEqual(webui._effective_digico_aux_ids_for_user(user_id), ["4", "99"])

        cleared = self.store.clear()
        restored = {str(group["name"]) for group in webui._get_user_groups(user_id)}
        self.assertEqual(cleared["cleared_memberships"], 1)
        self.assertEqual(restored, {"Band Practice", "Manual Permanent"})

    def test_declined_assignment_does_not_create_auto_allocation(self):
        user_id, *_ = self._seed_user_and_groups()
        self.store.cache_services([{"event_id": 801, "schedule_id": 50, "category_id": 7, "name": "8am"}])
        self.store.link_identity(external_id="11", user_id=user_id, display_name="Alex")
        roster = {
            "event_id": 801,
            "people": [{"id": 11, "name": "Alex", "email": "", "raw": {}}],
            "eligible_people": [],
            "positions": [{"position_id": 3, "position_name": "Drums", "assignments": [{"individual_id": 11, "status": "DECLINED"}], "raw": {}}],
        }
        run_id = self.store.start_workflow_run(801, ["roster"], "test")
        self.store.store_workflow_result(
            run_id, 801,
            {"ok": True, "branches": {"roster": {"ok": True, "data": roster, "warnings": []}}},
            {"roster": self.store.service(801)}, actor_user_id=None,
        )

        state = self.store.service_state(801)
        drums = next(role for role in state["roles"] if role["role_key"] == "drums")
        self.assertEqual(drums["allocations"], [])

    def test_api_requires_the_dedicated_companion_token(self):
        webui._ccb_secret_store().save({"companion_api_token": "test-token"}, preserve_blank=False)
        client = webui.app.test_client()
        with patch.object(webui, "_auth_enabled", return_value=True):
            missing = client.get("/api/ccb/status")
            wrong = client.get("/api/ccb/status", headers={"Authorization": "Bearer wrong"})
            allowed = client.get("/api/ccb/status", headers={"Authorization": "Bearer test-token"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_ccb_pages_and_group_mapping_controls_render(self):
        client = webui.app.test_client()
        with patch.object(webui, "_auth_enabled", return_value=False):
            service_page = client.get("/service-access")
            config_page = client.get("/config/ccb")
            groups_page = client.get("/admin/permissions?tab=groups")

        self.assertEqual(service_page.status_code, 200)
        self.assertIn(b"Pull All From CCB", service_page.data)
        self.assertIn(b"Run Service Setup", service_page.data)
        self.assertEqual(config_page.status_code, 200)
        self.assertIn(b"CCB position mapping", config_page.data)
        self.assertIn(b"Playbacks", groups_page.data)
        self.assertIn(b"Suspend this group while CCB service access is active", groups_page.data)

    def test_login_accepts_unique_email_address(self):
        conn = webui._db()
        try:
            group_id = int(conn.execute("INSERT INTO groups(name) VALUES ('Login Group')").lastrowid)
            user_id = int(conn.execute(
                "INSERT INTO users(username,email,password_hash,is_active,is_locked) VALUES (?,?,?,?,0)",
                ("email-user", "login@example.test", generate_password_hash("email-password"), 1),
            ).lastrowid)
            conn.execute("INSERT INTO user_groups(user_id,group_id) VALUES (?,?)", (user_id, group_id))
            conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES (?,?)", (group_id, "page:home"))
            conn.commit()
        finally:
            conn.close()
        client = webui.app.test_client()
        with patch.object(webui, "_auth_enabled", return_value=True):
            client.get("/login")
            with client.session_transaction() as session_state:
                csrf = session_state["_csrf"]
            response = client.post("/login", data={"_csrf": csrf, "username": "login@example.test", "password": "email-password"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
