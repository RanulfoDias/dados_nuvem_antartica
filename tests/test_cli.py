from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from maestro_bridge.cli import main, read_config


class CLITests(unittest.TestCase):
    def test_validate(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['validate', 'tests/fixtures/synthetic_zero.SPE']), 0)
        self.assertIn('"total_counts": 0', output.getvalue())

    def test_self_test(self):
        with redirect_stdout(io.StringIO()), self.assertLogs('maestro_bridge.sync', level='ERROR'):
            self.assertEqual(main(['offline-test']), 0)

    def test_config_relative_paths_and_invalid_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.toml'
            path.write_text('[bridge]\nwatch_directory="incoming"\n')
            self.assertEqual(read_config(path)['bridge']['watch_directory'], Path(directory).resolve()/'incoming')
            path.write_text('[bridge]\npoll_seconds=0\n')
            with self.assertRaises(ValueError):
                read_config(path)

    def test_missing_input(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            main(['validate', 'missing.SPE'])
        self.assertEqual(exc.exception.code, 2)
