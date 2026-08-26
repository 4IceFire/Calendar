import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendCompatibilityTests(unittest.TestCase):
    def test_page_features_use_explicit_initializers(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        initializers = (
            "_initRoutingPage",
            "_initConfigPage",
            "_initTimersPage",
            "_initPermissionsPage",
            "_initAdminUserDetailPage",
            "_initAccessLevelsPage",
            "_initCompanionSurfacesConfigPage",
            "_initFoyerAudioPage",
        )
        for initializer in initializers:
            with self.subTest(initializer=initializer):
                self.assertIn(f"function {initializer}()", source)
                self.assertIn(f"{initializer}();", source)

        self.assertIsNone(
            re.search(
                r"^if\s*\(document\.getElementById\(['\"][^'\"]+-page['\"]\)\)\s*\{",
                source,
                re.MULTILINE,
            ),
            "Page code must not use top-level conditional blocks; older WebKit can mis-scope nested functions.",
        )

    def test_supported_frontend_avoids_newer_untranspiled_methods(self):
        javascript = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "static").glob("*.js"))
        )
        for unsupported in (".at(", ".finally(", ".replaceChildren("):
            with self.subTest(unsupported=unsupported):
                self.assertNotIn(unsupported, javascript)

    def test_dialog_and_record_audio_have_runtime_fallbacks(self):
        digico = (ROOT / "static" / "digico_setup.js").read_text(encoding="utf-8")
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("typeof dialog.showModal === 'function'", digico)
        self.assertIn("digico-icon-dialog-fallback", digico)
        self.assertIn("/api/atem/audio/meters", app)
        self.assertIn("AbortController", app)
        self.assertIn("visibilitychange", app)
        self.assertNotRegex(app, r"setInterval\([^\n]*_loadState")

    def test_api_token_page_has_an_explicit_initializer(self):
        source = (ROOT / "static" / "api_tokens.js").read_text(encoding="utf-8")
        self.assertIn("function _initApiTokensPage()", source)
        self.assertIn("_initApiTokensPage();", source)
        self.assertNotRegex(
            source,
            r"^if\s*\(document\.getElementById\(['\"][^'\"]+-page['\"]\)\)\s*\{",
        )


if __name__ == "__main__":
    unittest.main()
