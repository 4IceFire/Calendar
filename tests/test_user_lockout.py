from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import webui


class UserLockoutTests(unittest.TestCase):
    password = "test-password"
    csrf = "lockout-test-csrf"
    threshold = 5

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "auth.db"
        self.cfg = {
            "auth_enabled": True,
            "auth_idle_timeout_enabled": False,
            "auth_lockout_failed_attempts": self.threshold,
            "api_legacy_anonymous_enabled": False,
            "api_write_rate_limit_per_minute": 600,
        }
        for context in (
            patch.object(webui, "_AUTH_DB_PATH", self.db_path),
            patch.object(webui, "_auth_cfg", return_value=self.cfg),
            patch.object(webui.utils, "get_config", return_value=self.cfg),
            patch.object(webui, "_bootstrap_default_users_roles"),
            # Login error rendering must not fetch any integration metadata.
            patch.object(webui, "render_template", return_value="Test page"),
        ):
            context.start()
            self.addCleanup(context.stop)
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

        password_hash = webui.generate_password_hash(
            self.password, method="pbkdf2:sha256:1000"
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO users(id,username,password_hash) VALUES (1,?,?)",
                ("operator", password_hash),
            )
        webui._init_auth_db()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash) VALUES (2,?,?)",
                ("administrator", password_hash),
            )
            conn.execute(
                "INSERT INTO groups(id,name,is_admin,is_system) VALUES (1,'Admin',1,1)"
            )
            conn.execute("INSERT INTO user_groups(user_id,group_id) VALUES (2,1)")
        self.client = webui.app.test_client()
        self.admin = webui.app.test_client()

    def _row(self, sql="SELECT * FROM users WHERE id=1", parameters=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, parameters).fetchone()

    def _execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(sql, parameters)

    def _login(self, client, *, username="operator", password=None):
        with client.session_transaction() as state:
            state["_csrf"] = self.csrf
        return client.post(
            "/login",
            data={
                "username": username,
                "password": self.password if password is None else password,
                "_csrf": self.csrf,
            },
        )

    def _admin_login(self):
        self.assertEqual(
            self._login(self.admin, username="administrator").status_code, 302
        )

    def _api_update(self, **changes):
        return self.admin.post(
            "/api/admin/users/1",
            json={"group_ids": [], "is_active": True, **changes},
            headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"},
        )

    def _access_form(self, **changes):
        return self.admin.post(
            "/admin/users/1",
            data={
                "action": "update_access",
                "is_active": "on",
                "_csrf": self.csrf,
                **changes,
            },
        )

    def _assert_lockout_audit(self, source, old, new):
        event = self._row(
            "SELECT * FROM activity_log WHERE action='user.access.update' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["actor_user_id"], 2)
        self.assertEqual(event["target_id"], "1")
        self.assertEqual(event["source"], source)
        self.assertEqual(
            json.loads(event["details_json"])["lockout_enabled"],
            {"old": old, "new": new},
        )

    def test_existing_and_new_users_default_to_enabled_and_migration_is_repeatable(self):
        self.assertEqual(self._row()["lockout_enabled"], 1)
        self.assertEqual(self._row("SELECT * FROM users WHERE id=2")["lockout_enabled"], 1)
        column = self._row("SELECT * FROM pragma_table_info('users') WHERE name='lockout_enabled'")
        self.assertEqual(column["notnull"], 1)
        self.assertEqual(column["dflt_value"], "1")
        self._execute("UPDATE users SET lockout_enabled=0 WHERE id=1")
        webui._init_auth_db()
        self.assertEqual(self._row()["lockout_enabled"], 0)

    def test_enabled_threshold_locks_account_and_invalidates_existing_session(self):
        self.assertEqual(self._login(self.client).status_code, 302)
        attacker = webui.app.test_client()
        for attempt in range(1, self.threshold + 1):
            self.assertEqual(self._login(attacker, password="wrong").status_code, 401)
            row = self._row()
            self.assertEqual(row["failed_login_count"], attempt)
            self.assertEqual(row["is_locked"], int(attempt == self.threshold))
        self.assertEqual(row["session_version"], 1)
        self.assertIsNotNone(row["locked_at"])
        self.assertIsNotNone(self._row("SELECT revoked_at FROM user_sessions WHERE user_id=1")["revoked_at"])
        self.assertEqual(self._login(attacker).status_code, 403)
        self.assertEqual(self.client.get("/auth/ping").status_code, 302)
        self.assertEqual(self._row("SELECT count(*) AS n FROM audit WHERE action='user_lockout'")["n"], 1)

    def test_disabled_lockout_keeps_sessions_and_accepts_correct_password_after_failures(self):
        self._execute("UPDATE users SET lockout_enabled=0 WHERE id=1")
        self.assertEqual(self._login(self.client).status_code, 302)
        attacker = webui.app.test_client()
        for _ in range(self.threshold + 2):
            self.assertEqual(self._login(attacker, password="wrong").status_code, 401)
        row = self._row()
        self.assertEqual(row["failed_login_count"], self.threshold + 2)
        self.assertIsNotNone(row["last_failed_login_at"])
        self.assertEqual(row["is_locked"], 0)
        self.assertEqual(row["session_version"], 0)
        self.assertIsNone(self._row("SELECT revoked_at FROM user_sessions WHERE user_id=1")["revoked_at"])
        self.assertEqual(self.client.get("/auth/ping").status_code, 204)
        self.assertEqual(self._row("SELECT count(*) AS n FROM audit WHERE action='login_fail'")["n"], self.threshold + 2)
        self.assertEqual(self._row("SELECT count(*) AS n FROM audit WHERE action='user_lockout'")["n"], 0)
        self.assertEqual(self._login(attacker).status_code, 302)
        self.assertEqual(self._row()["failed_login_count"], 0)
        self.assertIsNone(self._row()["last_failed_login_at"])

    def test_failure_uses_latest_setting_instead_of_stale_login_row(self):
        stale_enabled = self._row()
        self._execute(
            "UPDATE users SET lockout_enabled=0,failed_login_count=? WHERE id=1",
            (self.threshold - 1,),
        )
        with webui.app.test_request_context("/login"):
            webui._record_login_failure(stale_enabled, "operator")
        self.assertEqual(self._row()["is_locked"], 0)

        stale_disabled = self._row()
        self._execute("UPDATE users SET lockout_enabled=1 WHERE id=1")
        with webui.app.test_request_context("/login"):
            webui._record_login_failure(stale_disabled, "operator")
        self.assertEqual(self._row()["is_locked"], 1)

    def test_api_toggle_persists_resets_failures_and_audits_without_revoking_sessions(self):
        self.assertEqual(self._login(self.client).status_code, 302)
        self._admin_login()
        self._execute(
            "UPDATE users SET failed_login_count=4,last_failed_login_at='previous' WHERE id=1"
        )
        response = self._api_update(lockout_enabled=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["lockout_enabled"], False)
        self.assertEqual(response.get_json()["failed_login_count"], 0)
        row = self._row()
        self.assertEqual(row["lockout_enabled"], 0)
        self.assertEqual(row["failed_login_count"], 0)
        self.assertIsNone(row["last_failed_login_at"])
        self.assertEqual(row["updated_by"], 2)
        self.assertEqual(row["session_version"], 0)
        self.assertEqual(self.client.get("/auth/ping").status_code, 204)
        self._assert_lockout_audit("api", True, False)
        self._execute("UPDATE users SET failed_login_count=3 WHERE id=1")
        self.assertEqual(self._api_update().status_code, 200)
        self.assertEqual(self._row()["lockout_enabled"], 0)
        self.assertEqual(self._row()["failed_login_count"], 3)
        self.assertEqual(self._api_update(lockout_enabled=False).status_code, 200)
        self.assertEqual(self._row()["failed_login_count"], 3)
        self.assertEqual(self._api_update(lockout_enabled=True).status_code, 200)
        self.assertEqual(self._row()["lockout_enabled"], 1)
        self.assertEqual(self._row()["failed_login_count"], 0)
        self._assert_lockout_audit("api", False, True)

    def test_api_rejects_non_boolean_lockout_values_without_changing_access(self):
        self._admin_login()
        for value in (None, "false", "true", 0, 1, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self._api_update(lockout_enabled=value).status_code, 400)
                self.assertEqual(self._row()["lockout_enabled"], 1)

    def test_access_form_toggle_and_legacy_form_preserve_expected_setting(self):
        self._admin_login()
        self._execute(
            "UPDATE users SET failed_login_count=4,last_failed_login_at='previous' WHERE id=1"
        )
        self.assertEqual(self._access_form(lockout_settings_present="1").status_code, 302)
        self.assertEqual(self._row()["lockout_enabled"], 0)
        self.assertEqual(self._row()["failed_login_count"], 0)
        self.assertIsNone(self._row()["last_failed_login_at"])
        self._assert_lockout_audit("web", True, False)
        self._execute("UPDATE users SET failed_login_count=3 WHERE id=1")
        self.assertEqual(self._access_form().status_code, 302)
        self.assertEqual(self._row()["lockout_enabled"], 0)
        self.assertEqual(self._row()["failed_login_count"], 3)
        self.assertEqual(
            self._access_form(lockout_settings_present="1", lockout_enabled="on").status_code,
            302,
        )
        self.assertEqual(self._row()["lockout_enabled"], 1)
        self.assertEqual(self._row()["failed_login_count"], 0)
        self._assert_lockout_audit("web", False, True)

    def test_disabling_automatic_lockout_does_not_unlock_existing_locks(self):
        self._admin_login()
        for reason in ("Locked by admin", "Too many failed login attempts (5)"):
            with self.subTest(reason=reason):
                self._execute(
                    "UPDATE users SET lockout_enabled=1,is_locked=1,locked_reason=?,locked_at='previous' WHERE id=1",
                    (reason,),
                )
                self.assertEqual(self._api_update(lockout_enabled=False).status_code, 200)
                row = self._row()
                self.assertEqual(row["is_locked"], 1)
                self.assertEqual(row["locked_reason"], reason)
                self.assertEqual(row["locked_at"], "previous")
                self.assertEqual(self._login(self.client).status_code, 403)

    def test_manual_lock_still_revokes_sessions_when_automatic_lockout_is_disabled(self):
        self._execute("UPDATE users SET lockout_enabled=0 WHERE id=1")
        self.assertEqual(self._login(self.client).status_code, 302)
        self._admin_login()
        response = self.admin.post(
            "/admin/users/1", data={"action": "lock_user", "_csrf": self.csrf}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._row()["is_locked"], 1)
        self.assertEqual(self._row()["session_version"], 1)
        self.assertEqual(self.client.get("/auth/ping").status_code, 302)
        response = self.admin.post(
            "/admin/users/1", data={"action": "unlock_user", "_csrf": self.csrf}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._row()["lockout_enabled"], 0)
        self.assertEqual(self._login(self.client).status_code, 302)

    def test_update_requires_user_management_permission_and_csrf(self):
        api_path = "/api/admin/users/1"
        form_path = "/admin/users/1"
        data = {"lockout_enabled": False, "group_ids": [], "is_active": True}
        self.assertEqual(self.client.post(api_path, json=data).status_code, 401)
        self.assertEqual(self.client.post(form_path, data={}).status_code, 302)
        self.assertEqual(self._login(self.client).status_code, 302)
        self.assertEqual(
            self.client.post(api_path, json=data, headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(form_path, data={"action": "update_access", "_csrf": self.csrf, "lockout_settings_present": "1"}).status_code,
            403,
        )
        self._admin_login()
        self.assertEqual(self.admin.post(api_path, json=data).status_code, 403)
        self.assertEqual(
            self.admin.post(api_path, json=data, headers={"X-CSRF-Token": self.csrf, "Origin": "https://attacker.invalid"}).status_code,
            403,
        )
        self.assertEqual(
            self.admin.post(form_path, data={"action": "update_access", "lockout_settings_present": "1"}).status_code,
            400,
        )
        self.assertEqual(self._row()["lockout_enabled"], 1)


if __name__ == "__main__":
    unittest.main()
