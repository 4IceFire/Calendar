"""Personal Mixes display metadata never changes physical routes or grants."""
import copy
from unittest.mock import patch
from test_dlive_webui import DLiveApiTests


class OrganizationApiTests(DLiveApiTests):
    def test_digico_indexed_save_and_legacy_read_are_non_migrating(self):
        from types import SimpleNamespace
        from personal_mix_organization import project_channels
        old = [dict(channel=1, group='Vocals'), dict(channel=2, group=''),
               dict(channel=3, group='Band'), dict(channel=4, group='Vocals')]
        self.assertIs(project_channels(old, None), old)
        before = copy.deepcopy(self.cfg)
        self.client.get('/api/digico/setup')
        self.assertEqual(before, self.cfg)
        organization = dict(folders=[dict(id='later', name='Renamed'), dict(id='first', name='Renamed')],
                            items=[dict(id='3', folderId='later'), dict(id='2', folderId='first'), dict(id='1', folderId=None)])
        data = dict(digico_channels=[dict(channel=3, label='Three', icon='drums', order=1),
                                    dict(channel=1, label='One', icon='electric', order=2),
                                    dict(channel=2, label='Two', enabled=False, order=3)],
                    digico_channel_organization=organization)
        response = self.client.post('/api/digico/setup', headers=self.headers, json=data)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual([c['label'] for c in self.cfg['digico_channels']], ['One', 'Two', 'Three'])
        self.assertEqual([c['order'] for c in self.cfg['digico_channels']], [2, 3, 1])
        self.assertEqual(self.cfg['dlive_channels'], before['dlive_channels'])
        rows = [dict(channel=n, **c) for n, c in enumerate(self.cfg['digico_channels'], 1)]
        # New discoveries append to root without writing metadata or merging names.
        result = project_channels(rows + [dict(channel=4)], self.cfg['digico_channel_organization'])
        self.assertEqual([c['channel'] for c in result], [1, 4, 3, 2])
        self.assertEqual([c['groupId'] for c in result], [None, None, 'later', 'first'])

    def test_stale_generic_snapshot_preserves_setup_owned_digico_and_active_dlive(self):
        from personal_mixes import validate_config
        from test_dlive import wait_for
        # A Config tab opened before the dedicated setup has empty mapping arrays.
        stale = copy.deepcopy(self.cfg)
        stale.update(digico_channels=[], digico_auxes=[], digico_external_devices=[],
                     digico_enabled=True, digico_ip='stale.invalid', digico_port=9999,
                     digico_listen_address='127.0.0.2', digico_listen_port=9998,
                     digico_request_interval=5, digico_retry_interval=30, digico_stale_after=300)
        organization = dict(folders=[dict(id='band', name='Band')],
                            items=[dict(id='1', folderId='band')])
        setup = dict(digico_enabled=False, digico_ip='127.0.0.1', digico_port=9000,
                     digico_listen_address='127.0.0.1', digico_listen_port=8000,
                     digico_request_interval=.1, digico_retry_interval=1, digico_stale_after=10,
                     digico_channels=[dict(channel=1, label='Dedicated input')],
                     digico_auxes=[dict(channel=1, label='Dedicated AUX')],
                     digico_external_devices=[dict(ip='127.0.0.3', enabled=False)],
                     digico_channel_organization=organization)
        response = self.client.post('/api/digico/setup', headers=self.headers, json=setup)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = copy.deepcopy(self.cfg)
        stale['debug'] = not bool(saved.get('debug'))
        response = self.client.post('/api/config', headers=self.headers, json=stale)
        self.assertEqual(response.status_code, 200, response.get_json())
        readback = self.client.get('/api/digico/setup').get_json()['config']
        self.assertEqual(readback['digico_channels'], saved['digico_channels'])
        for key in setup:
            self.assertEqual(readback[key], saved[key], key)
        self.assertEqual(self.cfg['debug'], stale['debug'])
        validate_config(self.cfg)  # Inactive desk metadata must not poison active dLive.
        self.assertEqual(self.client.get('/api/digico/mixer/config').get_json()['provider'], 'dlive')
        wait_for(lambda: self.client.get('/api/digico/aux/2002/state').get_json()['status']['connected'])
        self.assertEqual(self.client.get('/api/digico/aux/2002/state').status_code, 200)

    def test_config_import_validates_both_metadata_keys_before_replacement(self):
        import json
        import zipfile
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'config.json'
            self.cfg['digico_channels'] = [dict(label='SD 1')]
            target.write_text(json.dumps(self.cfg))
            with patch.object(self.webui, '_APP_ROOT', root), patch.object(self.webui.utils, 'CONFIG_FILE', str(target)), patch.object(self.webui, '_clear_imported_config_caches'):
                payload, _ = self.webui._create_config_transport_zip(['config'], reason='test')
                source = root / 'source.zip'
                source.write_bytes(payload)
                with zipfile.ZipFile(source) as archive:
                    entries = {name: archive.read(name) for name in archive.namelist()}
                config_name = next(name for name in entries if name.endswith('config.json'))
                for provider in ('digico', 'dlive'):
                    candidate = json.loads(entries[config_name])
                    candidate[provider + '_channel_organization'] = dict(folders=[], items=[dict(id='999', folderId=None)])
                    bad = root / 'bad.zip'
                    with zipfile.ZipFile(bad, 'w') as archive:
                        for name, content in entries.items():
                            archive.writestr(name, json.dumps(candidate) if name == config_name else content)
                    before = target.read_bytes()
                    with self.assertRaises(ValueError):
                        self.webui._apply_config_transport_import(bad, ['config'])
                    self.assertEqual(target.read_bytes(), before)
                canonical = dict(folders=[dict(id='group', name='Imported')], items=[dict(id='1', folderId='group')])
                candidate = json.loads(entries[config_name])
                candidate.update(dlive_channel_organization=canonical, digico_channel_organization=canonical)
                good = root / 'good.zip'
                with zipfile.ZipFile(good, 'w') as archive:
                    for name, content in entries.items():
                        archive.writestr(name, json.dumps(candidate) if name == config_name else content)
                self.webui._apply_config_transport_import(good, ['config'])
                restored = json.loads(target.read_text())
                for provider in ('digico', 'dlive'):
                    self.assertEqual(restored[provider + '_channel_organization']['folders'][0]['id'], 'group')
                self.assertFalse(restored['dlive_enabled'])

    def test_invalid_metadata_is_atomic_and_generic_editor_cannot_own_it(self):
        organization = dict(folders=[dict(id='band', name='Band')], items=[dict(id='1', folderId='band')])
        # Both providers validate against their own physical IDs, including indexed DiGiCo arrays.
        for provider in ('dlive', 'digico'):
            for bad in (
                dict(folders=[dict(id=23, name='Bad')], items=[dict(id='1', folderId='23')]),
                dict(folders=[dict(id='band', name=['Bad'])], items=[dict(id='1', folderId='band')]),
                dict(folders=[], items=[dict(id='999', folderId=None)]),
                dict(folders=[], items=[dict(id='1'), dict(id='1')]),
            ):
                before = copy.deepcopy(self.cfg)
                response = self.client.post('/api/digico/setup', headers=self.headers,
                    json={provider + '_channel_organization': bad})
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(self.cfg, before)
            response = self.client.post('/api/config', headers=self.headers,
                                        json={provider + '_channel_organization': organization})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertIsNone(self.cfg.get(provider + '_channel_organization'))
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text()
        hidden = script.split('const hiddenKeys = new Set([', 1)[1].split(']);', 1)[0]
        self.assertIn("'dlive_channel_organization'", hidden)
        self.assertIn("'digico_channel_organization'", hidden)

    def test_explicit_groups_roundtrip_and_drive_performer_order(self):
        channels = [dict(channel=n, label=f'Input {n}', enabled=n != 2, icon='drums') for n in (1, 2, 3, 4)]
        organization = dict(folders=[dict(id='b', name='Band'), dict(id='a', name='Band'), dict(id='empty', name='Empty')],
                            items=[dict(id='3', folderId='b'), dict(id='2', folderId='b'),
                                   dict(id='1', folderId='a'), dict(id='4', folderId=None)])
        response = self.client.post('/api/digico/setup', headers=self.headers,
                                    json=dict(dlive_channels=channels, dlive_channel_organization=organization))
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = self.client.get('/api/digico/setup').get_json()['config']
        self.assertEqual([f['id'] for f in saved['dlive_channel_organization']['folders']], ['b', 'a', 'empty'])
        self.assertEqual([c['channel'] for c in saved['dlive_channels']], [1, 2, 3, 4])
        visible = self.client.get('/api/digico/mixer/config').get_json()['channels']
        self.assertEqual([c['channel'] for c in visible], [4, 3, 1])
        self.assertEqual([c['groupId'] for c in visible], [None, 'b', 'a'])
        self.assertEqual([c['group'] for c in visible], ['Ungrouped', 'Band', 'Band'])
        self.assertTrue(all(c['icon'] == 'drums' for c in visible))
        self.assertFalse(any(m.startswith(bytes.fromhex('b5')) or m.startswith(bytes.fromhex('95')) or
                             m.startswith(bytes.fromhex('f0 00 00 1a 50 10 01 00 03 0d')) for m in self.desk.messages))


# Reuse fixture setup without inheriting its unrelated test methods.
for name in DLiveApiTests.__dict__:
    if name.startswith('test_'):
        setattr(OrganizationApiTests, name, None)
del DLiveApiTests
