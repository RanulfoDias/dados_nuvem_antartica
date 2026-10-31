"""Opt-in Firestore operational status. Never stores tokens, logs or spectra."""
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import uuid

from .link import effective_drive
from .ui import snapshot

LOG = logging.getLogger(__name__)


def document(config, status, now=None):
    now = now or datetime.now(timezone.utc)
    bridge, ui = config['bridge'], config['ui']
    drive = effective_drive(config)
    settings = config['firebase_status']
    # Allowlist: never serialize the configuration or status wholesale.
    return {
        'schema_version': 1,
        'instance_id': settings['instance_id'],
        'sampled_at': now,
        'heartbeat_seconds': settings['heartbeat_seconds'],
        'stale_after_seconds': settings['heartbeat_seconds'] * 3,
        'monitor': status['monitor'],
        'drive_access': status['drive'],
        'last_scan': status['last_scan'],
        'last_drive_check': status['last_check'],
        'last_ingested_at': status['last_acquisition'],
        'last_synced_at': status['last_sync'],
        'queue': {'total': status['total'], 'pending': status['states'].get('pending', 0),
                  'sending': status['states'].get('sending', 0),
                  'synced': status['states'].get('synced', 0), 'errors': status['retry_errors']},
        'configuration': {
            'acquisition_timezone': bridge['acquisition_timezone'],
            'poll_seconds': bridge['poll_seconds'], 'stable_seconds': bridge['stable_seconds'],
            'sync_seconds': bridge['sync_seconds'], 'batch_size': bridge['batch_size'],
            'drive_enabled': drive.get('enabled', False), 'drive_folder_id': drive.get('folder_id', ''),
            'ui_enabled': ui['enabled'], 'ui_port': ui['port'],
        },
    }


def reported_state(value, now=None):
    """Remote consumers must evaluate freshness, never trust a cached 'running'."""
    now = now or datetime.now(timezone.utc)
    updated = value.get('updated_at')
    if not isinstance(updated, datetime):
        return 'unknown'
    age = (now - updated).total_seconds()
    if age < 0 or age > value['stale_after_seconds']:
        return 'no_recent_contact'
    return value['monitor']


class FirebaseStatus:
    def __init__(self, config):
        options = config['firebase_status']
        if not options['enabled']:
            raise ValueError('firebase_status.enabled must be true')
        path = options['credentials_file']
        if not path or not Path(path).is_file():
            raise ValueError('Configure an explicit Firebase service-account credentials file')
        import firebase_admin
        from firebase_admin import credentials, firestore
        credential = credentials.Certificate(str(path))
        if credential.project_id != options['project_id']:
            raise ValueError('Credential project differs from configured Firebase project')
        self.admin, self.timestamp = firebase_admin, firestore.SERVER_TIMESTAMP
        self.app = firebase_admin.initialize_app(credential, {'projectId': options['project_id']},
                                                name='maestro-status-'+uuid.uuid4().hex)
        self.db = firestore.client(app=self.app)
        self.ref = self.db.collection('app_status').document(options['instance_id'])

    def publish(self, payload):
        self.ref.set(dict(payload, updated_at=self.timestamp), retry=None, timeout=15)

    def close(self):
        self.db.close()
        self.admin.delete_app(self.app)


class Reporter:
    def __init__(self, config, factory=FirebaseStatus):
        self.config, self.factory = config, factory
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='firestore-status', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        backend = None
        try:
            while True:
                try:
                    if backend is None:
                        backend = self.factory(self.config)
                    backend.publish(document(self.config, snapshot(self.config['bridge']['database'])))
                except Exception:
                    LOG.exception('firestore_status_publish_failed')
                if self.stop_event.is_set():
                    break
                if self.stop_event.wait(self.config['firebase_status']['heartbeat_seconds']):
                    # One final sample after the caller marks the monitor stopped.
                    continue
        finally:
            if backend:
                backend.close()
