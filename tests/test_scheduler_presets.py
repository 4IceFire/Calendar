"""Preset triggers and timer offsets use only temporary storage and fake devices."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(threading.Thread, 'start'):
    import webui
from package.apps.calendar import storage, utils
from package.apps.calendar.models import Event, TimeOfTrigger, TriggerJob, TypeofTime, WeekDay
from package.apps.calendar.scheduler import ClockScheduler
from routing_presets import RoutingPresetStore, RoutingPresetRunner


class SchedulerPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        self.store = RoutingPresetStore(self.root)
        self.preset = self.store.save({'name': 'Welcome', 'media_id': 'a' * 32, 'output': 2,
                                       'actions': [{'label': 'Timer', 'method': 'POST',
                                                    'path': '/api/timers/apply', 'body': {'preset': 1}}]})
        self.config = {'auth_enabled': False, 'atem_media_enabled': True, 'atem_ip': '192.0.2.1',
                       'atem_media_destinations': [{'player': 1, 'slots': [1, 2], 'videohub_input': 3}]}
        self.backend = Mock()
        self.backend.list_presets.return_value = [{'id': 9, 'name': 'Stage'}]
        self.backend.apply_preset.return_value = {'ok': True}
        self.callbacks = []
        self.manager = Mock()
        self.manager.display.side_effect = lambda *args, **kwargs: self.callbacks.append(kwargs['on_complete'])
        self.runner = RoutingPresetRunner()
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.root / 'auth.db'),
            patch.object(webui, '_get_routing_preset_store', return_value=self.store),
            patch.object(webui, '_get_videohub_app', return_value=self.backend),
            patch.object(webui, '_get_media_library', return_value=Mock(get=Mock(return_value={'temporary': False}))),
            patch.object(webui, '_get_media_routing_manager', return_value=self.manager),
            patch.object(webui, '_routing_preset_runner', self.runner),
            patch.object(webui, '_active_media_job', return_value={}),
            patch.object(webui, '_effective_videohub_allowlists_for_user', return_value=([], [])),
            patch.object(webui, '_get_videohub_state_snapshot', return_value={'outputs': [{'number': 2, 'label': 'Foyer'}]}),
            patch.object(webui, '_invalidate_videohub_state_snapshot'),
            patch.object(webui, '_home_set_last_videohub_preset'),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_auth_cfg', return_value=self.config),
            patch.object(webui, 'log_event'),
            patch.object(utils, 'get_config', return_value=self.config),
            patch.object(utils, 'get_companion', return_value=None),
            patch('package.apps.calendar.scheduler._activity_log_scheduler_event'),
        ):
            self.stack.enter_context(context)
        self.scheduler = ClockScheduler(internal_action_executor=webui._execute_scheduler_internal_action)

    def job(self, kind, selection=None, timer=None):
        trigger = TimeOfTrigger(60, TypeofTime.BEFORE, uid='stable-cue', actionType=kind,
                                preset_action=selection, timer=timer)
        event = Event('Service', 41, WeekDay.Sunday, date(2026, 9, 20), time(10), True, [trigger])
        occurrence = datetime(2026, 9, 20, 10)
        return TriggerJob(occurrence - timedelta(minutes=60), event, occurrence, 0, trigger)

    def test_preset_references_and_ids_survive_storage(self):
        for kind, selection in [('videohub_preset', {'preset': 9}),
                                ('routing_preset', {'preset': self.preset['id'], 'output': 2})]:
            job = self.job(kind, selection)
            path = str(self.root / 'events.json')
            storage.save_events([job.event], path)
            loaded = storage.load_events_safe(path)[0]
            self.assertEqual(loaded.id, 41)
            self.assertEqual(loaded.times[0].uid, 'stable-cue')
            self.assertEqual(loaded.times[0].to_dict(), job.trigger.to_dict())

    def test_cli_json_round_trip_keeps_preset_selection_and_rejects_arbitrary_actions(self):
        from cli import _trigger_from_spec
        for kind, selection in [('videohub_preset', {'preset': 9}),
                                ('routing_preset', {'preset': self.preset['id'], 'output': 2})]:
            value = self.job(kind, selection).trigger.to_dict()
            self.assertEqual(_trigger_from_spec(json.dumps(value)).to_dict(), value)
            for invalid in (None, {}, {'preset': True}, {**selection, 'actions': []},
                            {**selection, 'output': -1}):
                with self.subTest(kind=kind, invalid=invalid), self.assertRaises(ValueError):
                    _trigger_from_spec(json.dumps({**value, 'preset_action': invalid}))

    def test_normalization_validates_existing_preset_and_fixed_destination(self):
        for kind, selection in [('videohub_preset', {'preset': 9}),
                                ('routing_preset', {'preset': self.preset['id'], 'output': 2})]:
            normalized, error = webui._normalize_trigger_action_spec(
                {'actionType': kind, 'preset_action': selection, 'uid': 'keep'})
            self.assertIsNone(error)
            self.assertEqual(normalized['uid'], 'keep')
            self.assertEqual(normalized['preset_action'], selection)
        for kind, selection in [('videohub_preset', {'preset': 88}),
                                ('videohub_preset', {'preset': True}),
                                ('routing_preset', {'preset': self.preset['id'], 'output': 3}),
                                ('routing_preset', {'preset': self.preset['id'], 'actions': []})]:
            self.assertIsNotNone(webui._normalize_trigger_action_spec(
                {'actionType': kind, 'preset_action': selection})[1])

    def test_template_create_edit_preserves_template_and_trigger_ids(self):
        with patch.object(webui, 'TRIGGER_TEMPLATES', self.root / 'templates.json'):
            client = webui.app.test_client()
            value = {'label': 'Preset cues', 'times': [self.job('videohub_preset', {'preset': 9}).trigger.to_dict()]}
            response = client.post('/api/templates/trigger', json=value)
            self.assertEqual(response.status_code, 200, response.get_json())
            identity = response.get_json()['template']['id']
            value['times'] = [self.job('routing_preset', {'preset': self.preset['id'], 'output': 2}).trigger.to_dict()]
            response = client.put('/api/templates/trigger/0', json=value)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['template']['id'], identity)
            saved = json.loads((self.root / 'templates.json').read_text())[0]
            self.assertEqual(saved['times'][0]['uid'], 'stable-cue')
            self.assertEqual(saved['times'][0]['preset_action']['preset'], self.preset['id'])

    def test_videohub_execution_uses_existing_verified_apply_and_reports_failure(self):
        job = self.job('videohub_preset', {'preset': 9})
        self.assertTrue(self.scheduler._handle_trigger(job))
        self.backend.apply_preset.assert_called_once_with(self.config, 9)
        self.backend.apply_preset.side_effect = RuntimeError('Readback mismatch')
        self.assertFalse(self.scheduler._handle_trigger(job))

    def test_routing_waits_for_display_then_executes_only_approved_actions(self):
        job = self.job('routing_preset', {'preset': self.preset['id'], 'output': 2})
        with patch.object(webui, '_execute_routing_preset_action', return_value=True) as execute:
            self.assertTrue(self.scheduler._handle_trigger(job))
            self.assertTrue(self.manager.display.call_args.kwargs['allow_shared_player'])
            execute.assert_not_called()
            self.callbacks[0]({'status': 'succeeded'})
            execute.assert_called_once()
        self.assertIsNone(self.runner.active_job())

    def test_disabled_unsafe_or_failed_image_never_executes_actions(self):
        job = self.job('routing_preset', {'preset': self.preset['id'], 'output': 2})
        with patch.object(webui, '_execute_routing_preset_action') as execute:
            self.assertTrue(self.scheduler._handle_trigger(job))
            self.callbacks[0]({'status': 'failed'})
            execute.assert_not_called()
        value = {key: value for key, value in self.preset.items() if key not in ('id', 'revision')}
        self.store.save({**value, 'enabled': False}, self.preset['id'], self.preset['revision'])
        self.assertFalse(self.scheduler._handle_trigger(job))
        unsafe = self.store.save({**value, 'actions': [{'label': 'Forbidden setup', 'method': 'POST',
                                                      'path': '/api/config', 'body': {}}]})
        self.assertFalse(self.scheduler._handle_trigger(self.job('routing_preset', {'preset': unsafe['id'], 'output': 2})))

    def test_generic_api_and_http_cannot_use_private_routing_capability(self):
        self.config['auth_enabled'] = True
        selection = {'preset': self.preset['id'], 'output': 2}
        self.assertFalse(webui._execute_scheduler_internal_action({'actionType': 'routing_preset', 'preset_action': selection}))
        self.assertFalse(webui._execute_scheduler_internal_action(
            {'method': 'POST', 'path': '/api/routing/presets/' + self.preset['id'] + '/apply', 'body': selection},
            self.job('api')))
        self.assertEqual(webui.app.test_client().post('/api/routing/presets/' + self.preset['id'] + '/apply', json=selection).status_code, 401)
        self.manager.display.assert_not_called()
        self.assertFalse(webui._preset_action_permitted({'label': 'Recursive', 'method': 'POST',
                         'path': '/api/routing/presets/' + self.preset['id'] + '/apply', 'body': selection}))

    def test_timer_target_offset_uses_service_start_not_trigger_due(self):
        for expression, expected in [('$-40', '09:20'), ('$0', '10:00'), ('$+15', '10:15')]:
            captured = []
            self.scheduler.set_internal_action_executor(lambda action, job: captured.append(action) or True)
            job = self.job('timer', timer={'preset': 1, 'time': expression, 'apply': True})
            self.assertTrue(self.scheduler._handle_trigger(job))
            body = captured[0]['body']
            self.assertEqual(body['event_due'], '2026-09-20T09:00:00')
            self.assertEqual(webui._resolve_time_hhmm_input(body['time'], body=body), (expected, None, True))
            self.assertEqual(body['action'], 'update_preset')
        self.assertEqual(webui._resolve_time_hhmm_input('$-40', body={'event_start': '2026-09-20T00:20:00'})[0], '23:40')
        self.assertIsNotNone(webui._resolve_time_hhmm_input('$-40', body={})[1])


if __name__ == '__main__':
    unittest.main()
