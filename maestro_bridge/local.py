"""Durable SQLite outbox and conservative polling of acquisition files."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from .science import parse_spe, SpectrumError

LOG = logging.getLogger(__name__)


class Queue:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS spectra (
                sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL, payload TEXT NOT NULL,
                source_path TEXT NOT NULL, created_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                lease_token TEXT, last_error TEXT, synced_at REAL
            );
            CREATE INDEX IF NOT EXISTS due_queue ON spectra(state, next_attempt, lease_until);
            CREATE TABLE IF NOT EXISTS runtime_status (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS email_outbox (
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL, payload TEXT NOT NULL,
                created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT, sent_at REAL
            );
            CREATE INDEX IF NOT EXISTS email_due ON email_outbox(state, next_attempt, lease_until);
            PRAGMA user_version=1;
        ''')

        columns = {row[1] for row in self.db.execute('PRAGMA table_info(spectra)')}
        if 'modified_date' not in columns:
            with self.db:
                self.db.execute('ALTER TABLE spectra ADD COLUMN modified_date TEXT')

    def runtime(self, key, **value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO runtime_status VALUES (?, ?)',
                            (key, json.dumps(value, allow_nan=False)))

    def close(self):
        self.db.close()

    def put(self, spectrum, raw: bytes, source: Path, modified_date=None) -> bool:
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO spectra
                (sha256, raw, payload, source_path, created_at, modified_date) VALUES (?, ?, ?, ?, ?, ?)''',
                (spectrum.sha256, raw, json.dumps(spectrum.to_dict(), allow_nan=False), str(source), time.time(), modified_date))
            if modified_date:
                self.db.execute("UPDATE spectra SET modified_date=? WHERE sha256=? AND modified_date IS NULL", (modified_date, spectrum.sha256))
        return cursor.rowcount == 1

    def claim(self, limit=100, now=None, lease_seconds=600, folder_id=None, selected_date=None):
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if folder_id is not None:
                destination = self.db.execute('SELECT folder FROM drive_destination WHERE id=1').fetchone()
                if not destination or destination[0] != folder_id:
                    raise ValueError('Drive destination changed; restart the synchronization worker')
            rows = self.db.execute('''SELECT * FROM spectra WHERE state != 'synced'
                AND next_attempt <= ? AND lease_until <= ? AND (? IS NULL OR modified_date=?) ORDER BY created_at LIMIT ?''',
                (now, now, selected_date, selected_date, limit)).fetchall()
            for row in rows:
                self.db.execute('''UPDATE spectra SET state='sending', lease_until=?, lease_token=?
                    WHERE sha256=?''', (now + lease_seconds, token, row['sha256']))
        return [dict(row, lease_token=token) for row in rows]

    def complete(self, rows, now=None, notification_recipient='', folder_id=''):
        now = time.time() if now is None else now
        completed = 0
        with self.db:
            for row in rows:
                cursor = self.db.execute("""UPDATE spectra SET state='synced', synced_at=?, lease_until=0,
                    lease_token=NULL, last_error=NULL WHERE sha256=? AND lease_token=?""",
                    (now, row['sha256'], row['lease_token']))
                completed += cursor.rowcount
            if completed and notification_recipient:
                payload = json.dumps({'count': completed, 'completed_at': now, 'folder_id': folder_id})
                self.db.execute('INSERT INTO email_outbox (id, recipient, payload, created_at) VALUES (?, ?, ?, ?)',
                                (uuid.uuid4().hex, notification_recipient, payload, now))
        return completed

    def fail(self, rows, error, now=None):
        now = time.time() if now is None else now
        with self.db:
            for row in rows:
                delay = min(3600, 5 * 2 ** min(row['attempts'], 10))
                self.db.execute('''UPDATE spectra SET state='pending', attempts=attempts+1,
                    next_attempt=?, lease_until=0, lease_token=NULL, last_error=?
                    WHERE sha256=? AND lease_token=?''',
                    (now + delay, str(error)[:2000], row['sha256'], row['lease_token']))

    def status(self):
        states = {row[0]: row[1] for row in self.db.execute('SELECT state, count(*) FROM spectra GROUP BY state')}
        return {'states': states, 'total': sum(states.values()), 'retry_errors': self.db.execute(
            'SELECT count(*) FROM spectra WHERE last_error IS NOT NULL').fetchone()[0]}


class Monitor:
    def __init__(self, directory, queue, *, stable_seconds=3, acquisition_timezone='UTC',
                 detector_by_filename=None, ambiguous_fold=None, max_file_bytes=64*1024*1024, selected_date=None):
        if stable_seconds <= 0:
            raise ValueError('stable_seconds must be positive')
        self.selected_date = selected_date
        self.directory = Path(directory)
        self.queue = queue
        self.stable_seconds = stable_seconds
        self.timezone = acquisition_timezone
        self.detectors = detector_by_filename or {}
        self.fold = ambiguous_fold
        self.max_file_bytes = max_file_bytes
        self.observed = {}
        self.finished = {}

    def accepts(self, path):
        return path.suffix.lower() == ".spe"

    def parse(self, raw, path):
        return parse_spe(raw, acquisition_timezone=self.timezone,
                         detector_hint=self.detectors.get(path.name), ambiguous_fold=self.fold)

    @staticmethod
    def signature(path):
        st = path.stat()
        return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino

    def scan(self, now=None):
        now = time.monotonic() if now is None else now
        added = 0
        seen = set()
        # A missing/unavailable directory is an operational error, never an empty success.
        for path in self.directory.iterdir():
            if not self.accepts(path) or not path.is_file() or path.is_symlink():
                continue
            seen.add(path)
            try:
                signature = self.signature(path)
                modified_date = datetime.fromtimestamp(signature[1] / 1_000_000_000, ZoneInfo(self.timezone)).date().isoformat()
                if self.selected_date is not None and modified_date != self.selected_date:
                    continue
                previous = self.observed.get(path)
                if previous is None or previous[0] != signature:
                    self.observed[path] = (signature, now)
                    continue
                if self.finished.get(path) == signature or now - previous[1] < self.stable_seconds:
                    continue
                if signature[0] > self.max_file_bytes:
                    raise SpectrumError('SPE exceeds configured size limit')
                with path.open('rb') as stream:
                    raw = stream.read(self.max_file_bytes + 1)
                if self.signature(path) != signature:
                    self.observed.pop(path, None)
                    continue
                if len(raw) > self.max_file_bytes:
                    raise SpectrumError('SPE exceeds configured size limit')
                spectrum = self.parse(raw, path)
                inserted = self.queue.put(spectrum, raw, path, modified_date=modified_date)
                added += int(inserted)
                self.finished[path] = signature
                LOG.info('ingested sha256=%s new=%s path=%s', spectrum.sha256, inserted, path)
            except SpectrumError as exc:
                self.finished[path] = signature
                LOG.error('invalid_spectrum path=%s error=%s', path, exc)
            except OSError:
                LOG.exception('file_read_failed path=%s', path)
        for mapping in (self.observed, self.finished):
            for path in list(mapping.keys() - seen):
                del mapping[path]
        return added
