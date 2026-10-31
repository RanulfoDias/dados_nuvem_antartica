"""Daily upload start time, using the station's configured timezone."""
from datetime import datetime
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo
from .destination import _EDIT_LOCK, _save_setting


def validate_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Informe um horário válido entre 00:00 e 23:59.')
    return value


def read_schedule(config_path, *, now=None, key="upload_time"):
    bridge = tomllib.loads(Path(config_path).read_text(encoding='utf-8')).get('bridge', {}) if config_path else {}
    zone = bridge.get('acquisition_timezone', 'UTC')
    current = datetime.now(ZoneInfo(zone)) if now is None else now.astimezone(ZoneInfo(zone))
    selected = bridge.get(key, '')
    if selected:
        validate_time(selected)
    return {'time': selected, 'timezone': zone, 'date': current.date().isoformat(),
            'due': bool(selected and current.strftime('%H:%M') >= selected)}


def save_schedule(config_path, value, *, key="upload_time"):
    selected = validate_time(value)
    with _EDIT_LOCK:
        _save_setting(config_path, 'bridge', key, selected)
    return read_schedule(config_path, key=key)


def sync_scheduled(queue, backend, config_path, *, now=None, key="upload_time", **kwargs):
    from .sync import sync_once
    schedule = read_schedule(config_path, now=now, key=key)
    if not schedule['due']:
        return 0
    return sync_once(queue, backend, selected_date=schedule['date'], **kwargs)
