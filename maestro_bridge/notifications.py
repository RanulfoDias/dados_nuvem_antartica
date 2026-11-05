"""Persistent SMTP settings and retryable notifications for confirmed Drive backups."""
from contextlib import closing
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import tempfile
import threading
import time
import uuid
from urllib.parse import quote

_LOCK = threading.Lock()


def settings_path(config_path):
    path = Path(config_path).resolve()
    return path.parent / 'secrets' / (path.stem + '-notifications.json')


def read_settings(config_path):
    if config_path is None:
        return {}
    path = settings_path(config_path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def email_address(value, optional=False):
    if not isinstance(value, str):
        raise ValueError('Informe um endereço de e-mail válido.')
    value = value.strip()
    if optional and not value:
        return ''
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError('Informe um único endereço de e-mail válido, sem nome ou espaços.')
    return value


def _write(config_path, settings):
    path = settings_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(settings, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_recipient(config_path, value):
    recipient = email_address(value, optional=True)
    with _LOCK:
        settings = read_settings(config_path)
        settings['recipient'] = recipient
        _write(config_path, settings)
    return recipient


def save_smtp(config_path, value):
    with _LOCK:
        settings = read_settings(config_path)
        if any(not isinstance(value.get(key, ''), str) for key in ('host', 'username', 'sender', 'password')):
            raise ValueError('Os campos SMTP devem conter texto.')
        host = value.get('host', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host):
            raise ValueError('Informe o nome do servidor SMTP, sem https:// ou espaços.')
        port = value.get('port')
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('A porta deve estar entre 1 e 65535.')
        security = value.get('security')
        if security not in ('ssl', 'starttls'):
            raise ValueError('Selecione SSL/TLS ou STARTTLS.')
        username = value.get('username', '').strip()
        if not username or len(username) > 254 or '\n' in username or '\r' in username:
            raise ValueError('Informe o usuário SMTP.')
        sender = email_address(value.get('sender'))
        previous = settings.get('smtp', {})
        password = value.get('password', '')
        if not isinstance(password, str) or len(password) > 2048:
            raise ValueError('Senha inválida.')
        # Never reuse a credential for another server or account.
        if not password and host == previous.get('host') and username == previous.get('username'):
            password = previous.get('password', '')
        if not password:
            raise ValueError('Informe a senha SMTP ou senha de aplicativo do provedor.')
        settings['smtp'] = dict(host=host, port=port, security=security, username=username, sender=sender, password=password)
        _write(config_path, settings)


def public_status(config_path, database):
    settings = read_settings(config_path)
    smtp = settings.get('smtp', {})
    result = {'recipient': settings.get('recipient', ''),
              'configured': bool(smtp.get('host') and smtp.get('password')),
              'smtp': {key: smtp.get(key, default) for key, default in
                       [('host', ''), ('port', 465), ('security', 'ssl'), ('username', ''), ('sender', '')]},
              'pending': 0, 'failed': 0, 'last_sent': None}
    path = Path(database).resolve()
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='email_outbox'").fetchone():
                row = db.execute("""SELECT count(CASE WHEN state IN ('pending','sending') THEN 1 END),
                    count(CASE WHEN state='pending' AND attempts>0 THEN 1 END), max(sent_at)
                    FROM email_outbox WHERE recipient=?""", (result['recipient'],)).fetchone()
                result.update(pending=row[0], failed=row[1], last_sent=row[2])
    return result


def send_email(smtp, row):
    event = json.loads(row['payload'])
    message = EmailMessage()
    message['From'] = smtp['sender']
    message['To'] = row['recipient']
    message['Subject'] = f"OPD: backup no Drive concluído ({event['count']} aquisições)"
    message['Message-ID'] = f"<{row['id']}@maestro-bridge.local>"
    moment = datetime.fromtimestamp(event['completed_at'], timezone.utc).strftime('%d/%m/%Y %H:%M:%S UTC')
    folder = 'https://drive.google.com/drive/folders/' + quote(event['folder_id'], safe='')
    message.set_content(f"Observatório do Pico dos Dias — OPD\n\n"
        f"A cópia de segurança no Google Drive foi concluída.\n"
        f"Aquisições confirmadas neste lote: {event['count']}\n"
        f"Cada aquisição inclui o arquivo SPE original e o JSON científico.\n"
        f"Conclusão: {moment}\nPasta de destino: {folder}\n\n"
        "Esta confirmação corresponde ao envio para o Drive; não representa uma segunda cópia em outro serviço.\n")
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if smtp['security'] == 'ssl' else smtplib.SMTP
    options = {'timeout': 20}
    if smtp['security'] == 'ssl':
        options['context'] = context
    with factory(smtp['host'], smtp['port'], **options) as client:
        if smtp['security'] == 'starttls':
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(smtp['username'], smtp['password'])
        refused = client.send_message(message)
        if refused:
            raise ValueError('SMTP recusou o destinatário.')


def deliver_pending(queue, config_path, *, now=None, send=send_email):
    """Called only by the online worker. Failures do not undo confirmed backups."""
    now = time.time() if now is None else now
    settings = read_settings(config_path)
    recipient, smtp = settings.get('recipient', ''), settings.get('smtp', {})
    with queue.db:
        queue.db.execute("UPDATE email_outbox SET state='cancelled' WHERE recipient!=? AND state IN ('pending','sending')", (recipient,))
    if not recipient or not smtp.get('password'):
        return 0
    sent = 0
    for _ in range(5):
        token = uuid.uuid4().hex
        with queue.db:
            queue.db.execute('BEGIN IMMEDIATE')
            row = queue.db.execute("""SELECT * FROM email_outbox WHERE recipient=?
                AND state IN ('pending','sending') AND next_attempt<=? AND lease_until<=?
                ORDER BY created_at LIMIT 1""", (recipient, now, now)).fetchone()
            if row is None:
                break
            queue.db.execute("UPDATE email_outbox SET state='sending',lease_until=?,lease_token=? WHERE id=?", (now+600, token, row['id']))
        try:
            # A recipient change made before this send takes effect immediately.
            if read_settings(config_path).get('recipient', '') != recipient:
                with queue.db:
                    queue.db.execute("UPDATE email_outbox SET state='cancelled' WHERE id=? AND lease_token=?", (row['id'], token))
                break
            send(smtp, row)
        except Exception:
            # Provider exception text can contain sensitive data; persist only a generic status.
            delay = min(3600, 30 * 2**min(row['attempts'], 7))
            with queue.db:
                queue.db.execute("""UPDATE email_outbox SET state='pending', attempts=attempts+1,
                    next_attempt=?,lease_until=0,lease_token=NULL WHERE id=? AND lease_token=?""", (now+delay, row['id'], token))
        else:
            with queue.db:
                queue.db.execute("""UPDATE email_outbox SET state='sent',sent_at=?,lease_until=0,
                    lease_token=NULL WHERE id=? AND lease_token=?""", (now, row['id'], token))
            sent += 1
    return sent
