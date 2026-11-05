import http.client
import json
from pathlib import Path
import tempfile
import unittest
from maestro_bridge.local import Queue
from maestro_bridge.science import parse_spe
from maestro_bridge.ui import snapshot, start_server
from tests.helpers import spe


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_database_is_stopped_and_not_created(self):
        status = snapshot(self.path)
        self.assertEqual(status['monitor'], 'stopped')
        self.assertEqual(status['drive'], 'unknown')
        self.assertFalse(self.path.exists())

    def test_heartbeat_stale_offline_and_queue_counts(self):
        queue = Queue(self.path)
        try:
            queue.put(parse_spe(spe()), spe(), Path('a.SPE'))
            queue.runtime('monitor', running=True, at=100, ttl=15, error=False)
            queue.runtime('drive', state='online', at=100, ttl=120)
            status = snapshot(self.path, now=105)
            self.assertEqual((status['monitor'], status['drive']), ('running', 'online'))
            self.assertEqual(status['states'], {'pending': 1})
            status = snapshot(self.path, now=116)
            self.assertEqual((status['monitor'], status['drive']), ('stale', 'unknown'))
            queue.runtime('drive', state='disabled', at=100)
            self.assertEqual(snapshot(self.path, now=110)['drive'], 'disabled')
            queue.runtime('monitor', running=True, at=110, ttl=15, error=True)
            self.assertEqual(snapshot(self.path, now=110)['monitor'], 'error')
            queue.runtime('monitor', running=False, at=111)
            self.assertEqual(snapshot(self.path, now=111)['monitor'], 'stopped')
        finally:
            queue.close()

    def test_http_page_api_and_read_only_routes(self):
        server, thread = start_server(self.path, port=0)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request('GET', '/')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('Observatório do Pico dos Dias'.encode(), response.read())
            connection.request('GET', '/api/status')
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertEqual(json.loads(response.read())['monitor'], 'stopped')
            connection.request('GET', '/config.toml')
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.request('POST', '/api/status')
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request('GET', '/api/status', headers={'Host': 'untrusted.example'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_db_failure_is_not_reported_as_empty_success(self):
        self.path.write_bytes(b'not sqlite')
        server, thread = start_server(self.path, port=0)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request('GET', '/api/status')
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b'not sqlite', response.read())
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()
