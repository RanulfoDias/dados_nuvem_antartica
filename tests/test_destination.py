import http.client
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from maestro_bridge.destination import folder_id, save_destination
from maestro_bridge.ui import start_server


class DestinationTests(unittest.TestCase):
    def test_links_and_ids(self):
        for value in ('folder_A', 'https://drive.google.com/drive/folders/folder_A?usp=drive_link',
                      'https://drive.google.com/drive/u/0/folders/folder_A'):
            self.assertEqual(folder_id(value), 'folder_A')
        for value in ('', None, 'https://evil.example/drive/folders/test', '../secret', 'a\nb',
                      'https://drive.google.com/file/d/abc/view'):
            with self.assertRaises(ValueError):
                folder_id(value)

    def test_post_persists_only_folder_and_requires_csrf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'config.toml'
            config.write_text("[drive]\nfolder_id = 'old'\nenabled = false\ntoken_file = 'private.json'\n[ui]\nport = 8765\n")
            server, thread = start_server(root/'db', port=0, config_path=config)
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request('GET', '/api/status')
                state = json.loads(connection.getresponse().read())
                self.assertEqual(state['folder_id'], 'old')
                self.assertNotIn('private.json', json.dumps(state))
                body = json.dumps({'folder': 'https://drive.google.com/drive/folders/new'})
                headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{server.server_port}'}
                connection.request('POST', '/api/destination', body, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                headers['X-CSRF-Token'] = state['csrf']
                connection.request('POST', '/api/destination', body, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)  # account changes now require OAuth
                response.read()
                connection.request('POST', '/api/drive/link', '{}', headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn('OAuth', json.loads(response.read())['error'])
                self.assertEqual(tomllib.loads(config.read_text())['drive']['folder_id'], 'old')
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_other_config_sections_and_comments_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.toml'
            path.write_text('# comment\n[drive]\nenabled=false\n[bridge]\npoll_seconds=1\n')
            save_destination(path, 'folder')
            self.assertTrue(path.read_text().startswith('# comment'))
            self.assertEqual(tomllib.loads(path.read_text())['bridge']['poll_seconds'], 1)


class DirectoryBrowserTests(unittest.TestCase):
    def test_lists_only_folders_and_validates_path(self):
        from maestro_bridge.destination import browse_directories
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'pasta com espaços').mkdir()
            (root/'spectrum.spe').write_text('test')
            result = browse_directories(str(root))
            self.assertEqual(result['directories'], [{'name': 'pasta com espaços', 'path': str(root/'pasta com espaços')}])
            self.assertEqual(result['parent'], str(root.parent))
            for value in (42, 'relative', str(root/'missing'), str(root/'spectrum.spe')):
                with self.assertRaises(ValueError):
                    browse_directories(value)
