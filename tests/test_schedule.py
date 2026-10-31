from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from maestro_bridge.schedule import read_schedule, save_schedule, sync_scheduled
from maestro_bridge.local import Queue
from maestro_bridge.science import parse_spe
from maestro_bridge.sync import MemoryBackend
from tests.helpers import spe


class ScheduleTests(unittest.TestCase):
    def test_daily_timezone_gate_and_no_reupload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text('[bridge]\nacquisition_timezone="America/Sao_Paulo"\n')
            queue = Queue(Path(directory)/'queue.db')
            backend = MemoryBackend()
            for day, detector in [('2026-10-26', 1), ('2026-10-27', 2)]:
                raw = spe(detector=detector)
                queue.put(parse_spe(raw), raw, Path(f'{detector}.SPE'), modified_date=day)
            at = lambda day, hour, minute: datetime(2026,10,day,hour,minute,tzinfo=timezone.utc)
            self.assertEqual(sync_scheduled(queue, backend, config, now=at(26,21,0)), 0)
            save_schedule(config, '18:00')
            self.assertEqual(sync_scheduled(queue, backend, config, now=at(26,20,59)), 0)
            self.assertEqual(len(backend.objects), 0)
            self.assertEqual(sync_scheduled(queue, backend, config, now=at(26,21,0)), 1)
            self.assertEqual(sync_scheduled(queue, backend, config, now=at(26,22,0)), 0)
            self.assertEqual(read_schedule(config, now=at(27,2,59))['date'], '2026-10-26')
            self.assertFalse(read_schedule(config, now=at(27,3,0))['due'])
            self.assertEqual(sync_scheduled(queue, backend, config, now=at(27,21,1)), 1)
            self.assertEqual(len(backend.objects), 2)
            queue.close()

    def test_validate_and_persist_time(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text('[bridge]\n')
            save_schedule(config, '23:59')
            for invalid in ('24:00', '18:60', '6:00', '', None):
                with self.assertRaises(ValueError):
                    save_schedule(config, invalid)
            self.assertEqual(read_schedule(config)['time'], '23:59')

    def test_arduino_time_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text('[bridge]\nacquisition_timezone="America/Sao_Paulo"\n')
            save_schedule(config, '18:00')
            now = datetime(2026,10,27,21,30,tzinfo=timezone.utc)
            self.assertFalse(read_schedule(config, now=now, key='arduino_upload_time')['due'])
            save_schedule(config, '19:00', key='arduino_upload_time')
            self.assertTrue(read_schedule(config, now=now)['due'])
            self.assertFalse(read_schedule(config, now=now, key='arduino_upload_time')['due'])
            self.assertEqual(read_schedule(config)['time'], '18:00')
