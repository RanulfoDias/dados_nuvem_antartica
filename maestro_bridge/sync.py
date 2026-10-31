"""Transport-independent, idempotent Google Drive delivery."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)


def metadata(row):
    result = json.loads(row['payload'])
    result['source_filename'] = Path(row['source_path']).name
    result['byte_size'] = len(row['raw'])
    if result.get('source_type') != 'arduino':
        result['uncertainty_model'] = 'poisson_sqrt_N_plugin_1sigma'
    return result


class MemoryBackend:
    """Explicit simulator; no credentials, imports, or network access."""
    def __init__(self):
        self.objects = {}
        self.documents = {}
        self.online = True
        self.fail_commit = False
        self.commits = 0

    def upload(self, sha256, raw):
        if not self.online:
            raise ConnectionError('Simulated offline Drive')
        self.objects.setdefault(sha256, raw)

    def commit(self, documents):
        if not self.online or self.fail_commit:
            raise ConnectionError('Simulated failed Drive JSON upload')
        self.documents.update(documents)
        self.commits += 1


def sync_once(queue, backend, *, batch_size=100, now=None, notification_recipient="", selected_date=None):
    if not 1 <= batch_size <= 100:
        raise ValueError('batch_size must be between 1 and 100')
    rows = queue.claim(batch_size, now=now, folder_id=getattr(backend, 'folder_id', None), selected_date=selected_date)
    ready = []
    for row in rows:
        try:
            if hashlib.sha256(row['raw']).hexdigest() != row['sha256']:
                raise ValueError('Local raw data SHA-256 mismatch')
            document = metadata(row)
            if document['sha256'] != row['sha256']:
                raise ValueError('Local metadata SHA-256 mismatch')
            backend.upload(row['sha256'], row['raw'])
            ready.append((row, document))
        except Exception as exc:
            queue.fail([row], exc, now=now)
            LOG.exception('upload_failed sha256=%s', row['sha256'])
    if not ready:
        return 0
    successful = [row for row, _ in ready]
    try:
        backend.commit([(row['sha256'], document) for row, document in ready])
        queue.complete(successful, now=now, notification_recipient=notification_recipient,
                       folder_id=getattr(backend, "folder_id", ""))
    except Exception as exc:
        queue.fail(successful, exc, now=now)
        LOG.exception('batch_commit_failed count=%s', len(ready))
        return 0
    LOG.info('batch_synced count=%s', len(ready))
    return len(ready)
