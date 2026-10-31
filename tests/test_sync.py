from pathlib import Path
import tempfile
import unittest
from maestro_bridge.local import Queue
from maestro_bridge.science import parse_spe
from maestro_bridge.sync import MemoryBackend, sync_once
from tests.helpers import spe


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'
        self.queue = Queue(self.path)
        for detector in (1, 2, 3, 4, 5, 7):
            raw = spe(detector)
            self.queue.put(parse_spe(raw), raw, Path(f'{detector}.SPE'))
        self.backend = MemoryBackend()

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def test_offline_restart_recover_batch(self):
        self.backend.online = False
        with self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(sync_once(self.queue, self.backend, now=0), 0)
        self.queue.close()
        self.queue = Queue(self.path)
        self.backend.online = True
        self.assertEqual(sync_once(self.queue, self.backend, now=4), 0)
        self.assertEqual(sync_once(self.queue, self.backend, now=5), 6)
        self.assertEqual(sync_once(self.queue, self.backend, now=6), 0)
        self.assertEqual(self.backend.commits, 1)
        self.assertEqual(len(self.backend.objects), 6)
        self.assertEqual(len(self.backend.documents), 6)

    def test_spe_success_json_failure_then_retry(self):
        self.backend.fail_commit = True
        with self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(sync_once(self.queue, self.backend, now=0), 0)
        self.assertEqual(len(self.backend.objects), 6)
        self.assertEqual(len(self.backend.documents), 0)
        self.backend.fail_commit = False
        self.assertEqual(sync_once(self.queue, self.backend, now=5), 6)
        self.assertEqual(len(self.backend.objects), 6)

    def test_corrupted_local_blob_does_not_upload(self):
        with self.queue.db:
            self.queue.db.execute("UPDATE spectra SET raw=X'00'")
        with self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(sync_once(self.queue, self.backend, now=0), 0)
        self.assertEqual(self.backend.objects, {})

    def test_remote_commit_before_local_ack_is_idempotent(self):
        rows = self.queue.claim(now=0, lease_seconds=1)
        from maestro_bridge.sync import metadata
        for row in rows:
            self.backend.upload(row['sha256'], row['raw'])
        self.backend.commit([(row['sha256'], metadata(row)) for row in rows])
        self.assertEqual(sync_once(self.queue, self.backend, now=2), 6)
        self.assertEqual(len(self.backend.documents), 6)

