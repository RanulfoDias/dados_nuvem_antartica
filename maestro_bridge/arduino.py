"""Independent TXT collection using the independent daily schedule and Drive account."""
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import time

from .local import Monitor, Queue
from .destination import read_watch_directory
from .arduino_daily import ArduinoDailyBackend
from .link import effective_drive
from .schedule import read_schedule, sync_scheduled

LOG = logging.getLogger(__name__)


def database_path(database):
    path = Path(database)
    return path.with_name(path.stem + '-arduino' + path.suffix)


@dataclass
class TextAcquisition:
    sha256: str

    def to_dict(self):
        return {'sha256': self.sha256, 'source_type': 'arduino'}


class TextMonitor(Monitor):
    def accepts(self, path):
        return path.suffix.lower() == '.txt'

    def parse(self, raw, path):
        # Preserve the exact bytes; do not impose an encoding or CSV schema.
        return TextAcquisition(hashlib.sha256(raw).hexdigest())


def run(config, offline, stop):
    settings = config['bridge']
    queue = Queue(database_path(settings['database']))
    monitor = backend = None
    identity = None
    checked = 0
    next_sync = 0
    ttl = max(15, settings['poll_seconds'] * 3)
    try:
        while not stop.is_set():
            try:
                directory = read_watch_directory(config['_path'], 'arduino_directory')
                schedule = read_schedule(config['_path'], key='arduino_upload_time')
                if not directory:
                    queue.runtime('monitor', running=False, at=time.time(), ttl=ttl, error=False)
                    stop.wait(settings['poll_seconds'])
                    continue
                if monitor is None or monitor.directory != Path(directory) or monitor.selected_date != schedule['date']:
                    monitor = TextMonitor(directory, queue, stable_seconds=settings['stable_seconds'],
                                          acquisition_timezone=settings['acquisition_timezone'], selected_date=schedule['date'])
                try:
                    monitor.scan()
                    queue.runtime('monitor', running=True, at=time.time(), ttl=ttl, error=False)
                except (OSError, ValueError):
                    queue.runtime('monitor', running=True, at=time.time(), ttl=ttl, error=True)
                    LOG.exception('arduino_directory_unavailable')
                if offline:
                    queue.runtime('drive', state='disabled', at=time.time())
                elif time.monotonic() >= next_sync:
                    next_sync = time.monotonic() + settings['sync_seconds']
                    try:
                        drive = effective_drive(config)
                        selected = (drive.get('folder_id'), str(drive['token_file']))
                        if backend is not None and selected != identity:
                            backend.close()
                            backend = None
                        if backend is None:
                            backend = ArduinoDailyBackend(folder_id=drive.get('folder_id'), token_file=drive['token_file'],
                                                   database=database_path(settings['database']), subdirectory='sensor_arduino')
                            identity = selected
                            checked = time.monotonic()
                        elif time.monotonic() - checked >= 60:
                            backend.check_connection()
                            checked = time.monotonic()
                        sync_scheduled(queue, backend, config['_path'], key='arduino_upload_time', batch_size=settings['batch_size'])
                        queue.runtime('drive', state='online', at=time.time(), ttl=max(120, settings['sync_seconds'] * 3))
                    except Exception:
                        queue.runtime('drive', state='offline', at=time.time(), ttl=max(120, settings['sync_seconds'] * 3))
                        LOG.exception('arduino_sync_failed')
            except (OSError, ValueError):
                queue.runtime('monitor', running=True, at=time.time(), ttl=ttl, error=True)
                LOG.exception('arduino_configuration_failed')
            stop.wait(settings['poll_seconds'])
    finally:
        queue.runtime('monitor', running=False, at=time.time(), ttl=ttl, error=False)
        if backend:
            backend.close()
        queue.close()
