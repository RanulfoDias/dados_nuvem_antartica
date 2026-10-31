"""Google Drive delivery with persistent IDs and explicit desktop OAuth."""
from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
from datetime import datetime
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile

SCOPES = ['https://www.googleapis.com/auth/drive.file']


def save_token(path, credentials):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorize(client_secrets_file, token_file, *, full_drive_access=False):
    from google_auth_oauthlib.flow import InstalledAppFlow
    scopes = ['https://www.googleapis.com/auth/drive'] if full_drive_access else SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), scopes)
    credentials = flow.run_local_server(port=0, access_type='offline', prompt='consent')
    save_token(token_file, credentials)


def service_from_token(token_file):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    if not Path(token_file).is_file():
        raise ValueError('Run drive-auth first to authorize this desktop application')
    credentials = Credentials.from_authorized_user_file(str(token_file))
    if not credentials.valid:
        if not credentials.refresh_token:
            raise ValueError('Missing refresh token; run drive-auth again')
        credentials.refresh(Request())
        save_token(token_file, credentials)
    return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
                 cache_discovery=False)


class DriveBackend:
    def __init__(self, *, folder_id, token_file, database, service=None, media_factory=None, subdirectory=None):
        if not folder_id or not all(c.isalnum() or c in '_-' for c in folder_id):
            raise ValueError('drive.folder_id must be a folder ID, not a URL')
        self.subdirectory = subdirectory
        self.folder_id = folder_id
        self.database = database
        self.directories = {}
        self.service = service if service is not None else service_from_token(token_file)
        if media_factory is None:
            from googleapiclient.http import MediaIoBaseUpload
            media_factory = MediaIoBaseUpload
        self.media_factory = media_factory
        self.check_connection()
        with closing(sqlite3.connect(database, timeout=30)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS drive_destination (id INTEGER PRIMARY KEY CHECK(id=1), folder TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT folder FROM drive_destination WHERE id=1').fetchone()
            db.execute("""CREATE TABLE IF NOT EXISTS drive_delivery (
                folder TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL, next_attempt REAL NOT NULL,
                last_error TEXT, synced_at REAL, PRIMARY KEY(folder, sha256))""")
            if previous and previous[0] != folder_id:
                import time
                if db.execute("SELECT 1 FROM spectra WHERE state='sending' AND lease_until>? LIMIT 1",
                              (time.time(),)).fetchone():
                    raise ValueError('Wait for active uploads/leases before switching Drive folder')
                db.execute("""INSERT OR REPLACE INTO drive_delivery
                    SELECT ?, sha256, state, attempts, next_attempt, last_error, synced_at FROM spectra""",
                    (previous[0],))
                db.execute("""UPDATE spectra SET state='pending', attempts=0, next_attempt=0,
                    lease_until=0, lease_token=NULL, last_error=NULL, synced_at=NULL""")
                for row in db.execute('SELECT sha256, state, attempts, next_attempt, last_error, synced_at FROM drive_delivery WHERE folder=?', (folder_id,)).fetchall():
                    sha, state, attempts, next_attempt, error, synced = row
                    db.execute('UPDATE spectra SET state=?, attempts=?, next_attempt=?, last_error=?, synced_at=? WHERE sha256=?',
                               ('pending' if state == 'sending' else state, attempts, next_attempt, error, synced, sha))
                db.execute('UPDATE drive_destination SET folder=? WHERE id=1', (folder_id,))
            elif not previous:
                db.execute('INSERT INTO drive_destination VALUES (1, ?)', (folder_id,))
                # Previously acknowledged Firebase data still needs delivery to Drive.
                db.execute("UPDATE spectra SET state='pending', next_attempt=0, lease_until=0, lease_token=NULL, synced_at=NULL WHERE state='synced'")
            db.execute('''CREATE TABLE IF NOT EXISTS drive_objects (
                folder TEXT, sha256 TEXT, kind TEXT, file_id TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY (folder, sha256, kind))''')

    def check_connection(self):
        folder = self.service.files().get(fileId=self.folder_id, fields='id,name,mimeType,trashed,capabilities(canAddChildren)',
                                          supportsAllDrives=True).execute()
        self.root_named = folder.get('name') == 'dados_do_espectrometro'
        if (folder.get('mimeType') != 'application/vnd.google-apps.folder' or folder.get('trashed')
                or not folder.get('capabilities', {}).get('canAddChildren')):
            raise ValueError('Destination must be an accessible, writable Google Drive folder')

    def close(self):
        self.service.close()

    def _file_id(self, sha256, kind, digest):
        key = (self.folder_id, sha256, kind)
        with closing(sqlite3.connect(self.database, timeout=30)) as db, db:
            row = db.execute('SELECT file_id, digest FROM drive_objects WHERE folder=? AND sha256=? AND kind=?', key).fetchone()
        if row:
            if row[1] != digest:
                raise ValueError('Immutable Drive payload changed locally')
            return row[0]
        generated = self.service.files().generateIds(count=1, space='drive', type='files').execute()['ids'][0]
        with closing(sqlite3.connect(self.database, timeout=30)) as db, db:
            db.execute('INSERT OR IGNORE INTO drive_objects VALUES (?, ?, ?, ?, ?)', (*key, generated, digest))
            row = db.execute('SELECT file_id, digest FROM drive_objects WHERE folder=? AND sha256=? AND kind=?', key).fetchone()
            if row[1] != digest:
                raise ValueError('Conflicting local Drive payload')
        return row[0]

    def _directory(self, parent, name):
        key = parent + ':' + name
        if key in self.directories:
            return self.directories[key]
        file_id = self._file_id(key, 'directory', name)
        try:
            self.service.files().create(body={'id': file_id, 'name': name,
                'parents': [parent], 'mimeType': 'application/vnd.google-apps.folder'},
                fields='id', supportsAllDrives=True).execute()
        except Exception as exc:
            if getattr(getattr(exc, 'resp', None), 'status', None) != 409:
                raise
            folder = self.service.files().get(fileId=file_id,
                fields='name,mimeType,parents,trashed', supportsAllDrives=True).execute()
            if (folder.get('trashed') or folder.get('name') != name
                    or folder.get('mimeType') != 'application/vnd.google-apps.folder'
                    or parent not in folder.get('parents', [])):
                raise ValueError('Existing acquisition folder does not match') from exc
        self.directories[key] = file_id
        return file_id

    def _acquisition_folder(self, sha256):
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            row = db.execute('SELECT modified_date FROM spectra WHERE sha256=?', (sha256,)).fetchone()
        if row is None:
            raise ValueError('Acquisition is not persisted locally')
        if not row[0]:
            raise ValueError('Data de modificação não registrada; selecione a data e verifique a pasta local.')
        name = datetime.strptime(row[0], '%Y-%m-%d').strftime('%d_%m_%Y')
        root = self.folder_id if self.root_named else self._directory(self.folder_id, 'dados_do_espectrometro')
        if self.subdirectory:
            root = self._directory(root, self.subdirectory)
        return self._directory(root, name)

    def _original_name(self, sha256, kind):
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            row = db.execute('SELECT source_path FROM spectra WHERE sha256=?', (sha256,)).fetchone()
        if row is None:
            raise ValueError('Acquisition is not persisted locally')
        # Handle acquisitions collected on Windows as well as POSIX hosts.
        name = PureWindowsPath(row[0]).name
        return name if kind == 'spe' else str(PureWindowsPath(name).with_suffix('.json'))

    def _put(self, sha256, kind, data, mime):
        parent = self._acquisition_folder(sha256)
        digest = hashlib.sha256(data).hexdigest()
        file_id = self._file_id(sha256, kind, digest)
        media = self.media_factory(io.BytesIO(data), mimetype=mime, resumable=False)
        try:
            self.service.files().create(body={
                'id': file_id, 'name': self._original_name(sha256, kind), 'parents': [parent],
                'appProperties': {'sha256': digest, 'spectrum_sha256': sha256}},
                media_body=media, fields='id', supportsAllDrives=True).execute()
        except Exception as exc:
            if getattr(getattr(exc, 'resp', None), 'status', None) != 409:
                raise
            existing = self.service.files().get(fileId=file_id,
                fields='id,size,sha256Checksum,parents,trashed', supportsAllDrives=True).execute()
            if (existing.get('trashed') or parent not in existing.get('parents', [])
                    or existing.get('sha256Checksum') != digest or int(existing.get('size', -1)) != len(data)):
                raise ValueError('Existing Drive file failed integrity/destination verification') from exc
        return file_id

    def upload(self, sha256, raw):
        self._put(sha256, 'spe', raw, 'application/octet-stream')

    def commit(self, documents):
        # Each SPE upload is already acknowledged and integrity checked by _put.
        # Keep the batch interface; no companion JSON is uploaded.
        pass
