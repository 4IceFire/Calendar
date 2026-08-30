from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import webui


class QualityOfLifeChangesTests(unittest.TestCase):
    def test_login_page_does_not_publish_default_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ), patch.object(webui, "_auth_enabled", return_value=True):
            response = webui.app.test_client().get("/login")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertNotIn("Default admin credentials", markup)
        self.assertNotIn("admin</strong> / <strong>admin", markup)

    def test_signed_out_login_hides_connection_statuses(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ), patch.object(webui, "_auth_enabled", return_value=True):
            response = webui.app.test_client().get("/login")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertNotIn('class="tdeck-nav-status"', markup)
        self.assertNotIn('id="mobile-connection-indicator"', markup)
        self.assertNotIn('id="companion-indicator"', markup)
        self.assertNotIn('id="propresenter-indicator"', markup)
        self.assertNotIn('id="videohub-indicator"', markup)

    def test_connection_statuses_return_after_successful_login(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ), patch.object(webui, "_auth_enabled", return_value=True):
            webui._bootstrap_default_users_roles()
            conn = webui._db()
            try:
                group_id = int(
                    conn.execute(
                        "INSERT INTO groups(name,is_admin) VALUES (?,0)",
                        ("Home Viewers",),
                    ).lastrowid
                )
                user_id = int(
                    conn.execute(
                        """
                        INSERT INTO users(username,password_hash,is_active)
                        VALUES (?,?,1)
                        """,
                        ("status-user", generate_password_hash("status-password")),
                    ).lastrowid
                )
                conn.execute(
                    "INSERT INTO user_groups(user_id,group_id) VALUES (?,?)",
                    (user_id, group_id),
                )
                conn.execute(
                    "INSERT INTO group_pages(group_id,page_key) VALUES (?,?)",
                    (group_id, "page:home"),
                )
                conn.commit()
            finally:
                conn.close()

            client = webui.app.test_client()
            client.get("/login")
            with client.session_transaction() as session_state:
                csrf_token = session_state["_csrf"]
            response = client.post(
                "/login",
                data={
                    "_csrf": csrf_token,
                    "username": "status-user",
                    "password": "status-password",
                    "next": "/",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn('class="tdeck-nav-status"', markup)
        self.assertIn('id="companion-indicator"', markup)

    def test_login_form_provides_password_manager_hints(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ), patch.object(webui, "_auth_enabled", return_value=True):
            response = webui.app.test_client().get("/login")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn('<form method="post" action="/login" autocomplete="on">', markup)
        self.assertIn('id="login-username"', markup)
        self.assertIn('autocomplete="username"', markup)
        self.assertIn('autocapitalize="none"', markup)
        self.assertIn('id="login-password"', markup)
        self.assertIn('autocomplete="current-password"', markup)

    def test_login_without_home_access_redirects_to_personal_mixes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
        ), patch.object(webui, "_auth_enabled", return_value=True):
            webui._bootstrap_default_users_roles()
            conn = webui._db()
            try:
                group_id = int(
                    conn.execute(
                        "INSERT INTO groups(name,is_admin) VALUES (?,0)",
                        ("Personal Mixers",),
                    ).lastrowid
                )
                user_id = int(
                    conn.execute(
                        """
                        INSERT INTO users(username,password_hash,is_active)
                        VALUES (?,?,1)
                        """,
                        ("mix-user", generate_password_hash("mix-password")),
                    ).lastrowid
                )
                conn.execute(
                    "INSERT INTO user_groups(user_id,group_id) VALUES (?,?)",
                    (user_id, group_id),
                )
                conn.execute(
                    "INSERT INTO group_pages(group_id,page_key) VALUES (?,?)",
                    (group_id, "page:digico_mixer"),
                )
                conn.commit()
            finally:
                conn.close()

            client = webui.app.test_client()
            login_page = client.get("/login?next=/")
            self.assertEqual(login_page.status_code, 200)
            with client.session_transaction() as session_state:
                csrf_token = session_state["_csrf"]

            response = client.post(
                "/login",
                data={
                    "_csrf": csrf_token,
                    "username": "mix-user",
                    "password": "mix-password",
                    "next": "/",
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/personal-mixes")

    def test_record_audio_name_is_used_while_route_stays_compatible(self):
        with patch.object(webui, "_auth_enabled", return_value=False):
            response = webui.app.test_client().get("/foyer-audio")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn("Record Audio", markup)
        self.assertNotIn(">Foyer Audio<", markup)

    def test_base_template_has_mobile_connection_summary(self):
        with patch.object(webui, "_auth_enabled", return_value=False):
            response = webui.app.test_client().get("/timers")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn('id="mobile-connection-indicator"', markup)
        self.assertIn('id="mobile-connection-label"', markup)

        app_js = (Path(webui.__file__).resolve().parent / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("All connections are online", app_js)
        self.assertIn("`${offlineLabel} is offline`", app_js)
        self.assertIn("_MOBILE_CONNECTION_CYCLE_MS = 2000", app_js)


if __name__ == "__main__":
    unittest.main()
