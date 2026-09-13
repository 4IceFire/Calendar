from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from werkzeug.security import generate_password_hash

import webui


class LoginRedirectTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        patches = (
            patch.object(webui, "_AUTH_DB_PATH", Path(temporary_directory.name) / "auth.db"),
            patch.object(
                webui,
                "_auth_cfg",
                return_value={
                    "auth_enabled": True,
                    "auth_idle_timeout_enabled": True,
                    "auth_idle_timeout_minutes": 2,
                },
            ),
            patch.object(webui, "log_event"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        webui._bootstrap_default_users_roles()
        conn = webui._db()
        try:
            group_id = int(
                conn.execute(
                    "INSERT INTO groups(name,is_admin) VALUES (?,0)",
                    ("Login regression mixers",),
                ).lastrowid
            )
            self.user_id = int(
                conn.execute(
                    "INSERT INTO users(username,password_hash,is_active) VALUES (?,?,1)",
                    ("login-mixer", generate_password_hash("login-test-password")),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO user_groups(user_id,group_id) VALUES (?,?)",
                (self.user_id, group_id),
            )
            conn.execute(
                "INSERT INTO group_pages(group_id,page_key) VALUES (?,?)",
                (group_id, "page:digico_mixer"),
            )
            conn.commit()
        finally:
            conn.close()
        self.client = webui.app.test_client()

    def _login(self, next_url="", *, client=None):
        client = client or self.client
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        return self._submit_login(client, next_url)

    def _submit_login(self, client, next_url=""):
        with client.session_transaction() as state:
            csrf_token = state["_csrf"]
        return client.post(
            "/login",
            data={
                "_csrf": csrf_token,
                "username": "login-mixer",
                "password": "login-test-password",
                "next": next_url,
            },
        )

    def _assert_redirect(self, response, expected="/personal-mixes"):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], expected)

    def _set_session_stale(self):
        with self.client.session_transaction() as state:
            state["_last_activity"] = int(time.time()) - 180

    def _change_user(self, assignment):
        conn = webui._db()
        try:
            conn.execute(f"UPDATE users SET {assignment} WHERE id=?", (self.user_id,))
            conn.commit()
        finally:
            conn.close()

    def test_successful_login_rejects_non_page_and_unsafe_next_targets(self):
        targets = (
            "/auth/ping",
            "/auth/touch?activity=1",
            "/login?timeout=1",
            "/logout",
            "/api/status/summary",
            "/static/app.js",
            "/missing-login-destination",
            "https://example.com/personal-mixes",
            "//example.com/personal-mixes",
            r"/\example.com/personal-mixes",
            r"\example.com\personal-mixes",
            "/%5cexample.com/personal-mixes",
            "http://[invalid",
            "javascript:alert(1)",
            "personal-mixes",
            "/personal-mixes\r\nLocation: https://example.com/",
        )
        for target in targets:
            with self.subTest(next=target):
                response = self._login(target, client=webui.app.test_client())
                self._assert_redirect(response)

    def test_successful_login_preserves_authorized_ui_query_and_fragment(self):
        target = "/personal-mixes?aux=2&label=Front%20row#channels"
        self._assert_redirect(self._login(target), target)

    def test_successful_login_falls_back_when_requested_ui_is_denied(self):
        for target in ("/", "/config?tab=security"):
            with self.subTest(next=target):
                self._assert_redirect(self._login(target, client=webui.app.test_client()))

    def test_successful_login_uses_password_page_when_no_ui_pages_are_granted(self):
        conn = webui._db()
        try:
            conn.execute("DELETE FROM user_groups WHERE user_id=?", (self.user_id,))
            conn.commit()
        finally:
            conn.close()
        self._assert_redirect(self._login("/auth/ping"), "/account/password")

    def test_authenticated_login_page_redirects_to_landing_even_with_timeout_flag(self):
        self._assert_redirect(self._login())
        for query in ({}, {"timeout": "1"}, {"next": "/auth/ping", "timeout": "1"}):
            with self.subTest(query=query):
                self._assert_redirect(self.client.get("/login", query_string=query))

    def test_authenticated_login_page_preserves_authorized_next(self):
        self._assert_redirect(self._login())
        target = "/personal-mixes?aux=3#channels"
        self._assert_redirect(self.client.get("/login", query_string={"next": target}), target)

    def test_authenticated_login_page_rejects_denied_and_external_next(self):
        self._assert_redirect(self._login())
        for target in ("/config", "//example.com/personal-mixes", "/logout"):
            with self.subTest(next=target):
                self._assert_redirect(self.client.get("/login", query_string={"next": target}))

    def test_authenticated_login_page_obeys_new_password_change_requirement(self):
        self._assert_redirect(self._login())
        self._change_user("force_password_change=1")
        self._assert_redirect(
            self.client.get("/login?timeout=1&next=/personal-mixes"),
            "/account/password?force=1",
        )

    def test_successful_login_obeys_password_change_requirement(self):
        self._change_user("force_password_change=1")
        self._assert_redirect(self._login("/personal-mixes"), "/account/password?force=1")

    def test_expired_session_on_login_page_renders_form_without_renewing_session(self):
        self._assert_redirect(self._login())
        self._set_session_stale()
        response = self.client.get("/login?timeout=1", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.history), 1)
        self.assertIn('id="login-password"', response.get_data(as_text=True))
        with self.client.session_transaction() as state:
            self.assertNotIn("_user_id", state)
            self.assertNotIn("_auth_session_id", state)
            self.assertNotIn("_last_activity", state)
        self.assertEqual(self.client.get("/login?timeout=1").status_code, 200)
        self._assert_redirect(self._submit_login(self.client))

    def test_revoked_session_on_login_page_renders_form_and_allows_new_login(self):
        self._assert_redirect(self._login())
        with self.client.session_transaction() as state:
            session_id = state["_auth_session_id"]
        conn = webui._db()
        try:
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE id=?",
                (webui._now_str(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
        response = self.client.get("/login?timeout=1", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.history), 1)
        self.assertIn('id="login-password"', response.get_data(as_text=True))
        with self.client.session_transaction() as state:
            self.assertNotIn("_user_id", state)
            self.assertNotIn("_auth_session_id", state)
            self.assertNotIn("_last_activity", state)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self._assert_redirect(self._submit_login(self.client))

    def test_changed_session_version_on_login_page_does_not_restore_authentication(self):
        self._assert_redirect(self._login())
        self._change_user("session_version=COALESCE(session_version,0)+1")
        response = self.client.get("/login", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.history), 1)
        with self.client.session_transaction() as state:
            self.assertNotIn("_user_id", state)
            self.assertNotIn("_last_activity", state)

    def test_direct_login_post_replaces_expired_or_revoked_session(self):
        for stale_reason in ("expired", "revoked"):
            with self.subTest(stale_reason=stale_reason):
                client = webui.app.test_client()
                self._assert_redirect(self._login(client=client))
                with client.session_transaction() as state:
                    old_session_id = state["_auth_session_id"]
                    if stale_reason == "expired":
                        state["_last_activity"] = int(time.time()) - 180
                if stale_reason == "revoked":
                    conn = webui._db()
                    try:
                        conn.execute(
                            "UPDATE user_sessions SET revoked_at=? WHERE id=?",
                            (webui._now_str(), old_session_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                self._assert_redirect(self._submit_login(client, "/auth/ping"))
                with client.session_transaction() as state:
                    self.assertNotEqual(state["_auth_session_id"], old_session_id)
                    self.assertEqual(str(state["_user_id"]), str(self.user_id))
                self.assertEqual(client.get("/auth/ping").status_code, 204)

    def test_anonymous_heartbeat_does_not_become_login_destination(self):
        for path in ("/auth/ping", "/auth/touch"):
            with self.subTest(path=path):
                response = webui.app.test_client().get(path)
                self.assertEqual(response.status_code, 302)
                location = urlsplit(response.headers["Location"])
                self.assertEqual(location.path, "/login")
                self.assertNotIn("next", parse_qs(location.query))

    def test_idle_api_then_heartbeat_can_log_in_again_to_authorized_page(self):
        self._assert_redirect(self._login())
        self._set_session_stale()
        expired_api = self.client.get("/api/status/summary")
        self.assertEqual(expired_api.status_code, 401)
        heartbeat = self.client.get("/auth/ping")
        self.assertEqual(heartbeat.status_code, 302)
        login_page = self.client.get(heartbeat.headers["Location"])
        self.assertEqual(login_page.status_code, 200)
        next_url = parse_qs(urlsplit(heartbeat.headers["Location"]).query).get("next", [""])[0]
        self._assert_redirect(self._submit_login(self.client, next_url))
        self.assertEqual(self.client.get("/auth/ping").status_code, 204)


if __name__ == "__main__":
    unittest.main()
