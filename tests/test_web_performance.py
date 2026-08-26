from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import webui


class WebAssetDeliveryTests(unittest.TestCase):
    def _asset_url(self, filename: str) -> str:
        with webui.app.test_request_context('/'):
            return webui.static_asset(filename)

    def test_base_page_uses_bundled_content_hashed_assets(self):
        response = webui.app.test_client().get('/login')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertNotIn('cdn.jsdelivr.net', html)
        for asset in (
            'vendor/bootstrap/bootstrap.min.css',
            'vendor/bootstrap/bootstrap.bundle.min.js',
            'style.css',
            'app.js',
        ):
            match = re.search(rf'/static/{re.escape(asset)}\?v=([0-9a-f]{{64}})', html)
            self.assertIsNotNone(match, asset)

    def test_asset_identity_changes_with_bytes_even_when_size_and_mtime_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = root / 'sample.js'
            asset.write_bytes(b'first')
            initial_stat = asset.stat()
            original_static_folder = webui.app.static_folder
            webui.app.static_folder = str(root)
            try:
                first_url = self._asset_url('sample.js')
                asset.write_bytes(b'other')
                os.utime(
                    asset,
                    ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns),
                )
                second_url = self._asset_url('sample.js')
            finally:
                webui.app.static_folder = original_static_folder

            first_digest = parse_qs(urlsplit(first_url).query)['v'][0]
            second_digest = parse_qs(urlsplit(second_url).query)['v'][0]
            self.assertEqual(first_digest, hashlib.sha256(b'first').hexdigest())
            self.assertEqual(second_digest, hashlib.sha256(b'other').hexdigest())
            self.assertNotEqual(first_url, second_url)
            with webui._STATIC_ASSET_MANIFEST_LOCK:
                self.assertEqual(
                    webui._STATIC_ASSET_MANIFEST.get('sample.js'),
                    second_digest,
                )

    def test_invalid_and_missing_asset_names_are_not_versioned(self):
        traversal_url = self._asset_url('../../webui.py')
        missing_url = self._asset_url('does-not-exist.js')
        self.assertNotIn('webui.py', traversal_url)
        self.assertNotIn('v=', traversal_url)
        self.assertTrue(urlsplit(traversal_url).path.startswith('/static'))
        self.assertEqual(urlsplit(missing_url).path, '/static/does-not-exist.js')
        self.assertNotIn('v=', missing_url)

    def test_text_assets_are_compressed_and_cached(self):
        client = webui.app.test_client()
        path = self._asset_url('vendor/bootstrap/bootstrap.min.css')
        plain = client.get(path)
        compressed = client.get(path, headers={'Accept-Encoding': 'gzip'})
        conditional = client.get(
            path,
            headers={
                'Accept-Encoding': 'gzip',
                'If-None-Match': compressed.headers['ETag'],
            },
        )
        try:
            self.assertEqual(plain.status_code, 200)
            self.assertEqual(compressed.status_code, 200)
            self.assertEqual(compressed.headers.get('Content-Encoding'), 'gzip')
            self.assertIn('Accept-Encoding', compressed.headers.get('Vary', ''))
            self.assertIn('public', compressed.headers.get('Cache-Control', ''))
            self.assertIn('max-age=31536000', compressed.headers.get('Cache-Control', ''))
            self.assertIn('immutable', compressed.headers.get('Cache-Control', ''))
            self.assertEqual(gzip.decompress(compressed.data), plain.data)
            self.assertLess(len(compressed.data), len(plain.data) // 2)
            self.assertEqual(conditional.status_code, 304)
            self.assertIn('Accept-Encoding', conditional.headers.get('Vary', ''))
            self.assertIn('immutable', conditional.headers.get('Cache-Control', ''))
        finally:
            plain.close()
            compressed.close()
            conditional.close()

    def test_static_200_and_304_share_immutable_cache_policy(self):
        client = webui.app.test_client()
        path = self._asset_url('style.css')
        initial = client.get(path)
        conditional = client.get(path, headers={'If-None-Match': initial.headers['ETag']})
        try:
            self.assertEqual(initial.status_code, 200)
            self.assertEqual(conditional.status_code, 304)
            expected = 'public, max-age=31536000, immutable'
            self.assertEqual(initial.headers.get('Cache-Control'), expected)
            self.assertEqual(conditional.headers.get('Cache-Control'), expected)
            self.assertIn('Accept-Encoding', initial.headers.get('Vary', ''))
            self.assertIn('Accept-Encoding', conditional.headers.get('Vary', ''))
        finally:
            initial.close()
            conditional.close()

    def test_unversioned_or_forged_static_urls_must_revalidate(self):
        client = webui.app.test_client()
        for path in (
            '/static/style.css',
            '/static/style.css?v=test-version',
            f'/static/style.css?v={"0" * 64}',
        ):
            response = client.get(path)
            try:
                policy = response.headers.get('Cache-Control', '')
                self.assertIn('no-cache', policy, path)
                self.assertIn('must-revalidate', policy, path)
                self.assertNotIn('immutable', policy, path)
            finally:
                response.close()

    def test_html_is_compressed_for_supporting_clients(self):
        client = webui.app.test_client()
        plain = client.get('/login')
        compressed = client.get('/login', headers={'Accept-Encoding': 'gzip'})

        self.assertEqual(compressed.headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(compressed.data), plain.data)
        self.assertLess(len(compressed.data), len(plain.data))

    def test_html_revalidates_privately_and_api_state_is_never_stored(self):
        client = webui.app.test_client()
        html = client.get('/login')
        api = client.get('/api/upcoming_triggers')

        html_policy = html.headers.get('Cache-Control', '')
        self.assertIn('private', html_policy)
        self.assertIn('no-cache', html_policy)
        self.assertIn('must-revalidate', html_policy)
        self.assertEqual(api.headers.get('Cache-Control'), 'no-store')

    def test_api_reference_uses_pinned_local_dependencies(self):
        with patch.object(webui, '_auth_enabled', return_value=False):
            response = webui.app.test_client().get('/api-reference')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertRegex(
            html,
            r'/static/vendor/marked/marked\.min\.js\?v=[0-9a-f]{64}',
        )
        self.assertRegex(
            html,
            r'/static/vendor/dompurify/purify\.min\.js\?v=[0-9a-f]{64}',
        )
        self.assertNotIn('cdn.jsdelivr.net', html)

    def test_templates_have_no_public_runtime_asset_urls(self):
        template_root = Path(webui.app.template_folder)
        external_asset = re.compile(
            r'''(?:src|href)\s*=\s*["']https?://''',
            re.IGNORECASE,
        )
        violations = []
        for path in template_root.rglob('*.html'):
            if external_asset.search(path.read_text(encoding='utf-8')):
                violations.append(str(path))
        self.assertEqual(violations, [])

    def test_vendored_dependency_versions_and_licenses_are_present(self):
        static_root = Path(webui.app.static_folder)
        marked = (static_root / 'vendor/marked/marked.min.js').read_text(encoding='utf-8')
        dompurify = (static_root / 'vendor/dompurify/purify.min.js').read_text(encoding='utf-8')
        self.assertIn('marked v12.0.2', marked[:200])
        self.assertIn('DOMPurify 3.2.6', dompurify[:300])
        self.assertTrue((static_root / 'vendor/marked/LICENSE.md').is_file())
        self.assertTrue((static_root / 'vendor/dompurify/LICENSE').is_file())
        self.assertTrue((static_root / 'vendor/THIRD_PARTY_NOTICES.md').is_file())


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
            with (
                patch.object(webui, "_auth_enabled", return_value=False),
                patch.object(
                    webui, "_get_videohub_client_from_config", return_value=_SlowVideohub()
                ),
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
