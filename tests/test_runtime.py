from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from maestro_bridge.ui import snapshot
from tests.helpers import spe


@unittest.skipIf(os.name == 'nt', 'POSIX signal lifecycle test; HTTP tests also run on Windows')
class RuntimeTests(unittest.TestCase):
    def test_offline_monitor_and_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root/'incoming'
            incoming.mkdir()
            for detector in (1, 2, 3, 4, 5, 7):
                (incoming/f'{detector}.spe').write_bytes(spe(detector))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            config = root/'config.toml'
            config.write_text('[bridge]\nwatch_directory="incoming"\ndatabase="queue.db"\n'
                              'log_file="bridge.log"\nstable_seconds=0.1\npoll_seconds=0.05\n'
                              f'upload_date="{datetime.now(timezone.utc).date().isoformat()}"\n'
                              f'[ui]\nport={port}\n')
            process = subprocess.Popen([sys.executable, '-m', 'maestro_bridge', '--config', str(config),
                                        'run', '--offline'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+10
                status = {}
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/api/status', timeout=1) as response:
                            status = json.load(response)
                        if status['total'] == 6:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                self.assertEqual(status.get('total'), 6)
                self.assertEqual(status['monitor'], 'running')
                self.assertEqual(status['drive'], 'disabled')
                alternate = root/'nova pasta'
                alternate.mkdir()
                (alternate/'new.spe').write_bytes(spe(1, counts=(5, 6, 7, 8)))
                endpoint = f'http://127.0.0.1:{port}/api/watch-directory'
                headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{port}'}
                body = json.dumps({'path': str(alternate)}).encode()
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(endpoint, data=body, headers=headers), timeout=2)
                self.assertEqual(denied.exception.code, 403)
                headers['X-CSRF-Token'] = status['csrf']
                with urlopen(Request(endpoint, data=body, headers=headers), timeout=2) as response:
                    self.assertEqual(json.load(response)['watch_directory'], str(alternate.resolve()))
                from maestro_bridge.cli import read_config
                self.assertEqual(read_config(config)['bridge']['watch_directory'], alternate.resolve())
                with urlopen(Request(f'http://127.0.0.1:{port}/api/upload-date', data=json.dumps({'date': status['upload_date']}).encode(), headers=headers), timeout=2) as response:
                    self.assertEqual(json.load(response)['upload_date'], status['upload_date'])
                before = config.read_bytes()
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(endpoint, data=json.dumps({'path': str(root/'missing')}).encode(), headers=headers), timeout=2)
                self.assertEqual(invalid.exception.code, 400)
                self.assertEqual(config.read_bytes(), before)
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    if snapshot(root/'queue.db')['total'] == 7:
                        break
                    time.sleep(.05)
                self.assertEqual(snapshot(root/'queue.db')['total'], 7)

            finally:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            self.assertEqual(process.returncode, 0)
            self.assertEqual(snapshot(root/'queue.db')['monitor'], 'stopped')
