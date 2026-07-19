from __future__ import annotations

import unittest
from unittest.mock import patch

import webui


class ConfigNavigationTests(unittest.TestCase):
    def setUp(self):
        self.auth_patch = patch.object(webui, "_auth_enabled", return_value=False)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.client = webui.app.test_client()

    def test_config_uses_unified_tabs_and_one_save_action(self):
        response = self.client.get("/config")

        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        self.assertIn('aria-label="Configuration sections"', markup)
        self.assertIn('href="/config#cfg-web-ui"', markup)
        self.assertIn('href="/config/digico"', markup)
        self.assertIn('href="/config/tvs"', markup)
        self.assertIn('href="/config/companion-surfaces"', markup)
        self.assertIn('href="/config/export"', markup)
        self.assertIn('href="/config/import"', markup)
        self.assertIn('id="config-save"', markup)
        self.assertNotIn("Export Config</a>", markup)
        self.assertNotIn("Import Config</a>", markup)

    def test_specialist_pages_keep_the_same_tabs_and_active_section(self):
        pages = {
            "/config/digico": 'active" href="/config/digico"',
            "/config/tvs": 'active" href="/config/tvs"',
            "/config/companion-surfaces": 'active" href="/config/companion-surfaces"',
            "/config/export": 'active" href="/config/export"',
            "/config/import": 'active" href="/config/import"',
        }

        for path, active_link in pages.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                self.assertIn('aria-label="Configuration sections"', markup)
                self.assertIn(active_link, markup)


if __name__ == "__main__":
    unittest.main()
