from __future__ import annotations

import io
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import api_security
import cli
import webui


class _AuthenticatedUser:
    is_authenticated = True
    username = "operator"

    @staticmethod
    def get_id():
        return "42"


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "auth.db"
        with patch.object(webui, "_AUTH_DB_PATH", self.db_path):
            webui._init_auth_db()
        self.client = webui.app.test_client()
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    def _cfg(self, **overrides):
        cfg = {
            "auth_enabled": True,
            "api_legacy_anonymous_enabled": False,
            "api_max_request_bytes": 2 * 1024 * 1024,
            "api_write_rate_limit_per_minute": 600,
        }
        cfg.update(overrides)
        return cfg

    def _security_patches(self, **cfg_overrides):
        return (
            patch.object(webui, "_AUTH_DB_PATH", self.db_path),
            patch.object(webui, "_auth_cfg", return_value=self._cfg(**cfg_overrides)),
            patch.object(webui, "log_event"),
        )

    def _browser_patches(self, *, can_access=True, **cfg_overrides):
        return self._security_patches(**cfg_overrides) + (
            patch.object(webui, "current_user", _AuthenticatedUser()),
            patch.object(webui, "_touch_current_user_session", return_value=True),
            patch.object(webui, "can_access", return_value=can_access),
        )

    def _set_session_csrf(self, token="csrf-test"):
        with self.client.session_transaction() as state:
            state["_csrf"] = token
            state["_last_activity"] = int(time.time())
        return token

    def test_secure_mode_requires_authentication_and_explicit_page_access(self):
        with self._security_patches()[0], self._security_patches()[1], self._security_patches()[2]:
            response = self.client.get("/api/status/summary")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

        with (
            self._browser_patches(can_access=False)[0],
            self._browser_patches(can_access=False)[1],
            self._browser_patches(can_access=False)[2],
            self._browser_patches(can_access=False)[3],
            self._browser_patches(can_access=False)[4],
            self._browser_patches(can_access=False)[5],
        ):
            response = self.client.get("/api/timers")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "forbidden")

    def test_browser_writes_require_csrf_and_same_origin(self):
        token = self._set_session_csrf()
        original = webui.app.view_functions["api_apply_timer_preset"]
        webui.app.view_functions["api_apply_timer_preset"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_apply_timer_preset", original)
        contexts = self._browser_patches(can_access=True)
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], contexts[5]:
            missing = self.client.post("/api/timers/apply", json={"preset": 1})
            self.assertEqual(missing.status_code, 403)
            self.assertEqual(missing.get_json()["error"], "invalid_csrf")

            cross_site = self.client.post(
                "/api/timers/apply",
                json={"preset": 1},
                headers={"X-CSRF-Token": token, "Origin": "https://attacker.invalid"},
            )
            self.assertEqual(cross_site.status_code, 403)
            self.assertEqual(cross_site.get_json()["error"], "invalid_origin")

            allowed = self.client.post(
                "/api/timers/apply",
                json={"preset": 1},
                headers={"X-CSRF-Token": token, "Origin": "http://localhost"},
            )
            self.assertEqual(allowed.status_code, 200)

    def test_scoped_token_is_hashed_used_and_rejected_after_revocation(self):
        record = api_security.create_service_token(
            self.db_path,
            name="Companion Test",
            scopes=["read", "timers"],
            expires_in_days=7,
        )
        conn = sqlite3.connect(self.db_path)
        try:
            stored_hash = conn.execute(
                "SELECT token_hash FROM service_tokens WHERE id=?", (record["id"],)
            ).fetchone()[0]
            raw_db = self.db_path.read_bytes()
        finally:
            conn.close()
        self.assertEqual(stored_hash, api_security.token_digest(record["token"]))
        self.assertNotIn(record["token"].encode(), raw_db)

        original = webui.app.view_functions["api_apply_timer_preset"]
        webui.app.view_functions["api_apply_timer_preset"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_apply_timer_preset", original)
        headers = {"Authorization": f"Bearer {record['token']}"}
        contexts = self._security_patches()
        with contexts[0], contexts[1], contexts[2]:
            allowed = self.client.post("/api/timers/apply", json={"preset": 1}, headers=headers)
            self.assertEqual(allowed.status_code, 200)
            denied = self.client.post("/api/videohub/route", json={"output": 1, "input": 1}, headers=headers)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.get_json()["error"], "insufficient_scope")

            listed = api_security.list_service_tokens(self.db_path)
            self.assertTrue(listed[0]["last_used_at"])
            api_security.revoke_service_token(self.db_path, record["id"])
            revoked = self.client.post("/api/timers/apply", json={"preset": 1}, headers=headers)
            self.assertEqual(revoked.status_code, 401)
            self.assertEqual(revoked.get_json()["error"], "revoked_token")

    def test_expired_token_and_write_rate_limit(self):
        expired = api_security.create_service_token(self.db_path, name="Expired", scopes=["timers"], expires_in_days=1)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE service_tokens SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), expired["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        active = api_security.create_service_token(self.db_path, name="Rate", scopes=["timers"], expires_in_days=1)
        original = webui.app.view_functions["api_apply_timer_preset"]
        webui.app.view_functions["api_apply_timer_preset"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_apply_timer_preset", original)
        contexts = self._security_patches(api_write_rate_limit_per_minute=10)
        with contexts[0], contexts[1], contexts[2]:
            expired_response = self.client.post(
                "/api/timers/apply", json={}, headers={"Authorization": f"Bearer {expired['token']}"}
            )
            self.assertEqual(expired_response.status_code, 401)
            self.assertEqual(expired_response.get_json()["error"], "expired_token")
            headers = {"Authorization": f"Bearer {active['token']}"}
            for _ in range(10):
                self.assertEqual(self.client.post("/api/timers/apply", json={}, headers=headers).status_code, 200)
            limited = self.client.post("/api/timers/apply", json={}, headers=headers)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.get_json()["error"], "rate_limited")
            self.assertIn("Retry-After", limited.headers)

    def test_legacy_flag_is_expiring_audited_and_not_a_permanent_bypass(self):
        original = webui.app.view_functions["api_status_summary"]
        webui.app.view_functions["api_status_summary"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_status_summary", original)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        contexts = self._security_patches(
            api_legacy_anonymous_enabled=True,
            api_legacy_anonymous_until=future,
        )
        with contexts[0], contexts[1], contexts[2] as activity:
            response = self.client.get("/api/status/summary")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(any(call.args and call.args[0] == "security.api.legacy_anonymous" for call in activity.call_args_list))
            forbidden = self.client.get("/api/config")
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(forbidden.get_json()["error"], "forbidden")

        unsafe_future = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        contexts = self._security_patches(
            api_legacy_anonymous_enabled=True,
            api_legacy_anonymous_until=unsafe_future,
        )
        with contexts[0], contexts[1], contexts[2]:
            response = self.client.get("/api/status/summary")
        self.assertEqual(response.status_code, 401)

    def test_v1_alias_preserves_contract_and_version_header(self):
        record = api_security.create_service_token(self.db_path, name="Reader", scopes=["read"])
        original = webui.app.view_functions["api_v1__api_status_summary"]
        webui.app.view_functions["api_v1__api_status_summary"] = lambda: webui.jsonify({"ok": True, "contract": "same"})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_v1__api_status_summary", original)
        contexts = self._security_patches()
        with contexts[0], contexts[1], contexts[2]:
            response = self.client.get(
                "/api/v1/status/summary",
                headers={"Authorization": f"Bearer {record['token']}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["contract"], "same")
        self.assertEqual(response.headers["X-TDeck-API-Version"], "1")

    def test_token_target_and_action_constraints_are_enforced(self):
        record = api_security.create_service_token(
            self.db_path,
            name="Restricted Router",
            scopes=["videohub"],
            constraints={
                "allowed_paths": ["POST /api/videohub/route"],
                "videohub_outputs": [2],
                "videohub_inputs": [3],
            },
        )
        original = webui.app.view_functions["api_videohub_route"]
        webui.app.view_functions["api_videohub_route"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_videohub_route", original)
        headers = {"Authorization": f"Bearer {record['token']}"}
        contexts = self._security_patches()
        with contexts[0], contexts[1], contexts[2]:
            allowed = self.client.post(
                "/api/videohub/route", json={"output": 2, "input": 3}, headers=headers
            )
            self.assertEqual(allowed.status_code, 200)
            denied_target = self.client.post(
                "/api/videohub/route", json={"output": 1, "input": 3}, headers=headers
            )
            self.assertEqual(denied_target.status_code, 403)
            self.assertEqual(denied_target.get_json()["error"], "token_constraint_denied")
            denied_action = self.client.get("/api/videohub/state", headers=headers)
            self.assertEqual(denied_action.status_code, 403)

    def test_videohub_monitor_requires_the_same_authenticated_permission_as_its_data(self):
        contexts = self._security_patches()
        with contexts[0], contexts[1], contexts[2]:
            anonymous = self.client.get("/videohub/monitor")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login", anonymous.headers["Location"])

        contexts = self._browser_patches(can_access=True)
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], contexts[5]:
            page = self.client.get("/videohub/monitor")
        self.assertEqual(page.status_code, 200)

    def test_auth_disabled_development_behavior_and_request_size_limit(self):
        original = webui.app.view_functions["api_apply_timer_preset"]
        webui.app.view_functions["api_apply_timer_preset"] = lambda: webui.jsonify({"ok": True})
        self.addCleanup(webui.app.view_functions.__setitem__, "api_apply_timer_preset", original)
        with patch.object(webui, "_auth_enabled", return_value=False):
            self.assertEqual(self.client.post("/api/timers/apply", json={}).status_code, 200)

        record = api_security.create_service_token(self.db_path, name="Small", scopes=["timers"])
        contexts = self._security_patches(api_max_request_bytes=1024)
        with contexts[0], contexts[1], contexts[2]:
            response = self.client.post(
                "/api/timers/apply",
                data=b"x" * 2048,
                content_type="application/json",
                headers={"Authorization": f"Bearer {record['token']}"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "request_too_large")

    def test_cli_create_list_rotate_and_revoke_never_relists_plaintext(self):
        with patch.object(cli, "_service_token_db_path", return_value=self.db_path):
            created_output = io.StringIO()
            with redirect_stdout(created_output):
                self.assertEqual(cli.main(["service-tokens", "create", "Companion", "--scope", "read", "--scope", "timers"]), 0)
            created_text = created_output.getvalue()
            plaintext = next(line for line in created_text.splitlines() if line.startswith("tdk_"))

            listed_output = io.StringIO()
            with redirect_stdout(listed_output):
                self.assertEqual(cli.main(["service-tokens", "list"]), 0)
            self.assertNotIn(plaintext, listed_output.getvalue())

            rotated_output = io.StringIO()
            with redirect_stdout(rotated_output):
                self.assertEqual(cli.main(["service-tokens", "rotate", "1"]), 0)
            self.assertIn("Revoked previous token", rotated_output.getvalue())
            records = api_security.list_service_tokens(self.db_path)
            self.assertFalse(records[0]["active"])
            self.assertTrue(records[1]["active"])

            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["service-tokens", "revoke", str(records[1]["id"])]), 0)
            self.assertFalse(api_security.list_service_tokens(self.db_path)[1]["active"])

    def test_base_installs_shared_csrf_fetch_layer_before_page_scripts(self):
        with patch.object(webui, "_auth_enabled", return_value=False):
            html = self.client.get("/").get_data(as_text=True)
            script_response = self.client.get("/static/api_client.js")
            script = script_response.get_data(as_text=True)
            script_response.close()
        self.assertLess(html.index("api_client.js"), html.index("client_telemetry.js"))
        self.assertLess(html.index("api_client.js"), html.index("app.js"))
        self.assertIn("parsed.pathname.indexOf('/api/')", script)
        self.assertIn("X-CSRF-Token", script)
        self.assertIn("parsed.origin === window.location.origin", script)


if __name__ == "__main__":
    unittest.main()
