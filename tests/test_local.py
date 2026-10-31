from pathlib import Path
import tempfile
import unittest
from maestro_bridge.local import Monitor, Queue
from maestro_bridge.science import parse_spe
from tests.helpers import spe


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root/'queue.sqlite3')
        self.monitor = Monitor(self.root, self.queue, stable_seconds=2)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def test_six_stable_duplicates_and_overwrite(self):
        for detector in (1, 2, 3, 4, 5, 7):
            (self.root/f'{detector}.SPE').write_bytes(spe(detector))
        self.assertEqual(self.monitor.scan(0), 0)
        self.assertEqual(self.monitor.scan(1), 0)
        self.assertEqual(self.monitor.scan(2), 6)
        (self.root/'copy.spe').write_bytes(spe())
        self.monitor.scan(3)
        self.assertEqual(self.monitor.scan(5), 0)
        (self.root/'1.SPE').write_bytes(spe(counts=(1,2,3,4)))
        self.monitor.scan(6)
        self.assertEqual(self.monitor.scan(8), 1)
        self.assertEqual(self.queue.status()['total'], 7)

    def test_incomplete_then_finished(self):
        path = self.root/'test.SPE'
        path.write_bytes(spe().split(b'$DATA:')[0])
        self.monitor.scan(0)
        with self.assertLogs('maestro_bridge.local', level='ERROR'):
            self.assertEqual(self.monitor.scan(2), 0)
        path.write_bytes(spe())
        self.monitor.scan(3)
        self.assertEqual(self.monitor.scan(5), 1)

    def test_restart_lease_retry_and_exact_bytes(self):
        self.queue.put(parse_spe(spe()), spe(), self.root/'test.SPE')
        rows = self.queue.claim(now=10, lease_seconds=10)
        self.assertEqual(rows[0]['raw'], spe())
        self.queue.close()
        self.queue = Queue(self.root/'queue.sqlite3')
        self.assertEqual(self.queue.claim(now=11), [])
        recovered = self.queue.claim(now=21)
        self.assertEqual(len(recovered), 1)
        self.queue.complete(rows, now=22)  # expired worker cannot acknowledge a new lease
        self.assertEqual(self.queue.status()['states'], {'sending': 1})
        self.queue.fail(recovered, 'offline', now=22)
        self.assertEqual(self.queue.claim(now=26), [])
        recovered = self.queue.claim(now=27)
        self.assertEqual(recovered[0]['attempts'], 1)
        self.queue.complete(recovered, now=28)
        self.assertEqual(self.queue.status()['states'], {'synced': 1})
