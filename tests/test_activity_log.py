from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webui


class ActivityLogTests(unittest.TestCase):
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
        conn = webui._db()
        try:
            conn.execute("DELETE FROM activity_log")
            conn.execute("DELETE FROM activity_log_ack")
            conn.executemany(
                """
                INSERT INTO activity_log(
                  ts,actor_username,actor_display,source,action,status,summary
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        "2026-07-18 09:00:00",
                        "operator",
                        "Operator",
                        "web",
                        "timer.apply",
                        "success",
                        "Applied timer",
                    ),
                    (
                        "2026-07-19 10:00:00",
                        "",
                        "System",
                        "system",
                        "videohub.connection",
                        "warning",
                        "VideoHub disconnected",
                    ),
                    (
                        "2026-07-19 11:00:00",
                        "",
                        "API",
                        "api",
                        "propresenter.timer.start",
                        "failure",
                        "Timer failed",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        self.client = webui.app.test_client()

    def test_activity_log_filters_and_pagination(self):
        response = self.client.get("/api/activity-log?limit=2&page=1")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["total_pages"], 2)
        self.assertEqual([event["status"] for event in data["events"]], ["failure", "warning"])

        response = self.client.get("/api/activity-log?status=warning")
        data = response.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["events"][0]["action"], "videohub.connection")

        response = self.client.get(
            "/api/activity-log?source=api&start=2026-07-19T00%3A00&end=2026-07-19T23%3A59"
        )
        data = response.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["events"][0]["summary"], "Timer failed")

    def test_alerts_can_be_acknowledged(self):
        response = self.client.get("/api/activity-log/alerts")
        data = response.get_json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["failures"], 1)
        self.assertEqual(data["warnings"], 1)

        response = self.client.post("/api/activity-log/alerts/acknowledge")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 0)


if __name__ == "__main__":
    unittest.main()
