from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from maestro_bridge.destination import read_upload_date, save_upload_date
from maestro_bridge.local import Monitor, Queue
from tests.helpers import spe


class UploadDateTests(unittest.TestCase):
    def test_config_persists_and_rejects_invalid_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text('[bridge]\npoll_seconds = 1\n')
            self.assertEqual(read_upload_date(config), '')
            save_upload_date(config, '2026-10-26')
            self.assertEqual(read_upload_date(config), '2026-10-26')
            for invalid in ('2026-02-30', '', '20261026', None):
                with self.assertRaises(ValueError):
                    save_upload_date(config, invalid)
            self.assertEqual(read_upload_date(config), '2026-10-26')

    def test_modification_day_uses_local_timezone_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, detector, hour in [('yesterday.spe', 1, 2), ('today.SPE', 2, 3)]:
                path = root/name
                path.write_bytes(spe(detector=detector))
                timestamp = datetime(2026, 10, 26, hour, 30, tzinfo=timezone.utc).timestamp()
                os.utime(path, (timestamp, timestamp))
            queue = Queue(root/'queue.db')
            monitor = Monitor(root, queue, stable_seconds=1, acquisition_timezone='America/Sao_Paulo', selected_date='2026-10-26')
            monitor.scan(0)
            self.assertEqual(monitor.scan(1), 1)
            row = queue.db.execute('SELECT * FROM spectra').fetchone()
            self.assertEqual(Path(row['source_path']).name, 'today.SPE')
            self.assertEqual(row['modified_date'], '2026-10-26')
            queue.complete(queue.claim(now=10, selected_date='2026-10-26'), now=11)
            queue.close()
            queue = Queue(root/'queue.db')
            monitor = Monitor(root, queue, stable_seconds=1, acquisition_timezone='America/Sao_Paulo', selected_date='2026-10-26')
            monitor.scan(0)
            self.assertEqual(monitor.scan(1), 0)
            self.assertEqual(queue.claim(now=20, selected_date='2026-10-26'), [])
            queue.close()
