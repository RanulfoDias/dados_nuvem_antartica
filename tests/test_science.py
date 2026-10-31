import hashlib
from pathlib import Path
import unittest
from maestro_bridge.science import parse_spe, SpectrumError, DETECTORS
from tests.helpers import spe


class ScienceTests(unittest.TestCase):
    def test_synthetic_zero_fixture(self):
        raw = Path('tests/fixtures/synthetic_zero.SPE').read_bytes()
        s = parse_spe(raw)
        self.assertEqual(s.total_counts, 0)
        self.assertEqual(s.counts, [0]*4)
        self.assertEqual(s.live_rate_sigma_cps, 0)
        self.assertEqual(s.sha256, hashlib.sha256(raw).hexdigest())
        for name in ('ROI', 'ENER_FIT', 'MCA_CAL', 'SHAPE_CAL'):
            self.assertIn(name, s.sections)

    def test_positive_durations_and_detectors(self):
        for duration in (600, 900, 1800):
            for detector in DETECTORS:
                with self.subTest(duration=duration, detector=detector):
                    s = parse_spe(spe(detector, duration, duration+10, (1, 4, 9, 2)), acquisition_timezone='America/Sao_Paulo')
                    self.assertEqual(s.total_counts, 16)
                    self.assertEqual(s.count_sigma, 4)
                    self.assertEqual(s.live_rate_cps, 16/duration)
                    self.assertEqual(s.live_rate_sigma_cps, 4/duration)
                    self.assertEqual(s.real_rate_cps, 16/(duration+10))
                    self.assertEqual(s.acquired_at, '2026-10-14T15:30:00+00:00')
                    self.assertEqual(s.detector_id, detector)

    def test_invalid(self):
        cases = [b'', b'\x00', b'\x81', spe().split(b'$SHAPE_CAL:')[0]+b'$SHAPE_CAL:\n', spe(6), spe(live=0), spe(live=901, real=900),
                 spe(live='nan'), spe().replace(b'0 3\n0\n', b'0 4\n0\n'),
                 spe(counts=(1, -1, 0, 0)), spe()+b'$DATA:\n0 0\n1\n',
                 spe().replace(b'$ROI:\n1\n0 3', b'$ROI:\n1\n0 4')]
        for raw in cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(SpectrumError):
                parse_spe(raw)
        with self.assertRaises(SpectrumError):
            parse_spe(spe(), detector_hint=2)

    def test_dst(self):
        for date in (b'03/08/2026 02:30:00', b'11/01/2026 01:30:00'):
            with self.assertRaises(SpectrumError):
                parse_spe(spe().replace(b'10/14/2026 12:30:00', date), acquisition_timezone='America/New_York')
