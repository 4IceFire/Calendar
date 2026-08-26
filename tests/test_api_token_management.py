from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import api_security
import webui


class _AdminUser:
    is_authenticated = True
    username = "administrator"

    @staticmethod
    def get_id():
        return "42"


class ApiTokenManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "auth.db"
        with patch.object(webui, "_AUTH_DB_PATH", self.db_path):
            webui._init_auth_db()
        self.client = webui.app.test_client()
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    @staticmethod
    def _config():
        return {
            "auth_enabled": True,
            "api_legacy_anonymous_enabled": False,
            "api_max_request_bytes": 2 * 1024 * 1024,
            "api_write_rate_limit_per_minute": 600,
        }

    def _secure_context(self, *, admin: bool):
        stack = ExitStack()
        stack.enter_context(patch.object(webui, "_AUTH_DB_PATH", self.db_path))
        stack.enter_context(patch.object(webui, "_auth_cfg", return_value=self._config()))
        stack.enter_context(patch.object(webui, "current_user", _AdminUser()))
        stack.enter_context(patch.object(webui, "_touch_current_user_session", return_value=True))
        stack.enter_context(patch.object(webui, "can_access", return_value=True))
        stack.enter_context(patch.object(webui, "_user_is_admin", return_value=admin))
        self.event_log = stack.enter_context(patch.object(webui, "log_event"))
        return stack

    def _csrf_headers(self):
        token = "token-management-csrf"
        with self.client.session_transaction() as state:
            state["_csrf"] = token
            state["_last_activity"] = int(time.time())
        return {
            "X-CSRF-Token": token,
            "Origin": "http://localhost",
        }

    def test_config_page_is_available_without_auth_and_has_management_controls(self):
        with patch.object(webui, "_auth_enabled", return_value=False):
            response = self.client.get("/config/api-tokens")
        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn("Create token", markup)
        self.assertIn("shown once", markup)
        self.assertIn("api_tokens.js", markup)
        self.assertIn('active" href="/config/api-tokens"', markup)

    def test_non_admin_cannot_open_page_or_management_api(self):
        with self._secure_context(admin=False):
            page = self.client.get("/config/api-tokens")
            api = self.client.get("/api/config/service-tokens")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api.get_json()["error"], "forbidden")

    def test_admin_can_create_list_rotate_and_revoke_without_leaking_plaintext(self):
        headers = self._csrf_headers()
        body = {
            "name": "Companion Auditorium",
            "description": "Auditorium Stream Deck",
            "expires_in_days": 365,
            "scopes": ["read", "timers", "videohub"],
            "constraints": {
                "allowed_paths": ["POST /api/timers/apply"],
                "videohub_outputs": [2],
            },
        }
        with self._secure_context(admin=True):
            created = self.client.post(
                "/api/config/service-tokens",
                json=body,
                headers=headers,
            )
            self.assertEqual(created.status_code, 201)
            created_token = created.get_json()["token"]
            plaintext = created_token["token"]
            self.assertTrue(plaintext.startswith("tdk_"))

            listed = self.client.get("/api/config/service-tokens")
            self.assertEqual(listed.status_code, 200)
            metadata = listed.get_json()["tokens"][0]
            self.assertNotIn("token", metadata)
            self.assertNotIn("token_hash", metadata)
            self.assertEqual(metadata["constraints"]["videohub_outputs"], [2])

            rotated = self.client.post(
                f"/api/config/service-tokens/{created_token['id']}/rotate",
                json={"name": "Companion Auditorium", "expires_in_days": 365},
                headers=headers,
            )
            self.assertEqual(rotated.status_code, 200)
            replacement = rotated.get_json()["token"]
            self.assertNotEqual(replacement["token"], plaintext)
            self.assertEqual(replacement["scopes"], created_token["scopes"])
            self.assertEqual(replacement["constraints"], created_token["constraints"])

            old_record, old_error = api_security.authenticate_service_token(
                self.db_path, plaintext, update_last_used=False
            )
            new_record, new_error = api_security.authenticate_service_token(
                self.db_path, replacement["token"], update_last_used=False
            )
            self.assertIsNone(old_record)
            self.assertEqual(old_error, "revoked_token")
            self.assertIsNotNone(new_record)
            self.assertIsNone(new_error)

            revoked = self.client.delete(
                f"/api/config/service-tokens/{replacement['id']}", headers=headers
            )
            self.assertEqual(revoked.status_code, 200)
            self.assertFalse(revoked.get_json()["token"]["active"])

            calls = " ".join(str(call) for call in self.event_log.call_args_list)
            self.assertNotIn(plaintext, calls)
            self.assertNotIn(replacement["token"], calls)
            self.assertIn("security.service_token.create", calls)
            self.assertIn("security.service_token.rotate", calls)
            self.assertIn("security.service_token.revoke", calls)

    def test_service_token_cannot_manage_service_tokens(self):
        bearer = api_security.create_service_token(
            self.db_path,
            name="Admin automation",
            scopes=["admin"],
            expires_in_days=30,
        )
        headers = {"Authorization": f"Bearer {bearer['token']}"}
        with (
            patch.object(webui, "_AUTH_DB_PATH", self.db_path),
            patch.object(webui, "_auth_cfg", return_value=self._config()),
            patch.object(webui, "log_event"),
        ):
            response = self.client.get("/api/config/service-tokens", headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "forbidden")

    def test_rotation_is_atomic_when_replacement_insert_fails(self):
        original = api_security.create_service_token(
            self.db_path,
            name="Atomic",
            scopes=["timers"],
            expires_in_days=30,
        )
        with patch.object(api_security, "token_digest", return_value=api_security.token_digest(original["token"])):
            with self.assertRaises(Exception):
                api_security.rotate_service_token(
                    self.db_path,
                    original["id"],
                    expires_in_days=30,
                )
        records = api_security.list_service_tokens(self.db_path)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["active"])


if __name__ == "__main__":
    unittest.main()
