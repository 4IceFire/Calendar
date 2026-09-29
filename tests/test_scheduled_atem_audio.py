"""Scheduled ATEM audio actions use fake ATEM controls only."""
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


class ScheduledAtemAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        self.atem = Mock()
        self.atem.get_audio_state.return_value = {
            'connected': True,
            'sources': [
                {'id': 'master', 'label': 'Master', 'volume': 0.0, 'muted': False},
                {'id': '7', 'label': 'Lectern', 'volume': -12.0, 'muted': False},
            ],
        }
        self.config = {'auth_enabled': False, 'atem_ip': '192.0.2.1', 'EVENTS_FILE': str(self.root / 'events.json')}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.root / 'auth.db'),
            patch.object(webui, '_get_atem_client_from_config', return_value=self.atem),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_auth_cfg', return_value=self.config),
            patch.object(webui, 'log_event'),
            patch.object(utils, 'get_config', return_value=self.config),
            patch.object(utils, 'get_companion', return_value=None),
            patch('package.apps.calendar.scheduler._activity_log_scheduler_event'),
        ):
            self.stack.enter_context(context)
        self.scheduler = ClockScheduler(internal_action_executor=webui._execute_scheduler_internal_action)

    @staticmethod
    def _job(trigger):
        event = Event('Service', 41, WeekDay.Sunday, date(2026, 9, 20), time(10), True, [trigger])
        occurrence = datetime(2026, 9, 20, 10)
        return TriggerJob(occurrence - timedelta(minutes=60), event, occurrence, 0, trigger)

    def test_trigger_normalization_accepts_volume_and_rejects_master_mute(self):
        normalized, error = webui._normalize_trigger_action_spec({
            'actionType': 'atem_audio',
            'source_id': 'master',
            'operation': 'volume',
            'db': -18.5,
            'fade_seconds': 12.3,
        })
        self.assertIsNone(error)
        self.assertEqual(normalized['atem_audio'], {
            'source_id': 'master', 'operation': 'volume', 'db': -18.5, 'fade_seconds': 12.3,
        })
        self.assertIsNotNone(webui._normalize_trigger_action_spec({
            'actionType': 'atem_audio', 'source_id': 'master', 'operation': 'mute',
        })[1])

    def test_calendar_editor_has_atem_audio_action_controls(self):
        editor = (Path(__file__).resolve().parents[1] / 'templates' / 'calendar_new.html').read_text(encoding='utf-8')
        self.assertIn('<option value="atem_audio">ATEM Audio</option>', editor)
        self.assertIn('/api/templates/atem-audio-sources', editor)
        self.assertIn('name="atemAudioSource"', editor)
        self.assertIn('name="atemAudioOperation"', editor)
        self.assertIn('name="atemAudioDb"', editor)
        self.assertIn('name="atemAudioFade"', editor)

    def test_template_audio_source_catalog_includes_master_and_all_atem_sources(self):
        self.atem.get_audio_state.return_value = {
            'connected': True,
            'sources': [
                {'id': 'master', 'label': 'Master', 'kind': 'master'},
                {'id': '7', 'label': 'Lectern', 'kind': 'input'},
            ],
        }

        response = webui.app.test_client().get('/api/templates/atem-audio-sources')

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['sources'], self.atem.get_audio_state.return_value['sources'])

    def test_action_rejects_non_finite_fade(self):
        response = webui.app.test_client().post('/api/atem/audio/action', json={
            'source_id': '7', 'operation': 'volume', 'db': -18.5, 'fade_seconds': float('nan'),
        })

        self.assertEqual(response.status_code, 400, response.get_json())
        self.atem.set_volume.assert_not_called()

    def test_action_logs_failure_when_atem_is_unavailable(self):
        webui.log_event.reset_mock()
        with patch.object(webui, '_get_atem_client_from_config', return_value=None):
            response = webui.app.test_client().post('/api/atem/audio/action', json={
                'source_id': '7', 'operation': 'mute',
            })

        self.assertEqual(response.status_code, 502, response.get_json())
        self.assertEqual(webui.log_event.call_args.args[0], 'atem.audio.action')
        self.assertEqual(webui.log_event.call_args.kwargs['status'], 'failure')

    def test_action_rejects_unavailable_source_and_logs_failure(self):
        self.atem.get_audio_state.return_value = {
            'connected': True,
            'sources': [{'id': '7', 'volume': -12.0, 'muted': False}],
        }

        response = webui.app.test_client().post('/api/atem/audio/action', json={
            'source_id': 'missing', 'operation': 'mute',
        })

        self.assertEqual(response.status_code, 502, response.get_json())
        self.atem.set_mute.assert_not_called()
        webui.log_event.assert_called_once()
        self.assertEqual(webui.log_event.call_args.args[0], 'atem.audio.action')
        self.assertEqual(webui.log_event.call_args.kwargs['status'], 'failure')

    def test_manual_volume_change_cancels_active_fade(self):
        cancelled = threading.Event()
        with webui._atem_audio_fade_lock:
            webui._atem_audio_fades['7'] = (13, cancelled)

        response = webui.app.test_client().post('/api/atem/audio/volume', json={'source_id': '7', 'db': -6})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(cancelled.is_set())
        self.atem.set_volume.assert_called_once_with('7', -6.0)

    def test_fade_uses_thirty_steps_per_second(self):
        with webui._atem_audio_fade_lock:
            webui._atem_audio_fades['7'] = (12, threading.Event())

        webui._run_atem_audio_fade(self.atem, '7', -12.0, -18.0, 0.1, 12)

        self.assertEqual(self.atem.set_volume.call_count, 3)
        self.assertAlmostEqual(self.atem.set_volume.call_args_list[-1].args[1], -18.0)

    def test_cancelled_fade_is_logged_and_removed(self):
        cancelled = threading.Event()
        cancelled.set()
        with webui._atem_audio_fade_lock:
            webui._atem_audio_fades['7'] = (11, cancelled)
        webui.log_event.reset_mock()

        webui._run_atem_audio_fade(self.atem, '7', -12.0, -18.5, 5, 11)

        self.assertFalse(webui._atem_audio_fades)
        webui.log_event.assert_called_once()
        self.assertEqual(webui.log_event.call_args.args[0], 'atem.audio.fade.cancelled')
        self.assertEqual(webui.log_event.call_args.kwargs['status'], 'info')

    def test_cli_json_round_trip_preserves_atem_audio(self):
        from cli import _trigger_from_spec
        action = {'source_id': '7', 'operation': 'volume', 'db': -18.5, 'fade_seconds': 5}
        value = {'minutes': 0, 'typeOfTrigger': 'AT', 'actionType': 'atem_audio', 'atem_audio': action}

        self.assertEqual(_trigger_from_spec(__import__('json').dumps(value)).to_dict()['atem_audio'], action)

    def test_cli_display_preserves_atem_audio_payload(self):
        from cli import _trigger_display_dict
        action = {'source_id': '7', 'operation': 'mute'}
        trigger = TimeOfTrigger(0, TypeofTime.AT, actionType='atem_audio', atem_audio=action)

        self.assertEqual(_trigger_display_dict(trigger)['atem_audio'], action)

    def test_event_create_persists_atem_audio_action(self):
        action = {'source_id': '7', 'operation': 'volume', 'db': -18.5, 'fade_seconds': 5}
        response = webui.app.test_client().post('/api/ui/events', json={
            'name': 'Service', 'date': '2026-09-20', 'time': '10:00', 'day': 'Sunday',
            'repeating': False, 'active': True,
            'times': [{'minutes': 0, 'typeOfTrigger': 'AT', 'actionType': 'atem_audio', 'atem_audio': action}],
        })

        self.assertEqual(response.status_code, 200, response.get_json())
        loaded = storage.load_events_safe(self.config['EVENTS_FILE'])[0].times[0]
        self.assertEqual(loaded.to_dict()['atem_audio'], action)

    def test_volume_action_round_trips_and_executes(self):
        action = {'source_id': '7', 'operation': 'volume', 'db': -18.5, 'fade_seconds': 0}
        trigger = TimeOfTrigger(60, TypeofTime.BEFORE, uid='atem-cue', actionType='atem_audio', atem_audio=action)

        self.assertEqual(trigger.to_dict()['atem_audio'], action)
        self.assertTrue(self.scheduler._handle_trigger(self._job(trigger)))
        self.atem.set_volume.assert_called_once_with('7', -18.5)

        path = str(self.root / 'events.json')
        storage.save_events([self._job(trigger).event], path)
        loaded = storage.load_events_safe(path)[0].times[0]
        self.assertEqual(loaded.to_dict()['atem_audio'], action)


if __name__ == '__main__':
    unittest.main()
