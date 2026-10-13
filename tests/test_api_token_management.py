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

    def test_permissions_edit_keeps_identity_and_changes_current_authorization(self):
        original = api_security.create_service_token(self.db_path, name='Companion', scopes=['read'], expires_in_days=30)
        expected = {'scopes': original['scopes'], 'constraints': original['constraints']}
        with self._secure_context(admin=True):
            response = self.client.patch('/api/config/service-tokens/' + str(original['id']), headers=self._csrf_headers(),
                json={'scopes': ['read', 'routing_presets'], 'constraints': {'routing_outputs': [2]}, 'expected': expected, 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        updated = response.get_json()['token']
        self.assertEqual(updated['id'], original['id'])
        self.assertEqual(updated['expires_at'], original['expires_at'])
        self.assertNotIn('token', updated); self.assertNotIn('token_hash', updated)
        record, error = api_security.authenticate_service_token(self.db_path, original['token'], update_last_used=False)
        self.assertIsNone(error); self.assertIn('routing_presets', record['scopes']); self.assertEqual(record['constraints'], {'routing_outputs': [2]})
        self.assertIn('security.service_token.permissions', str(self.event_log.call_args_list))
        self.assertNotIn(original['token'], str(self.event_log.call_args_list))

    def test_permissions_edit_denials_and_conflict_leave_metadata_unchanged(self):
        original = api_security.create_service_token(self.db_path, name='Restricted', scopes=['read'])
        expected = {'scopes': original['scopes'], 'constraints': original['constraints']}
        body = {'scopes': ['timers'], 'constraints': {}, 'expected': expected, 'confirmed': True}
        path = '/api/config/service-tokens/' + str(original['id'])
        with self._secure_context(admin=False):
            self.assertEqual(self.client.patch(path, json=body, headers=self._csrf_headers()).status_code, 403)
        with self._secure_context(admin=True):
            headers = self._csrf_headers()
            self.assertEqual(self.client.patch(path, json=body).status_code, 403)
            for value in ([], [0], [True], 'all', None):
                bad = {**body, 'constraints': {'videohub_outputs': value}}
                self.assertEqual(self.client.patch(path, json=bad, headers=headers).status_code, 400)
            self.assertEqual(self.client.patch(path, json={**body, 'confirmed': False}, headers=headers).status_code, 400)
            self.assertEqual(api_security.list_service_tokens(self.db_path)[0]['scopes'], ['read'])
            self.assertEqual(self.client.patch(path, json=body, headers=headers).status_code, 200)
            self.assertEqual(self.client.patch(path, json=body, headers=headers).status_code, 409)
        token = api_security.create_service_token(self.db_path, name='Cannot self-edit', scopes=['*'])
        with patch.object(webui, '_AUTH_DB_PATH', self.db_path), patch.object(webui, '_auth_cfg', return_value=self._config()):
            self.assertEqual(self.client.patch(path, json=body, headers={'Authorization': 'Bearer '+token['token']}).status_code, 403)

    def test_permissions_edit_does_not_revive_revoked_token(self):
        original = api_security.create_service_token(self.db_path, name='Revoked', scopes=['read'])
        api_security.revoke_service_token(self.db_path, original['id'])
        with self._secure_context(admin=True):
            response = self.client.patch('/api/config/service-tokens/' + str(original['id']), headers=self._csrf_headers(),
                json={'scopes': ['timers'], 'constraints': {}, 'expected': {'scopes':['read'],'constraints':{}}, 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(response.get_json()['token']['active'])
        self.assertEqual(api_security.authenticate_service_token(self.db_path, original['token'])[1], 'revoked_token')

    def test_permissions_reduction_takes_effect_on_next_bearer_request(self):
        from unittest.mock import Mock
        original = api_security.create_service_token(self.db_path, name='Reduce', scopes=['read','routing_presets'])
        store = Mock(); store.list.return_value = []
        with self._secure_context(admin=True), patch.object(webui, '_get_routing_preset_store', return_value=store):
            auth = {'Authorization':'Bearer '+original['token']}
            self.assertEqual(self.client.get('/api/automation/routing-presets', headers=auth).status_code, 200)
            response = self.client.patch('/api/config/service-tokens/'+str(original['id']), headers=self._csrf_headers(),
                json={'scopes':['read'],'constraints':{},'expected':{'scopes':original['scopes'],'constraints':{}},'confirmed':True})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.client.get('/api/automation/routing-presets', headers=auth).status_code, 403)

    def test_permissions_edit_does_not_extend_expired_token(self):
        import sqlite3
        from contextlib import closing
        original = api_security.create_service_token(self.db_path, name='Expired', scopes=['read'])
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute('UPDATE service_tokens SET expires_at=? WHERE id=?', ('2020-01-01T00:00:00Z',original['id']));conn.commit()
        with self._secure_context(admin=True):
            response = self.client.patch('/api/config/service-tokens/'+str(original['id']), headers=self._csrf_headers(),
                json={'scopes':['timers'],'constraints':{},'expected':{'scopes':['read'],'constraints':{}},'confirmed':True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['token']['expires_at'],'2020-01-01T00:00:00Z')
        self.assertEqual(api_security.authenticate_service_token(self.db_path,original['token'])[1],'expired_token')

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
