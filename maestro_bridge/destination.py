"""Local destination preference editing; no OAuth or remote permission changes."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import tomllib
from urllib.parse import urlparse


def folder_id(value):
    if not isinstance(value, str):
        raise ValueError('Informe o link ou ID de uma pasta do Google Drive.')
    value = value.strip()
    if value.startswith('https://'):
        url = urlparse(value)
        match = re.fullmatch(r'/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)/*', url.path)
        if url.netloc != 'drive.google.com' or not match:
            raise ValueError('Use um link de pasta de drive.google.com.')
        value = match[1]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('Link ou ID de pasta inválido.')
    return value


def read_destination(config_path):
    if config_path is None:
        return ''
    return tomllib.loads(Path(config_path).read_text(encoding='utf-8')).get('drive', {}).get('folder_id', '')


_EDIT_LOCK = threading.Lock()

def save_destination(config_path, value):
    selected = folder_id(value)
    with _EDIT_LOCK:
        _save_setting(config_path, "drive", "folder_id", selected)
    return selected

def read_watch_directory(config_path, key="watch_directory"):
    if config_path is None:
        return ""
    path = Path(config_path).resolve()
    value = tomllib.loads(path.read_text(encoding="utf-8")).get("bridge", {}).get(key, "incoming" if key == "watch_directory" else "")
    if not value:
        return ""
    directory = Path(value).expanduser()
    return str((directory if directory.is_absolute() else path.parent / directory).resolve())

def save_watch_directory(config_path, value, key="watch_directory"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Informe o caminho da pasta de aquisições do MAESTRO.")
    directory = Path(value.strip()).expanduser()
    if not directory.is_absolute():
        directory = Path(config_path).resolve().parent / directory
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError("A pasta informada não existe neste computador.")
    try:
        with os.scandir(directory) as entries:
            next(entries, None)
    except OSError as exc:
        raise ValueError("Não foi possível acessar a pasta informada.") from exc
    with _EDIT_LOCK:
        _save_setting(config_path, "bridge", key, str(directory))
    return str(directory)

def _save_setting(config_path, section_name, key, selected):
    path = Path(config_path)
    source = path.read_text(encoding='utf-8')
    original = tomllib.loads(source)
    section = re.search(rf'^\[{section_name}\][ \t]*(?:#.*)?\r?\n(?P<body>.*?)(?=^\[|\Z)', source, re.M | re.S)
    if not section:
        raise ValueError(f'Seção [{section_name}] não encontrada na configuração.')
    body = section['body']
    replacement = key + ' = ' + json.dumps(selected, ensure_ascii=False)
    if re.search(rf'^{key}\s*=', body, re.M):
        body = re.sub(rf'^{key}\s*=.*$', lambda _: replacement, body, count=1, flags=re.M)
    else:
        body = replacement + '\n' + body
    updated = source[:section.start('body')] + body + source[section.end('body'):]
    expected = dict(original)
    expected[section_name] = dict(original.get(section_name, {}), **{key: selected})
    if tomllib.loads(updated) != expected:
        raise ValueError('Formato de configuração não suportado para edição segura.')
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return selected


def browse_directories(value=None):
    """List directories on the Bridge host; never return file contents."""
    if value is not None and not isinstance(value, str):
        raise ValueError('Caminho inválido.')
    directory = Path(value).expanduser() if value else Path.home()
    if not directory.is_absolute():
        raise ValueError('Informe um caminho absoluto.')
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError('A pasta não existe ou está indisponível.')
    children = []
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if entry.is_dir():
                    children.append({'name': entry.name, 'path': str(Path(entry.path))})
            except OSError:
                continue
    children.sort(key=lambda entry: entry['name'].casefold())
    roots = [str(Path.home())]
    if os.name == 'nt':
        roots.extend(f'{letter}:/' for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:/').is_dir())
    else:
        roots.append('/')
    return {'path': str(directory), 'parent': str(directory.parent), 'directories': children, 'roots': roots}


def read_upload_date(config_path):
    if config_path is None:
        return ''
    value = tomllib.loads(Path(config_path).read_text(encoding='utf-8')).get('bridge', {}).get('upload_date', '')
    return validate_upload_date(value) if value else ''


def validate_upload_date(value):
    from datetime import date
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Selecione uma data de modificação válida.')
    date.fromisoformat(value)
    return value


def save_upload_date(config_path, value):
    selected = validate_upload_date(value)
    with _EDIT_LOCK:
        _save_setting(config_path, 'bridge', 'upload_date', selected)
    return selected
