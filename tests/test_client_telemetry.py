from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webui


class _AuthenticatedUser:
    is_authenticated = True
    username = "telemetry-test"

    @staticmethod
    def get_id():
        return "1"


class ClientTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = patch.object(
            webui, "_AUTH_DB_PATH", Path(self.temp_dir.name) / "auth.db"
        )
        self.auth_patch = patch.object(webui, "_auth_enabled", return_value=False)
        self.db_patch.start()
        self.auth_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.auth_patch.stop)
        webui._init_auth_db()
        with webui._CLIENT_ERROR_RATE_LOCK:
            webui._CLIENT_ERROR_RATE_BUCKETS.clear()
        self.client = webui.app.test_client()

    def test_base_page_loads_versioned_telemetry_before_app(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        telemetry_pos = html.index("/static/client_telemetry.js?v=")
        app_pos = html.index("/static/app.js?v=")
        self.assertLess(telemetry_pos, app_pos)
        self.assertIn('data-endpoint="/api/client-errors"', html)
        self.assertRegex(html, r'data-build-id="(?:local-)?[A-Za-z0-9._-]+"')

    def test_report_is_whitelisted_truncated_and_redacted(self):
        response = self.client.post(
            "/api/client-errors",
            json={
                "kind": "unhandledrejection",
                "message": "Failed password=do-not-store token=also-secret",
                "stack": "at https://tdeck.invalid/static/app.js?v=private token=stack-secret",
                "source": "https://tdeck.invalid/static/app.js?token=query-secret",
                "route": "/routing?password=route-secret",
                "line": 14,
                "column": 8,
                "buildId": "release 42",
                "userAgent": "forged-user-agent",
                "requestBody": "must-never-be-stored",
                "password": "must-never-be-stored-either",
            },
            headers={"User-Agent": "TelemetryBrowser/1.0"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])

        conn = webui._db()
        try:
            row = conn.execute(
                "SELECT action,summary,details_json FROM activity_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["action"], "client.error")
        stored = f'{row["summary"]} {row["details_json"]}'
        for secret in (
            "do-not-store",
            "also-secret",
            "stack-secret",
            "query-secret",
            "route-secret",
            "must-never-be-stored",
            "forged-user-agent",
        ):
            self.assertNotIn(secret, stored)
        details = json.loads(row["details_json"])
        self.assertEqual(details["route"], "/routing")
        self.assertEqual(details["source_path"], "/static/app.js")
        self.assertEqual(details["user_agent"], "TelemetryBrowser/1.0")
        self.assertEqual(details["client_build_id"], "release-42")

    def test_endpoint_rejects_cross_site_non_json_and_large_payloads(self):
        cross_site = self.client.post(
            "/api/client-errors",
            json={"message": "boom"},
            headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(cross_site.status_code, 403)

        non_json = self.client.post(
            "/api/client-errors", data="message=boom", content_type="text/plain"
        )
        self.assertEqual(non_json.status_code, 415)

        oversized = self.client.post(
            "/api/client-errors", json={"message": "x" * 9000}
        )
        self.assertEqual(oversized.status_code, 413)

    def test_server_rate_limit_is_per_client(self):
        with patch.object(webui, "_CLIENT_ERROR_RATE_MAX", 2):
            first = self.client.post("/api/client-errors", json={"message": "one"})
            second = self.client.post("/api/client-errors", json={"message": "two"})
            limited = self.client.post("/api/client-errors", json={"message": "three"})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers.get("Retry-After"), "60")

    def test_auth_enabled_requires_login_and_csrf(self):
        self.auth_patch.stop()
        self.addCleanup(lambda: None)
        with patch.object(webui, "_auth_enabled", return_value=True):
            anonymous = self.client.post(
                "/api/client-errors", json={"message": "anonymous"}
            )
            self.assertEqual(anonymous.status_code, 401)

            with (
                patch.object(webui, "current_user", _AuthenticatedUser()),
                patch.object(webui, "_touch_current_user_session", return_value=True),
            ):
                with self.client.session_transaction() as state:
                    state["_csrf"] = "expected-csrf"
                missing_csrf = self.client.post(
                    "/api/client-errors", json={"message": "missing csrf"}
                )
                self.assertEqual(missing_csrf.status_code, 403)
                accepted = self.client.post(
                    "/api/client-errors",
                    json={"message": "authenticated report"},
                    headers={
                        "X-CSRF-Token": "expected-csrf",
                        "Origin": "http://localhost",
                    },
                )
                self.assertEqual(accepted.status_code, 202)


if __name__ == "__main__":
    unittest.main()
