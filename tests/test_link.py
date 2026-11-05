import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
import unittest
from maestro_bridge.link import LinkManager, profile_path, read_profile, effective_drive, root_folder, destination_folder, save_client_path, validate_client


class FakeFlow:
    redirect_uri = ''
    credentials = MagicMock()
    credentials.to_json.return_value = '{"test_token": true}'

    @classmethod
    def from_client_secrets_file(cls, *args, **kwargs):
        return cls()

    def authorization_url(self, **kwargs):
        assert kwargs['prompt'] == 'consent select_account'
        return 'https://accounts.google.com/o/oauth2/auth?state=test-state', 'test-state'

    def fetch_token(self, authorization_response):
        assert 'state=test-state' in authorization_response


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root/'config.toml'
        self.config.write_text('[drive]\n')
        self.manager = LinkManager(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_client_does_not_replace_profile(self):
        previous = {'folder_id': 'previous', 'token_file': 'previous-token'}
        profile_path(self.config).write_text(json.dumps(previous))
        with self.assertRaisesRegex(ValueError, 'OAuth'):
            self.manager.start()
        self.assertEqual(read_profile(self.config), previous)

    def test_client_configuration_validation_and_persistence(self):
        path = self.root/'client.json'
        path.write_text(json.dumps({'installed': {'client_id':'test', 'client_secret':'test',
            'auth_uri':'https://accounts.google.com/o/oauth2/auth',
            'token_uri':'https://oauth2.googleapis.com/token'}}))
        self.assertTrue(self.manager.status()['setup_error'])
        self.assertEqual(save_client_path(self.config, str(path)), str(path.resolve()))
        self.assertEqual(self.manager.status()['setup_error'], '')
        previous = self.config.read_text()
        path.write_text(json.dumps({'web': {'client_id':'test'}}))
        with self.assertRaisesRegex(ValueError, 'Aplicativo para computador'):
            save_client_path(self.config, str(path))
        self.assertEqual(self.config.read_text(), previous)

    def test_untrusted_oauth_endpoint_rejected(self):
        path = self.root/'client.json'
        path.write_text(json.dumps({'installed': {'client_id':'test', 'client_secret':'test',
            'auth_uri':'https://example.com/auth', 'token_uri':'https://oauth2.googleapis.com/token'}}))
        with self.assertRaisesRegex(ValueError, 'oficiais'):
            validate_client(path)

    def test_activation_and_effective_config(self):
        service = MagicMock()
        service.about.return_value.get.return_value.execute.return_value = {'user': {'emailAddress':'test@example.org','permissionId':'account1'}}
        service.files.return_value.list.return_value.execute.return_value = {'files': [{'id':'root1'}]}
        with patch('maestro_bridge.link.service_from_token', return_value=service):
            self.manager._activate(self.root/'token.json')
        profile = read_profile(self.config)
        self.assertEqual(profile['folder_id'], 'root1')
        self.assertEqual(profile['destination_name'], 'dados_opd')
        config = {'_path': self.config, 'drive': {'enabled': False, 'folder_id': 'old'}}
        selected = effective_drive(config)
        self.assertEqual(selected['folder_id'], 'root1')
        self.assertTrue(selected['enabled'])
        self.assertEqual(config['drive']['folder_id'], 'old')

    def test_root_creation(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {'files': []}
        service.files.return_value.create.return_value.execute.return_value = {'id':'new-root'}
        self.assertEqual(root_folder(service), 'new-root')
        body = service.files.return_value.create.call_args.kwargs['body']
        self.assertEqual(body['name'], 'dados_opd')
        self.assertEqual(body['parents'], ['root'])

    def test_destination_folder_keeps_selected_dados_opd(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            'id': 'root1', 'name': 'dados_opd', 'mimeType': 'application/vnd.google-apps.folder',
            'trashed': False, 'capabilities': {'canAddChildren': True}}
        self.assertEqual(destination_folder(service, 'root1'), 'root1')
        service.files.return_value.list.assert_not_called()

    def test_destination_folder_resolves_child_named_dados_opd(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            'id': 'parent1', 'name': 'Compartilhado', 'mimeType': 'application/vnd.google-apps.folder',
            'trashed': False, 'capabilities': {'canAddChildren': True}}
        service.files.return_value.list.return_value.execute.return_value = {'files': [{
            'id': 'root1', 'name': 'dados_opd', 'mimeType': 'application/vnd.google-apps.folder',
            'trashed': False, 'capabilities': {'canAddChildren': True}}]}
        self.assertEqual(destination_folder(service, 'parent1'), 'root1')

    def test_destination_folder_creates_root_inside_selected_parent(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            'id': 'parent1', 'name': 'Compartilhado', 'mimeType': 'application/vnd.google-apps.folder',
            'trashed': False, 'capabilities': {'canAddChildren': True}}
        service.files.return_value.list.return_value.execute.return_value = {'files': []}
        service.files.return_value.create.return_value.execute.return_value = {'id': 'root1'}
        self.assertEqual(destination_folder(service, 'parent1'), 'root1')
        body = service.files.return_value.create.call_args.kwargs['body']
        self.assertEqual(body['name'], 'dados_opd')
        self.assertEqual(body['parents'], ['parent1'])

    def _callback_flow(self, denied):
        instance = FakeFlow()
        factory = MagicMock()
        factory.from_client_secrets_file.return_value = instance
        with patch.object(self.manager, '_activate') as activate:
            thread = threading.Thread(target=self.manager._authorize, args=(self.root/'client.json', factory))
            thread.start()
            deadline = time.monotonic()+5
            while self.manager.phase != 'waiting' and thread.is_alive() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(self.manager.phase, 'waiting')
            url = urlparse(instance.redirect_uri)
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
            try:
                conn.request('GET', '/?state=wrong&code=test')
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                conn.request('GET', '/?state=test-state&'+('error=access_denied' if denied else 'code=test'))
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                conn.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(activate.call_count, 0 if denied else 1)
            self.assertEqual(self.manager.phase, 'error' if denied else 'complete')
            self.assertIsNone(self.manager.auth_url)

    def test_oauth_callback_validates_state_then_activates(self):
        self._callback_flow(False)

    def test_oauth_denial_does_not_activate(self):
        self._callback_flow(True)
        self.assertEqual(list(self.root.glob('drive-token-*.json')), [])
