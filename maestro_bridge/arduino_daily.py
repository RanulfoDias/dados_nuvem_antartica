"""One mutable, verified Drive text file per modification day."""
from contextlib import closing
from datetime import datetime
import hashlib
import io
from pathlib import PureWindowsPath
import sqlite3

from .drive import DriveBackend


def consolidate(rows):
    """Merge sensor exports, preserving distinct readings and one header."""
    if len(rows) == 1:
        return rows[0]['raw']
    header = None
    records = []
    seen = set()
    for row in rows:
        lines = row['raw'].decode('utf-8-sig').splitlines()
        index = next((i for i, line in enumerate(lines) if line.startswith('DATA_HORA;')), None)
        if index is None:
            raise ValueError('TXT Arduino sem cabeçalho DATA_HORA; não é seguro consolidar formatos diferentes.')
        current = lines[:index + 1]
        if header is None:
            header = current
        elif current[-1] != header[-1]:
            raise ValueError('Colunas diferentes nos TXT Arduino; consolidação interrompida para preservar dados.')
        for line in lines[index + 1:]:
            if line.strip() and line not in seen:
                seen.add(line)
                records.append(line)
    # Exact duplicate lines are repeated snapshots; different values are retained.
    def order(line):
        try:
            return (0, datetime.strptime(line.split(';', 1)[0], '%d/%m/%Y %H:%M:%S'))
        except ValueError:
            return (1, line)
    records.sort(key=order)
    return ('\n'.join(header + records) + '\n').encode('utf-8')


class ArduinoDailyBackend(DriveBackend):
    def upload(self, sha256, raw):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            day = db.execute('SELECT modified_date FROM spectra WHERE sha256=?', (sha256,)).fetchone()[0]
            rows = db.execute('SELECT * FROM spectra WHERE modified_date=? ORDER BY created_at,sha256', (day,)).fetchall()
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS arduino_daily_files (folder TEXT, day TEXT, file_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(folder,day))')
            daily = db.execute('SELECT file_id,name FROM arduino_daily_files WHERE folder=? AND day=?', (self.folder_id,day)).fetchone()
            legacy = db.execute("SELECT o.file_id,o.digest,s.source_path FROM drive_objects o JOIN spectra s ON s.sha256=o.sha256 WHERE o.folder=? AND o.kind='spe' AND s.modified_date=? ORDER BY s.created_at", (self.folder_id,day)).fetchall()
        data = consolidate(rows)
        digest = hashlib.sha256(data).hexdigest()
        parent = self._acquisition_folder(sha256)
        if daily:
            file_id, name = daily
        else:
            # Reuse an existing original file when possible; otherwise reserve an ID.
            file_id = legacy[0]['file_id'] if legacy else self.service.files().generateIds(count=1,space='drive',type='files').execute()['ids'][0]
            name = PureWindowsPath(rows[0]['source_path']).name
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('INSERT INTO arduino_daily_files VALUES (?,?,?,?)', (self.folder_id,day,file_id,name))
        try:
            existing = self.service.files().get(fileId=file_id,fields='id,name,parents,trashed,sha256Checksum',supportsAllDrives=True).execute()
        except Exception as exc:
            if getattr(getattr(exc, 'resp', None), 'status', None) != 404:
                raise
            existing = None
        if not daily and legacy and existing and existing.get('sha256Checksum') != legacy[0]['digest']:
            raise ValueError('Arquivo original alterado no Drive; preservado para revisão.')
        if existing and (existing.get('trashed') or parent not in existing.get('parents', [])):
            raise ValueError('Arquivo diário Arduino removido ou movido; verifique o destino.')
        media = self.media_factory(io.BytesIO(data), mimetype='text/plain', resumable=False)
        if existing is None:
            self.service.files().create(body={'id':file_id,'name':name,'parents':[parent], 'appProperties':{'arduino_day':day}},media_body=media,fields='id',supportsAllDrives=True).execute()
        elif existing.get('sha256Checksum') != digest:
            self.service.files().update(fileId=file_id,media_body=media,fields='id',supportsAllDrives=True).execute()
        verified = self.service.files().get(fileId=file_id,fields='sha256Checksum',supportsAllDrives=True).execute()
        if verified.get('sha256Checksum') != digest:
            raise ValueError('Conteúdo diário Arduino não confirmado no Drive.')
        # Replace older snapshots with the verified aggregate; trash is recoverable.
        for old in legacy:
            if old['file_id'] == file_id:
                continue
            remote = self.service.files().get(fileId=old['file_id'],fields='sha256Checksum,parents,trashed',supportsAllDrives=True).execute()
            if not remote.get('trashed'):
                if parent not in remote.get('parents', []) or remote.get('sha256Checksum') != old['digest']:
                    raise ValueError('Cópia antiga alterada no Drive; preservada para revisão.')
                self.service.files().update(fileId=old['file_id'],body={'trashed':True},fields='id',supportsAllDrives=True).execute()
