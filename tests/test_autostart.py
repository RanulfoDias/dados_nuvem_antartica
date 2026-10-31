from contextlib import nullcontext
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from maestro_bridge import autostart


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = self.root/'station config.toml'
        self.config.write_text('[bridge]\nwatch_directory="."\n[drive]\nenabled=false\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_macos_registration_and_disable_without_launching(self):
        with patch.object(autostart, 'sys', SimpleNamespace(platform='darwin', executable=sys.executable)), patch.object(Path, 'home', return_value=self.root):
            self.assertFalse(autostart.status(self.config)['enabled'])
            result = autostart.configure(self.config, True)
            self.assertTrue(result['enabled'])
            content = plistlib.loads(autostart.registration_path(self.config).read_bytes())
            self.assertEqual(content['ProgramArguments'], autostart.command(self.config, True))
            self.assertTrue(content['RunAtLoad'])
            self.assertNotIn('KeepAlive', content)
            self.assertFalse(autostart.configure(self.config, False)['enabled'])

    def test_linux_xdg_registration_with_spaces_and_disable(self):
        with patch.object(autostart, 'sys', SimpleNamespace(platform='linux', executable=sys.executable)), patch.dict(os.environ, {'XDG_CONFIG_HOME':str(self.root/'desktop config')}):
            self.assertTrue(autostart.configure(self.config, True)['enabled'])
            path = autostart.registration_path(self.config)
            content = path.read_text()
            self.assertIn('"'+str(self.config)+'"', content)
            self.assertIn('"--offline"', content)
            self.assertIn('Terminal=false', content)
            self.assertFalse(autostart.configure(self.config, False)['enabled'])
            self.assertFalse(path.exists())
            self.assertIn('%%', autostart.desktop_quote('x%y'))

    def test_windows_current_user_registration(self):
        values = {}
        def query(key, name):
            if name not in values:
                raise FileNotFoundError()
            return values[name], 1
        registry = SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1,
            OpenKey=lambda *a: nullcontext(1), CreateKey=lambda *a: nullcontext(1),
            QueryValueEx=query, SetValueEx=lambda key,name,reserved,kind,value: values.update({name:value}),
            DeleteValue=lambda key,name: values.pop(name))
        with patch.object(autostart, 'sys', SimpleNamespace(platform='win32', executable=sys.executable)), patch.dict(sys.modules, {'winreg':registry}):
            result = autostart.configure(self.config, True)
            self.assertTrue(result['enabled'])
            self.assertTrue(result['offline'])
            self.assertEqual(values[autostart.identity(self.config)], subprocess.list2cmdline(autostart.command(self.config, True)))
            self.assertFalse(autostart.configure(self.config, False)['enabled'])

    def test_online_requires_configured_drive_and_valid_booleans(self):
        with self.assertRaises(ValueError):
            autostart.configure(self.config, True, False)
        with self.assertRaises(ValueError):
            autostart.configure(self.config, 'true', True)

    def test_single_instance_released_after_exit(self):
        path = self.root/'queue.db'
        with autostart.single_instance(path):
            with self.assertRaises(ValueError):
                with autostart.single_instance(path):
                    self.fail('duplicate instance acquired lock')
        with autostart.single_instance(path):
            pass

    def test_runner_works_from_another_directory(self):
        runner = Path(autostart.__file__).with_name('autostart_runner.py')
        result = subprocess.run([sys.executable, str(runner), '--help'], cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('maestro-bridge', result.stdout)
