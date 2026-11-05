"""User-initiated OAuth linking, with atomic activation of a complete account profile."""
import json
import os
from pathlib import Path
import secrets
import threading
import time
import tomllib
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server, WSGIRequestHandler

from .destination import _EDIT_LOCK, _save_setting
from .drive import SCOPES, save_token, service_from_token

ROOT_NAME = 'dados_opd'


def profile_path(config_path):
    return Path(config_path).resolve().with_suffix('.drive-link.json')


def read_profile(config_path):
    path = profile_path(config_path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def effective_drive(config):
    drive = dict(config['drive'])
    profile = read_profile(config['_path'])
    if profile:
        drive.update(folder_id=profile['folder_id'], token_file=profile['token_file'], enabled=True)
    return drive


def root_folder(service):
    query = ("trashed=false and 'root' in parents and mimeType='application/vnd.google-apps.folder' "
             "and name='dados_opd' and appProperties has { key='maestro_root' and value='1' }")
    found = service.files().list(q=query, fields='files(id)', pageSize=100).execute()['files']
    if found:
        return sorted(item['id'] for item in found)[0]
    return service.files().create(body={'name': ROOT_NAME, 'mimeType': 'application/vnd.google-apps.folder',
        'parents': ['root'], 'appProperties': {'maestro_root': '1'}}, fields='id').execute()['id']


def destination_folder(service, folder_id):
    """Resolve a selected Drive folder to the actual ``dados_opd`` upload root."""
    selected = service.files().get(fileId=folder_id,
        fields='id,name,mimeType,trashed,capabilities(canAddChildren)',
        supportsAllDrives=True).execute()
    if (selected.get('mimeType') != 'application/vnd.google-apps.folder'
            or selected.get('trashed')
            or not selected.get('capabilities', {}).get('canAddChildren')):
        raise ValueError('A pasta informada não existe ou a conta Google não pode gravar nela.')
    if selected.get('name') == ROOT_NAME:
        return selected['id']

    query = (f"'{selected['id']}' in parents and trashed=false "
             "and mimeType='application/vnd.google-apps.folder' "
             f"and name='{ROOT_NAME}'")
    found = service.files().list(q=query,
        fields='files(id,name,mimeType,trashed,capabilities(canAddChildren))',
        pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()['files']
    for child in sorted(found, key=lambda item: item['id']):
        if (child.get('name') == ROOT_NAME
                and child.get('mimeType') == 'application/vnd.google-apps.folder'
                and not child.get('trashed')):
            if not child.get('capabilities', {}).get('canAddChildren'):
                raise ValueError('A pasta dados_opd encontrada não permite gravação pela conta Google.')
            return child['id']
    return service.files().create(body={'name': ROOT_NAME,
        'mimeType': 'application/vnd.google-apps.folder', 'parents': [selected['id']],
        'appProperties': {'maestro_root': '1'}}, fields='id',
        supportsAllDrives=True).execute()['id']


def validate_client(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Selecione um arquivo JSON OAuth válido e acessível neste computador.') from exc
    installed = data.get('installed') if isinstance(data, dict) else None
    if not isinstance(installed, dict) or not all(isinstance(installed.get(k), str) and installed[k] for k in ('client_id', 'client_secret')):
        raise ValueError('O JSON precisa ser de um cliente OAuth do tipo Aplicativo para computador.')
    if installed.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth' or installed.get('token_uri') != 'https://oauth2.googleapis.com/token':
        raise ValueError('O arquivo precisa usar os endereços oficiais de autenticação do Google.')


def client_path(config_path):
    config = tomllib.loads(Path(config_path).read_text(encoding='utf-8'))
    path = Path(config.get('drive', {}).get('client_secrets_file', 'secrets/drive-client.json')).expanduser()
    return path if path.is_absolute() else Path(config_path).resolve().parent / path


def save_client_path(config_path, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Informe o caminho do arquivo JSON OAuth.')
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    path = path.resolve()
    validate_client(path)
    with _EDIT_LOCK:
        _save_setting(config_path, 'drive', 'client_secrets_file', str(path))
    return str(path)


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_):
        pass  # Never log OAuth authorization codes.


class LinkManager:
    def __init__(self, config_path):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.phase = 'idle'
        self.message = ''
        self.auth_url = None
        self.connection = 'unknown'
        self.checked_at = 0
        self.probing = False

    def start(self):
        with self.lock:
            if self.phase in ('waiting', 'preparing'):
                raise ValueError('Uma autenticação já está em andamento.')
            client = client_path(self.config_path)
            if not client.is_file():
                raise ValueError('Falta o JSON OAuth. Abra Configuração do Google Drive abaixo e informe o arquivo de Aplicativo para computador.')
            validate_client(client)
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError as exc:
                raise ValueError('Instale as dependências: python -m pip install ".[drive]"') from exc
            self.phase, self.message, self.auth_url = 'preparing', '', None
            thread = threading.Thread(target=self._authorize, args=(client, InstalledAppFlow), daemon=True)
            thread.start()

    def _authorize(self, client, flow_class):
        candidate = None
        try:
            flow = flow_class.from_client_secrets_file(str(client), SCOPES, autogenerate_code_verifier=True)
            received = []
            expected_state = None
            def callback(environ, start_response):
                query = parse_qs(environ.get('QUERY_STRING', ''))
                valid = (environ.get('PATH_INFO') == '/' and expected_state is not None
                         and secrets.compare_digest(query.get('state', [''])[0], expected_state))
                if valid:
                    received.append(environ.get('QUERY_STRING', ''))
                start_response('200 OK' if valid else '400 Bad Request', [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store')])
                return ['Retorne ao painel para verificar o vínculo.' .encode() if valid else b'Invalid OAuth callback.']
            with make_server('127.0.0.1', 0, callback, handler_class=QuietHandler) as server:
                server.timeout = 1
                flow.redirect_uri = f'http://127.0.0.1:{server.server_port}/'
                url, expected_state = flow.authorization_url(access_type='offline', prompt='consent select_account')
                with self.lock:
                    self.phase, self.auth_url = 'waiting', url
                deadline = time.monotonic()+180
                while not received and time.monotonic() < deadline:
                    server.handle_request()
                if not received:
                    raise TimeoutError('Autenticação não concluída em 3 minutos.')
                if 'error' in parse_qs(received[0]):
                    raise ValueError('Autorização cancelada ou recusada.')
                # Same loopback handling used by google-auth-oauthlib.run_local_server.
                flow.fetch_token(authorization_response=flow.redirect_uri.replace('http:', 'https:', 1)+'?'+received[0])
            with self.lock:
                self.phase, self.auth_url = 'preparing', None
            candidate = client.parent/('drive-token-'+secrets.token_hex(12)+'.json')
            save_token(candidate, flow.credentials)
            self._activate(candidate)
            candidate = None
            with self.lock:
                self.phase, self.message = 'complete', 'Drive vinculado. A troca será aplicada entre os lotes de envio.'
                self.connection, self.checked_at = 'online', time.time()
        except Exception as exc:
            with self.lock:
                self.phase, self.auth_url = 'error', None
                self.message = (str(exc) if isinstance(exc, (TimeoutError, ValueError)) else
                                'Falha ao vincular. Verifique o cliente OAuth, a API Google Drive e a conexão.')
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)

    def _activate(self, token_file):
        service = service_from_token(token_file)
        try:
            user = service.about().get(fields='user(emailAddress,permissionId)').execute()['user']
            config = tomllib.loads(Path(self.config_path).read_text(encoding='utf-8'))
            folder = config.get('drive', {}).get('folder_id', '').strip()
            if folder:
                folder = destination_folder(service, folder)
            else:
                folder = root_folder(service)
            profile = {'folder_id': folder, 'destination_name': ROOT_NAME,
                       'token_file': str(Path(token_file).resolve()),
                       'email': user.get('emailAddress', ''), 'account_id': user['permissionId']}
            path = profile_path(self.config_path)
            # Credentials and folder must be ready before replacing the old profile.
            temporary = path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(profile, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            service.close()

    def status(self):
        profile = read_profile(self.config_path)
        config = tomllib.loads(Path(self.config_path).read_text(encoding='utf-8')) if self.config_path else {}
        configured_folder = config.get('drive', {}).get('folder_id', '')
        client = client_path(self.config_path)
        try:
            validate_client(client)
            setup_error = ''
        except ValueError as exc:
            setup_error = str(exc) if client.is_file() else 'Configuração inicial pendente: informe o arquivo JSON OAuth do Google.'
        with self.lock:
            if profile and not self.probing and time.time()-self.checked_at > 60 and self.phase not in ('waiting', 'preparing'):
                self.probing = True
                threading.Thread(target=self._probe, args=(profile,), daemon=True).start()
            return {'client_path': str(client), 'setup_error': setup_error, 'phase': self.phase, 'message': self.message, 'auth_url': self.auth_url,
                    'linked': profile is not None, 'email': profile.get('email', '') if profile else '',
                    'folder_id': profile.get('folder_id', '') if profile else configured_folder,
                    'folder_ready': bool(profile and profile.get('destination_name') == ROOT_NAME),
                    'connection': (self.connection if time.time()-self.checked_at <= 120 else 'unknown') if profile else 'unlinked', 'checked_at': self.checked_at}

    def _probe(self, profile):
        service = None
        state = 'offline'
        try:
            service = service_from_token(profile['token_file'])
            folder = service.files().get(fileId=profile['folder_id'], fields='trashed,capabilities(canAddChildren)', supportsAllDrives=True).execute()
            if not folder.get('trashed') and folder.get('capabilities', {}).get('canAddChildren'):
                state = 'online'
        except Exception:
            pass
        finally:
            if service:
                service.close()
            with self.lock:
                # A late probe for the old account cannot overwrite the new account status.
                if read_profile(self.config_path) == profile:
                    self.connection, self.checked_at = state, time.time()
                self.probing = False
