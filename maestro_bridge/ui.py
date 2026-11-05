"""Loopback status page and local destination preference editor."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import secrets
from .link import LinkManager, save_client_path
from .schedule import read_schedule, save_schedule
from .arduino import database_path as arduino_database_path
from .autostart import status as autostart_status, configure as configure_autostart
from .notifications import public_status, save_recipient, save_smtp
from .destination import read_upload_date, save_upload_date, read_destination, read_watch_directory, save_watch_directory, browse_directories
from pathlib import Path
import sqlite3
import threading
import time


def snapshot(database, *, now=None):
    now = time.time() if now is None else now
    result = {'server_time': now, 'monitor': 'stopped', 'drive': 'unknown',
              'states': {}, 'total': 0, 'retry_errors': 0, 'last_acquisition': None,
              'last_sync': None, 'last_check': None, 'last_scan': None, 'active_folder': None}
    path = Path(database).resolve()
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        result['states'] = dict(db.execute('SELECT state, count(*) FROM spectra GROUP BY state'))
        result['total'] = sum(result['states'].values())
        result['retry_errors'], result['last_acquisition'], result['last_sync'] = db.execute(
            'SELECT count(CASE WHEN last_error IS NOT NULL THEN 1 END), max(created_at), max(synced_at) FROM spectra').fetchone()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='drive_destination'").fetchone():
            destination = db.execute('SELECT folder FROM drive_destination WHERE id=1').fetchone()
            result['active_folder'] = destination[0] if destination else None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_status'").fetchone():
            runtime = {key: json.loads(value) for key, value in db.execute('SELECT key, value FROM runtime_status')}
        else:
            runtime = {}
    monitor = runtime.get('monitor', {})
    result['last_scan'] = monitor.get('at')
    if monitor.get('running') and 0 <= now - monitor['at'] <= monitor.get('ttl', 15):
        result['monitor'] = 'error' if monitor.get('error') else 'running'
    elif monitor.get('running'):
        result['monitor'] = 'stale'
    drive = runtime.get('drive', {})
    result['last_check'] = drive.get('at')
    if drive.get('state') == 'disabled':
        result['drive'] = 'disabled'
    elif result['monitor'] in ('running', 'error') and 0 <= now - drive.get('at', 0) <= drive.get('ttl', 120):
        result['drive'] = drive.get('state', 'unknown')
    return result


def make_server(database, port=8765, *, config_path=None):
    linker = LinkManager(config_path) if config_path else None
    csrf = secrets.token_urlsafe(32)
    page = files('maestro_bridge').joinpath('status.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            host = self.headers.get('Host')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if (host not in allowed or self.headers.get('Origin') != f'http://{host}'
                    or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf)):
                self.send_error(403)
                return
            if self.path not in ('/api/arduino-schedule', '/api/arduino-directory', '/api/upload-schedule', '/api/upload-date', '/api/destination', '/api/drive/link', '/api/drive/client', '/api/watch-directory', '/api/directories', '/api/notifications', '/api/notifications/smtp', '/api/autostart') or config_path is None:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Requisição inválida.')
                self.connection.settimeout(5)
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('Requisição inválida.')
                if self.path == '/api/drive/link':
                    linker.start()
                    body = json.dumps({'started': True}).encode()
                elif self.path == '/api/drive/client':
                    with linker.lock:
                        if linker.phase in ('waiting', 'preparing'):
                            raise ValueError('Aguarde a autenticação em andamento antes de trocar o arquivo.')
                        selected = save_client_path(config_path, value.get('path'))
                        linker.message = ''
                        linker.phase = 'idle'
                    body = json.dumps({'client_path': selected}).encode()
                elif self.path == '/api/arduino-schedule':
                    body = json.dumps(save_schedule(config_path, value.get('time'), key='arduino_upload_time')).encode()
                elif self.path == '/api/upload-schedule':
                    body = json.dumps(save_schedule(config_path, value.get('time'))).encode()
                elif self.path == '/api/upload-date':
                    body = json.dumps({'upload_date': save_upload_date(config_path, value.get('date'))}).encode()
                elif self.path == '/api/autostart':
                    body = json.dumps(configure_autostart(config_path, value.get('enabled'), value.get('offline', True))).encode()
                elif self.path == '/api/notifications':
                    body = json.dumps({'recipient': save_recipient(config_path, value.get('recipient'))}).encode()
                elif self.path == '/api/notifications/smtp':
                    save_smtp(config_path, value)
                    body = b'{"saved":true}'
                elif self.path == '/api/directories':
                    body = json.dumps(browse_directories(value.get('path'))).encode()
                elif self.path == '/api/arduino-directory':
                    selected = save_watch_directory(config_path, value.get('path'), 'arduino_directory')
                    body = json.dumps({'arduino_directory': selected}).encode()
                elif self.path == '/api/watch-directory':
                    selected = save_watch_directory(config_path, value.get('path'))
                    body = json.dumps({'watch_directory': selected}).encode()
                else:
                    raise ValueError('Use Vincular Drive para autenticar a conta e criar o destino automaticamente.')
                status = 200
            except (ValueError, OSError) as exc:
                if self.path == '/api/drive/link':
                    with linker.lock:
                        linker.message = str(exc)
                body = json.dumps({'error': str(exc)}).encode()
                status = 400
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed:
                self.send_error(403)
                return
            route = self.path.split('?', 1)[0]
            if route == '/':
                body, mime, status = page, 'text/html; charset=utf-8', 200
            elif route in ('/assets/opd-institutions.png', '/assets/opd-ieav.png', '/assets/opd-erisa-d.png'):
                body = files('maestro_bridge').joinpath(route.lstrip('/')).read_bytes()
                mime = 'image/jpeg' if route.endswith('.jpg') else 'image/png'
                status = 200
            elif route == '/api/status':
                try:
                    state = snapshot(database)
                    state['arduino'] = snapshot(arduino_database_path(database))
                    state['arduino_schedule'] = read_schedule(config_path, key='arduino_upload_time')
                    state['arduino_directory'] = read_watch_directory(config_path, 'arduino_directory')
                    state.update(upload_schedule=read_schedule(config_path), upload_date=read_upload_date(config_path), autostart=autostart_status(config_path), notifications=public_status(config_path, database), watch_directory=read_watch_directory(config_path), can_edit_watch_directory=config_path is not None, folder_id=read_destination(config_path), can_edit_destination=False, csrf=csrf,
                                 link=linker.status() if linker else {'phase': 'idle', 'linked': False, 'connection': 'unlinked'})
                    body = json.dumps(state, allow_nan=False).encode()
                    status = 200
                except (sqlite3.Error, ValueError, OSError):
                    body, status = b'{"error":"Status temporarily unavailable"}', 503
                mime = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def start_server(database, port=8765, *, config_path=None):
    server = make_server(database, port, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, name='status-http', daemon=True)
    thread.start()
    return server, thread


def serve(database, port=8765, *, config_path=None):
    server = make_server(database, port, config_path=config_path)
    print(f'Status: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
