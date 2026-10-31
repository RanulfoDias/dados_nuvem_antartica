from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import MagicMock
import unittest
from maestro_bridge.cli import read_config
from maestro_bridge.telemetry import document, reported_state, FirebaseStatus, Reporter
from maestro_bridge.ui import snapshot


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'config.toml'
        self.path.write_text('[bridge]\n[firebase_status]\nenabled=true\n')
        self.config = read_config(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_allowlist_excludes_credentials_and_scientific_data(self):
        self.config['drive']['token_file'] = 'SECRET_TOKEN'
        self.config['firebase_status']['credentials_file'] = 'SECRET_KEY'
        status = snapshot(self.config['bridge']['database'])
        status.update(counts=[9876], credentials='SECRET_PAYLOAD')
        payload = document(self.config, status)
        self.assertNotIn('SECRET', str(payload))
        self.assertNotIn('counts', payload)
        self.assertEqual(payload['monitor'], 'stopped')
        self.assertEqual(payload['stale_after_seconds'], 180)

    def test_remote_freshness(self):
        now = datetime.now(timezone.utc)
        payload = {'monitor': 'running', 'stale_after_seconds': 180, 'updated_at': now}
        self.assertEqual(reported_state(payload, now), 'running')
        self.assertEqual(reported_state(payload, now+timedelta(seconds=181)), 'no_recent_contact')
        self.assertEqual(reported_state({}), 'unknown')

    def test_single_document_server_timestamp_and_timeout(self):
        backend = FirebaseStatus.__new__(FirebaseStatus)
        backend.ref = MagicMock()
        backend.timestamp = object()
        backend.publish({'monitor': 'running'})
        backend.ref.set.assert_called_once_with({'monitor': 'running', 'updated_at': backend.timestamp}, retry=None, timeout=15)

    def test_credentials_required(self):
        with self.assertRaisesRegex(ValueError, 'credentials'):
            FirebaseStatus(self.config)

    def test_reporter_recovers_and_closes(self):
        remote = MagicMock()
        reporter = Reporter(self.config, factory=lambda _: remote)
        self.config['firebase_status']['heartbeat_seconds'] = .001
        def publish(payload):
            if remote.publish.call_count == 1:
                raise ConnectionError('offline')
            reporter.stop_event.set()
        remote.publish.side_effect = publish
        with self.assertLogs('maestro_bridge.telemetry', level='ERROR'):
            reporter.start()
            reporter.thread.join(timeout=3)
        self.assertFalse(reporter.thread.is_alive())
        self.assertEqual(remote.publish.call_count, 2)
        remote.close.assert_called_once()
