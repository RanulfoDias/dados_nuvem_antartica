"""Small command-line entry point; Google Drive is opt-in."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import tempfile
import threading
import time
import tomllib
from zoneinfo import ZoneInfo

from .notifications import read_settings, deliver_pending
from .destination import read_watch_directory, read_upload_date
from .schedule import read_schedule, sync_scheduled
from .link import effective_drive
from .ui import start_server, serve
from .local import Monitor, Queue
from .science import parse_spe
from .sync import MemoryBackend, sync_once
from .drive import DriveBackend, authorize, service_from_token

LOG = logging.getLogger(__name__)


def read_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    bridge = config.setdefault('bridge', {})
    for key, default in [('watch_directory', 'incoming'), ('database', 'data/bridge.sqlite3'),
                         ('log_file', 'logs/bridge.log')]:
        value = Path(bridge.get(key, default))
        bridge[key] = value if value.is_absolute() else path.parent/value
    for key, default in [('stable_seconds', 3), ('poll_seconds', 1), ('sync_seconds', 30)]:
        value = bridge.setdefault(key, default)
        import math
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be finite and positive')
    size = bridge.setdefault('batch_size', 100)
    if not isinstance(size, int) or not 1 <= size <= 100:
        raise ValueError('batch_size must be an integer between 1 and 100')
    ZoneInfo(bridge.setdefault('acquisition_timezone', 'UTC'))
    if bridge.get('ambiguous_fold') not in (None, 0, 1):
        raise ValueError('ambiguous_fold must be 0 or 1')
    from .science import DETECTORS
    if any(value not in DETECTORS for value in config.get('detector_by_filename', {}).values()):
        raise ValueError('Filename detector mapping contains an unsupported detector')
    drive = config.setdefault('drive', {})
    for key, default in [('client_secrets_file', 'secrets/drive-client.json'), ('token_file', 'secrets/drive-token.json')]:
        value = Path(drive.get(key, default))
        drive[key] = value if value.is_absolute() else path.parent/value
    ui = config.setdefault('ui', {})
    if not isinstance(ui.setdefault('enabled', True), bool):
        raise ValueError('ui.enabled must be true or false')
    port = ui.setdefault('port', 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('ui.port must be between 1 and 65535')
    telemetry = config.setdefault('firebase_status', {})
    if not isinstance(telemetry.setdefault('enabled', False), bool):
        raise ValueError('firebase_status.enabled must be true or false')
    telemetry.setdefault('project_id', '')
    identity = telemetry.setdefault('instance_id', 'opd')
    import re
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
        raise ValueError('firebase_status.instance_id must use letters, digits, underscore or hyphen')
    interval = telemetry.setdefault('heartbeat_seconds', 60)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 60 <= interval <= 3600:
        raise ValueError('firebase_status.heartbeat_seconds must be between 60 and 3600')
    value = telemetry.get('credentials_file', '')
    if value:
        value = Path(value)
        value = value if value.is_absolute() else path.parent/value
    telemetry['credentials_file'] = value
    config['_path'] = path
    return config


def setup_logging(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s',
                        handlers=[RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5,
                                                      encoding='utf-8'), logging.StreamHandler()])


def run(config, offline):
    from .autostart import single_instance
    with single_instance(config['bridge']['database']):
        return _run(config, offline)


def _run(config, offline):
    settings = config['bridge']
    if not settings['watch_directory'].is_dir():
        raise ValueError('watch_directory must be an existing directory')
    setup_logging(settings['log_file'])
    drive = effective_drive(config)
    if not offline and drive.get('enabled') is not True:
        raise ValueError('Set drive.enabled=true for synchronization, or run with --offline')
    stop = threading.Event()
    queue = Queue(settings['database'])
    server = http_thread = None
    try:
        if config['ui']['enabled']:
            server, http_thread = start_server(settings['database'], config['ui']['port'], config_path=config['_path'])
            LOG.info('status_page http://127.0.0.1:%s', server.server_port)
    except Exception:
        queue.close()
        raise
    monitor = Monitor(settings['watch_directory'], queue, stable_seconds=settings['stable_seconds'],
                      acquisition_timezone=settings['acquisition_timezone'],
                      detector_by_filename=config.get('detector_by_filename'),
                      ambiguous_fold=settings.get('ambiguous_fold'), selected_date=read_upload_date(config['_path']))
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    ttl = max(15, settings['poll_seconds'] * 3)
    queue.runtime('monitor', running=True, at=time.time(), ttl=ttl, error=False)
    queue.runtime('drive', state='disabled' if offline else 'checking', at=time.time(), ttl=120)

    def sender():
        worker_queue = Queue(settings['database'])
        backend = None
        checked = 0
        current_identity = None
        check_ttl = max(120, settings['sync_seconds'] * 3)
        try:
            while not stop.is_set():
                try:
                    selected = effective_drive(config)
                    identity = (selected.get('folder_id'), str(selected['token_file']))
                    if backend is not None and identity != current_identity:
                        backend.close()
                        backend = None
                        worker_queue.runtime('drive', state='checking', at=time.time(), ttl=check_ttl)
                    drive = selected
                    if backend is None:
                        backend = DriveBackend(folder_id=drive.get('folder_id'), token_file=drive['token_file'],
                                               database=settings['database'])
                        current_identity = identity
                        checked = time.monotonic()
                        worker_queue.runtime('drive', state='online', at=time.time(), ttl=check_ttl)
                    elif time.monotonic() - checked >= 60:
                        backend.check_connection()
                        checked = time.monotonic()
                        worker_queue.runtime('drive', state='online', at=time.time(), ttl=check_ttl)
                    try:
                        recipient = read_settings(config['_path']).get('recipient', '')
                    except (OSError, ValueError):
                        recipient = ''
                        LOG.error('notification_settings_unavailable')
                    sync_scheduled(worker_queue, backend, config['_path'], batch_size=settings['batch_size'], notification_recipient=recipient)
                except Exception:
                    worker_queue.runtime('drive', state='offline', at=time.time(), ttl=check_ttl)
                    LOG.exception('sync_worker_failed')
                try:
                    if not stop.is_set():
                        deliver_pending(worker_queue, config['_path'])
                except Exception:
                    LOG.error('notification_worker_failed; check notification settings')
                stop.wait(settings['sync_seconds'])
        finally:
            if backend:
                backend.close()
            worker_queue.close()

    worker = threading.Thread(target=sender, name='drive-sync') if not offline else None
    if worker:
        worker.start()
    from .arduino import run as run_arduino
    arduino_worker = threading.Thread(target=run_arduino, args=(config, offline, stop), name='arduino-monitor')
    arduino_worker.start()
    reporter = None
    if config['firebase_status']['enabled'] and not offline:
        from .telemetry import Reporter
        reporter = Reporter(config).start()
    LOG.info('bridge_started offline=%s', offline)
    last_heartbeat = 0
    last_error = None
    try:
        while not stop.is_set():
            scan_error = False
            try:
                schedule = read_schedule(config['_path'])
                selected_date = schedule['date'] if schedule['time'] else read_upload_date(config['_path'])
                if selected_date != monitor.selected_date:
                    monitor.selected_date = selected_date
                    monitor.observed.clear()
                    monitor.finished.clear()
                selected_directory = Path(read_watch_directory(config['_path']))
                if selected_directory != monitor.directory:
                    monitor.directory = selected_directory
                    monitor.observed.clear()
                    monitor.finished.clear()
                    LOG.info('watch_directory_changed path=%s', selected_directory)
                monitor.scan()
            except (OSError, ValueError):
                scan_error = True
                LOG.exception('watch_directory_unavailable')
            if time.monotonic() - last_heartbeat >= 5 or scan_error != last_error:
                queue.runtime('monitor', running=True, at=time.time(), ttl=ttl, error=scan_error)
                last_heartbeat, last_error = time.monotonic(), scan_error
            stop.wait(settings['poll_seconds'])
    finally:
        stop.set()
        queue.runtime('monitor', running=False, at=time.time(), ttl=ttl, error=False)
        if reporter:
            reporter.stop()
        if server:
            server.shutdown()
            server.server_close()
            http_thread.join()
        if worker:
            worker.join()
        arduino_worker.join()
        queue.close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        LOG.info('bridge_stopped')


def offline_test():
    raw = (b'$SPEC_REM:\nDET# 1\n$DATE_MEA:\n10/14/2026 12:30:00\n'
           b'$MEAS_TIM:\n900 910\n$DATA:\n0 3\n1\n2\n3\n4\n')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'test.sqlite3'
        queue = Queue(path)
        try:
            queue.put(parse_spe(raw), raw, Path('synthetic.SPE'))
            remote = MemoryBackend()
            remote.online = False
            sync_once(queue, remote, now=0)
        finally:
            queue.close()
        queue = Queue(path)
        try:
            remote.online = True
            if sync_once(queue, remote, now=5) != 1 or len(remote.documents) != 1:
                raise RuntimeError('Offline recovery self-test failed')
            print(json.dumps({'offline_test': 'passed', 'status': queue.status()}))
        finally:
            queue.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='maestro-bridge')
    parser.add_argument('--config', default='config.toml')
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('run', help='Monitor SPE files continuously')
    start.add_argument('--offline', action='store_true')
    validate = commands.add_parser('validate', help='Validate without writing or connecting')
    validate.add_argument('file', type=Path)
    validate.add_argument('--timezone', default='UTC')
    validate.add_argument('--detector', type=int)
    commands.add_parser('publish-status', help='Publish operational configuration/status to Firestore once')
    commands.add_parser('ui', help='Show local browser status without starting the monitor')
    commands.add_parser('status', help='Show persistent local queue status')
    commands.add_parser('offline-test', help='Exercise offline/restart/recovery in an isolated database')
    auth = commands.add_parser('drive-auth', help='Authorize Google Drive on this computer')
    auth.add_argument('--create-folder', metavar='NAME', help='Create an app-accessible destination folder')
    auth.add_argument('--full-drive-access', action='store_true',
                      help='Explicitly request broad Drive access for a pre-existing folder without Picker')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            value = parse_spe(args.file.read_bytes(), acquisition_timezone=args.timezone, detector_hint=args.detector)
            print(json.dumps({key: value.to_dict()[key] for key in
                             ('sha256', 'detector_id', 'acquired_at', 'live_seconds', 'real_seconds',
                              'total_counts', 'live_rate_cps', 'live_rate_sigma_cps')}, indent=2))
        elif args.command == 'offline-test':
            offline_test()
        else:
            config = read_config(args.config)
            if args.command == 'drive-auth':
                drive = config['drive']
                authorize(drive['client_secrets_file'], drive['token_file'], full_drive_access=args.full_drive_access)
                if args.create_folder:
                    service = service_from_token(drive['token_file'])
                    try:
                        folder = service.files().create(body={'name': args.create_folder,
                            'mimeType': 'application/vnd.google-apps.folder'}, fields='id').execute()
                        print(json.dumps({'folder_id': folder['id']}))
                    finally:
                        service.close()
                else:
                    print('Google Drive authorization saved.')
            elif args.command == 'publish-status':
                from .telemetry import FirebaseStatus, document
                from .ui import snapshot
                publisher = FirebaseStatus(config)
                try:
                    publisher.publish(document(config, snapshot(config['bridge']['database'])))
                    print('Status published to app_status/' + config['firebase_status']['instance_id'])
                finally:
                    publisher.close()
            elif args.command == 'ui':
                serve(config['bridge']['database'], config['ui']['port'], config_path=config['_path'])
            elif args.command == 'status':
                if not config['bridge']['database'].is_file():
                    print(json.dumps({'states': {}, 'total': 0, 'retry_errors': 0}))
                else:
                    queue = Queue(config['bridge']['database'])
                    try:
                        print(json.dumps(queue.status(), indent=2))
                    finally:
                        queue.close()
            else:
                run(config, args.offline)
    except (OSError, ValueError, ImportError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    return 0
