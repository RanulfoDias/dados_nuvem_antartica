import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from maestro_bridge.drive import DriveBackend
from maestro_bridge.local import Queue
from maestro_bridge.science import parse_spe
from maestro_bridge.sync import sync_once
from tests.helpers import spe


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self):
        return self.action()


class Conflict(Exception):
    resp = SimpleNamespace(status=409)


class FakeDrive:
    def __init__(self):
        self.objects = {}
        self.generated = 0
        self.lose_response = False
        self.fail_json = False
        self.online = True

    def files(self):
        return self

    def close(self):
        pass

    def generateIds(self, **kwargs):
        def execute():
            self.generated += 1
            return {'ids': [f'id-{self.generated}']}
        return Request(execute)

    def get(self, fileId, **kwargs):
        def execute():
            if fileId.startswith('folder'):
                return {'name': 'dados_do_espectrometro', 'mimeType': 'application/vnd.google-apps.folder',
                        'capabilities': {'canAddChildren': True}}
            body, raw = self.objects[fileId]
            return dict(body, size=str(len(raw)), sha256Checksum=hashlib.sha256(raw).hexdigest())
        return Request(execute)

    def create(self, body, media_body=b'', **kwargs):
        def execute():
            if not self.online or (self.fail_json and body['name'].endswith('.json')):
                raise ConnectionError('offline')
            if body['id'] in self.objects:
                raise Conflict()
            self.objects[body['id']] = (body, media_body)
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError('Upload accepted, response lost')
            return {'id': body['id']}
        return Request(execute)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'
        self.queue = Queue(self.path)
        self.service = FakeDrive()
        raw = spe(counts=(1, 2, 3, 4))
        self.queue.put(parse_spe(raw), raw, Path('original.SPE'), modified_date='2026-10-26')

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def backend(self, folder='folder1'):
        return DriveBackend(folder_id=folder, token_file='unused', database=self.path,
                            service=self.service, media_factory=lambda stream, **kw: stream.read())

    def test_original_spe_only_in_modification_day_folder(self):
        self.assertEqual(sync_once(self.queue, self.backend(), now=0), 1)
        names = {body['name'] for body, _ in self.service.objects.values()}
        self.assertEqual(names, {'original.SPE', '26_10_2026'})
        raw = next(raw for body, raw in self.service.objects.values() if body['name'] == 'original.SPE')
        self.assertEqual(raw, spe(counts=(1, 2, 3, 4)))
        self.assertEqual(sync_once(self.queue, self.backend(), now=5), 0)

    def test_windows_original_filename(self):
        raw = spe(detector=7)
        self.queue.put(parse_spe(raw), raw, Path(r'C:\MAESTRO\Detector 7.SPE'), modified_date='2026-10-26')
        self.assertEqual(sync_once(self.queue, self.backend(), now=0), 2)
        names = {body['name'] for body, _ in self.service.objects.values()}
        self.assertIn('Detector 7.SPE', names)
        self.assertFalse(any(name.endswith('.json') for name in names))

    def test_six_detectors_share_timestamp_folder_without_overwrite(self):
        for detector in (1, 2, 3, 4, 5, 7):
            raw = spe(detector=detector)
            self.queue.put(parse_spe(raw), raw, Path(f'{detector}.SPE'), modified_date='2026-10-26')
        self.assertEqual(sync_once(self.queue, self.backend(), now=0), 7)
        folders = [body for body, raw in self.service.objects.values() if body.get('mimeType') == 'application/vnd.google-apps.folder']
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]['name'], '26_10_2026')
        self.assertEqual(len(self.service.objects), 8)

    def test_response_lost_and_restart_without_duplicates(self):
        self.service.lose_response = True
        with self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(sync_once(self.queue, self.backend(), now=0), 0)
        self.queue.close()
        self.queue = Queue(self.path)
        self.assertEqual(sync_once(self.queue, self.backend(), now=5), 1)
        self.assertEqual(len(self.service.objects), 2)
        self.assertEqual(self.service.generated, 2)

    def test_remote_corruption_is_rejected(self):
        backend = self.backend()
        row = self.queue.db.execute('SELECT * FROM spectra').fetchone()
        backend.upload(row['sha256'], row['raw'])
        key = next(k for k,(b, _) in self.service.objects.items() if b['name'].lower().endswith('.spe'))
        body, raw = self.service.objects[key]
        self.service.objects[key] = (body, b'corrupt')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            backend.upload(row['sha256'], row['raw'])

    def test_selected_date_excludes_other_pending_files(self):
        backend = self.backend()
        self.assertEqual(sync_once(self.queue, backend, now=0, selected_date='2026-10-25'), 0)
        self.assertEqual(sync_once(self.queue, backend, now=0, selected_date=''), 0)
        self.assertEqual(sync_once(self.queue, backend, now=0, selected_date='2026-10-26'), 1)
        self.assertEqual(sync_once(self.queue, backend, now=5, selected_date='2026-10-26'), 0)

    def test_destination_switch_preserves_delivery_history(self):
        first = self.backend()
        self.assertEqual(sync_once(self.queue, first, now=0), 1)
        second = self.backend('folder2')
        self.assertEqual(self.queue.status()['states'], {'pending': 1})
        with self.assertRaisesRegex(ValueError, 'destination changed'):
            sync_once(self.queue, first, now=5)
        self.assertEqual(sync_once(self.queue, second, now=5), 1)
        self.assertEqual(len(self.service.objects), 4)
        restored = self.backend('folder1')
        self.assertEqual(sync_once(self.queue, restored, now=6), 0)
        self.assertEqual(len(self.service.objects), 4)

    def test_destination_switch_refuses_active_upload(self):
        self.backend()
        self.queue.claim()  # unexpired lease
        with self.assertRaisesRegex(ValueError, 'active uploads'):
            self.backend('folder2')

    def test_firebase_acknowledgments_migrated_once(self):
        self.queue.complete(self.queue.claim(now=0), now=0)
        self.assertEqual(self.queue.status()['states'], {'synced': 1})
        backend = self.backend()
        self.assertEqual(self.queue.status()['states'], {'pending': 1})
        self.assertEqual(sync_once(self.queue, backend, now=1), 1)
        self.backend()
        self.assertEqual(self.queue.status()['states'], {'synced': 1})
