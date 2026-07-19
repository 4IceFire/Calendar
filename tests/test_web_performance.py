from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import webui


class RequestIsolationTests(unittest.TestCase):
    def test_user_snapshot_reuses_page_and_mixer_permissions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ):
            webui._init_auth_db()
            conn = webui._db()
            try:
                user_id = int(
                    conn.execute(
                        "INSERT INTO users(username,password_hash,is_active) VALUES (?,?,1)",
                        ("mixer-user", "unused"),
                    ).lastrowid
                )
                group_id = int(
                    conn.execute(
                        """
                        INSERT INTO groups(
                          name,is_admin,auth_idle_timeout_minutes_override,
                          videohub_allowed_outputs,videohub_allowed_inputs,digico_allowed_auxes
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            "Worship Team",
                            0,
                            15,
                            json.dumps([2, 4]),
                            json.dumps([1, 3]),
                            json.dumps(["2", "5"]),
                        ),
                    ).lastrowid
                )
                conn.execute(
                    "INSERT INTO user_groups(user_id,group_id) VALUES (?,?)",
                    (user_id, group_id),
                )
                for page_key in ("page:routing", "page:digico_mixer"):
                    conn.execute(
                        "INSERT INTO group_pages(group_id,page_key) VALUES (?,?)",
                        (group_id, page_key),
                    )
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            finally:
                conn.close()

            user = webui._User(row)
            self.assertTrue(user.allows_page("page:routing"))
            self.assertTrue(user.allows_page("page:digico_mixer"))
            self.assertFalse(user.allows_page("page:admin"))
            self.assertEqual(user.videohub_allowed_outputs, [2, 4])
            self.assertEqual(user.videohub_allowed_inputs, [1, 3])
            self.assertEqual(user.digico_allowed_auxes, ["2", "5"])
            self.assertEqual(user.idle_timeout_override, 15)

            with (
                patch.object(webui, "_auth_enabled", return_value=True),
                patch.object(webui, "current_user", user),
                patch.object(
                    webui,
                    "_user_allows_page",
                    side_effect=AssertionError("database fallback should not run"),
                ),
            ):
                self.assertTrue(webui.can_access("page:routing"))

    def test_permissions_sources_do_not_wait_for_atem(self):
        entered = threading.Event()
        release = threading.Event()

        class _SlowAtem:
            def get_audio_state(self):
                entered.set()
                release.wait(2)
                return {"sources": [{"id": "1", "label": "Camera", "kind": "input"}]}

        with webui._atem_permission_sources_lock:
            old_cache = dict(webui._atem_permission_sources_cache)
            webui._atem_permission_sources_cache.update(
                {"ts": 0.0, "payload": None, "refreshing": False}
            )
        try:
            with patch.object(webui, "_get_atem_client_from_config", return_value=_SlowAtem()):
                sources = webui._get_atem_audio_sources_for_permissions()
                self.assertTrue(sources)
                self.assertTrue(entered.wait(0.5))
                release.set()
                deadline = time.time() + 1
                while time.time() < deadline:
                    with webui._atem_permission_sources_lock:
                        if not webui._atem_permission_sources_cache["refreshing"]:
                            break
                    time.sleep(0.01)
                with webui._atem_permission_sources_lock:
                    self.assertFalse(webui._atem_permission_sources_cache["refreshing"])
                    self.assertEqual(
                        webui._atem_permission_sources_cache["payload"][0]["label"],
                        "Camera",
                    )
        finally:
            release.set()
            with webui._atem_permission_sources_lock:
                webui._atem_permission_sources_cache.clear()
                webui._atem_permission_sources_cache.update(old_cache)

    def test_routing_state_refresh_does_not_wait_for_videohub(self):
        entered = threading.Event()
        release = threading.Event()

        class _SlowVideohub:
            def get_state(self, *, fallback_count=40):
                entered.set()
                release.wait(2)
                return {
                    "inputs": [{"number": 1, "label": "Stage"}],
                    "outputs": [{"number": 1, "label": "Screen"}],
                    "routing": [1],
                }

        with webui._status_cache_lock:
            old_cache = dict(webui._videohub_state_cache)
            webui._videohub_state_cache.update({"ts": 0.0, "payload": None})
        with webui._videohub_state_refresh_lock:
            old_refreshing = webui._videohub_state_refreshing
            webui._videohub_state_refreshing = False
        try:
            with patch.object(
                webui, "_get_videohub_client_from_config", return_value=_SlowVideohub()
            ):
                response = webui.app.test_client().get("/api/videohub/state")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["refreshing"])
                self.assertEqual(len(response.get_json()["inputs"]), 40)
                self.assertTrue(entered.wait(0.5))
                release.set()
                deadline = time.time() + 1
                while time.time() < deadline:
                    with webui._videohub_state_refresh_lock:
                        if not webui._videohub_state_refreshing:
                            break
                    time.sleep(0.01)

                refreshed = webui.app.test_client().get("/api/videohub/state")
                self.assertFalse(refreshed.get_json().get("refreshing", False))
                self.assertEqual(refreshed.get_json()["inputs"][0]["label"], "Stage")
        finally:
            release.set()
            with webui._status_cache_lock:
                webui._videohub_state_cache.clear()
                webui._videohub_state_cache.update(old_cache)
            with webui._videohub_state_refresh_lock:
                webui._videohub_state_refreshing = old_refreshing


if __name__ == "__main__":
    unittest.main()
