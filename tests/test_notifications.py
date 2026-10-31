import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from maestro_bridge.local import Queue
from maestro_bridge.notifications import (save_recipient, save_smtp, read_settings,
    public_status, deliver_pending, send_email, settings_path)
from maestro_bridge.science import parse_spe
from maestro_bridge.sync import MemoryBackend, sync_once
from maestro_bridge.ui import start_server
from tests.helpers import spe


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root/'config.toml'
        self.config.write_text('[bridge]\n[drive]\n')
        self.database = self.root/'queue.db'
        self.queue = Queue(self.database)
        self.smtp = dict(host='smtp.example.org', port=465, security='ssl',
                         sender='sender@example.org', username='sender@example.org', password='private-secret')
        save_recipient(self.config, 'observer@example.org')
        save_smtp(self.config, self.smtp)
        raw = spe()
        self.queue.put(parse_spe(raw), raw, Path('test.spe'))
        self.backend = MemoryBackend()

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def complete_backup(self):
        return sync_once(self.queue, self.backend, now=10, notification_recipient='observer@example.org')

    def test_partial_backup_does_not_notify_then_success_enqueues_once(self):
        self.backend.fail_commit = True
        with self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(self.complete_backup(), 0)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM email_outbox').fetchone()[0], 0)
        self.backend.fail_commit = False
        self.assertEqual(sync_once(self.queue, self.backend, now=15, notification_recipient='observer@example.org'), 1)
        self.assertEqual(sync_once(self.queue, self.backend, now=16, notification_recipient='observer@example.org'), 0)
        row = self.queue.db.execute('SELECT * FROM email_outbox').fetchone()
        self.assertEqual(json.loads(row['payload'])['count'], 1)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM email_outbox').fetchone()[0], 1)

    def test_retry_survives_restart_without_reuploading(self):
        self.complete_backup()
        failed = MagicMock(side_effect=OSError('secret provider error'))
        self.assertEqual(deliver_pending(self.queue, self.config, now=10, send=failed), 0)
        self.assertEqual(self.queue.status()['states'], {'synced': 1})
        self.queue.close()
        self.queue = Queue(self.database)
        sent = MagicMock()
        self.assertEqual(deliver_pending(self.queue, self.config, now=39, send=sent), 0)
        self.assertEqual(deliver_pending(self.queue, self.config, now=40, send=sent), 1)
        self.assertEqual(deliver_pending(self.queue, self.config, now=41, send=sent), 0)
        sent.assert_called_once()
        self.assertEqual(public_status(self.config, self.database)['last_sent'], 40)

    def test_disabled_or_changed_recipient_cancels_old_notifications(self):
        self.complete_backup()
        save_recipient(self.config, '')
        sent = MagicMock()
        deliver_pending(self.queue, self.config, now=11, send=sent)
        sent.assert_not_called()
        self.assertEqual(self.queue.db.execute('SELECT state FROM email_outbox').fetchone()[0], 'cancelled')

    def test_stale_lease_cannot_enqueue_notification(self):
        old_rows = self.queue.claim(now=0, lease_seconds=1)
        self.queue.claim(now=2)
        self.assertEqual(self.queue.complete(old_rows, notification_recipient='observer@example.org'), 0)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM email_outbox').fetchone()[0], 0)

    def test_settings_validate_and_preserve_password_without_disclosing_it(self):
        save_smtp(self.config, dict(self.smtp, password=''))
        self.assertEqual(read_settings(self.config)['smtp']['password'], 'private-secret')
        self.assertNotIn('private-secret', json.dumps(public_status(self.config, self.database)))
        for recipient in ('a@example.org\r\nBcc: x@example.org', 'a@example.org,b@example.org', 'invalid'):
            with self.assertRaises(ValueError):
                save_recipient(self.config, recipient)
        with self.assertRaises(ValueError):
            save_smtp(self.config, dict(self.smtp, host='other.example.org', password=''))
        with self.assertRaises(ValueError):
            save_smtp(self.config, dict(self.smtp, security='none'))
        self.assertEqual(read_settings(self.config)['recipient'], 'observer@example.org')
        if __import__('os').name != 'nt':
            self.assertEqual(settings_path(self.config).stat().st_mode & 0o777, 0o600)

    def test_tls_and_message_summary_without_spectra(self):
        self.complete_backup()
        row = self.queue.db.execute('SELECT * FROM email_outbox').fetchone()
        for mode, factory in [('ssl', 'SMTP_SSL'), ('starttls', 'SMTP')]:
            with patch('maestro_bridge.notifications.smtplib.'+factory) as mocked:
                client = mocked.return_value.__enter__.return_value
                client.send_message.return_value = {}
                send_email(dict(self.smtp, security=mode), row)
                if mode == 'starttls':
                    client.starttls.assert_called_once()
                client.login.assert_called_once_with('sender@example.org', 'private-secret')
                message = client.send_message.call_args.args[0]
                self.assertEqual(message['To'], 'observer@example.org')
                self.assertIn('backup no Drive concluído', str(message['Subject']))
                self.assertEqual(len(list(message.iter_attachments())), 0)

    def test_http_settings_require_csrf_and_never_return_password(self):
        server, thread = start_server(self.database, 0, config_path=self.config)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request('GET', '/api/status')
            state = json.loads(connection.getresponse().read())
            self.assertNotIn('private-secret', json.dumps(state))
            headers = {'Content-Type':'application/json', 'Origin':f'http://127.0.0.1:{server.server_port}'}
            connection.request('POST', '/api/notifications', json.dumps({'recipient':'new@example.org'}), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            headers['X-CSRF-Token'] = state['csrf']
            connection.request('POST', '/api/notifications', json.dumps({'recipient':'new@example.org'}), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertEqual(read_settings(self.config)['recipient'], 'new@example.org')
            connection.request('POST', '/api/notifications/smtp', json.dumps(dict(self.smtp, password='')), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(b'private-secret', response.read())
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()
