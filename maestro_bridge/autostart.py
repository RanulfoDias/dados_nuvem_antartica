"""Per-user login startup. Registration never starts a second live process."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import threading

_LOCK = threading.Lock()


def identity(config_path):
    digest = hashlib.sha256(str(Path(config_path).resolve()).encode()).hexdigest()[:12]
    return 'br.opd.bridge.' + digest


def command(config_path, offline):
    runner = Path(__file__).with_name('autostart_runner.py').resolve()
    args = [sys.executable, str(runner), '--config', str(Path(config_path).resolve()), 'run']
    if offline:
        args.append('--offline')
    return args


def registration_path(config_path):
    name = identity(config_path)
    if sys.platform == 'darwin':
        return Path.home()/'Library'/'LaunchAgents'/(name+'.plist')
    if sys.platform.startswith('linux'):
        base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))
        if not base.is_absolute():
            base = Path.home()/'.config'
        return base/'autostart'/(name+'.desktop')
    return None


def _registry_value(config_path):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            return winreg.QueryValueEx(key, identity(config_path))[0]
    except FileNotFoundError:
        return ''


def status(config_path):
    platform = 'macOS' if sys.platform == 'darwin' else 'Windows' if sys.platform == 'win32' else 'Linux' if sys.platform.startswith('linux') else sys.platform
    result = {'supported': config_path is not None and platform in ('macOS','Windows','Linux'),
              'platform': platform, 'enabled': False, 'offline': True,
              'trigger': 'Ao entrar na sessão gráfica do usuário' if platform == 'Linux' else 'Ao entrar na conta do usuário'}
    if not result['supported']:
        return result
    if platform == 'Windows':
        value = _registry_value(config_path)
        result.update(enabled=bool(value), offline='--offline' in value)
    else:
        path = registration_path(config_path)
        if path.is_file():
            if platform == 'macOS':
                value = plistlib.loads(path.read_bytes())
                result.update(enabled=not value.get('Disabled', False), offline='--offline' in value.get('ProgramArguments', []))
            else:
                value = path.read_text(encoding='utf-8')
                result.update(enabled='Hidden=true' not in value and 'X-GNOME-Autostart-enabled=false' not in value,
                              offline='--offline' in value)
    return result


def desktop_quote(value):
    # Desktop Entry has a string-escape layer followed by Exec argument quoting.
    value = value.replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\'+char)
    return ('"'+value+'"').replace('\\', '\\\\')


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(config_path, enabled, offline=True):
    if not isinstance(enabled, bool) or not isinstance(offline, bool):
        raise ValueError('Selecione ativação e modo de inicialização válidos.')
    with _LOCK:
        if not status(config_path)['supported']:
            raise ValueError('Inicialização automática não suportada neste sistema.')
        if enabled:
            # Validate the same configuration that the login process will use.
            from .cli import read_config
            from .link import effective_drive
            config = read_config(config_path)
            if not config['bridge']['watch_directory'].is_dir():
                raise ValueError('Configure uma pasta de aquisições existente antes de ativar.')
            if not offline and effective_drive(config).get('enabled') is not True:
                raise ValueError('Vincule o Google Drive antes de selecionar o modo online.')
        args = command(config_path, offline)
        if any('\n' in arg or '\r' in arg for arg in args):
            raise ValueError('Os caminhos não podem conter quebras de linha.')
        if sys.platform == 'win32':
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
                if enabled:
                    winreg.SetValueEx(key, identity(config_path), 0, winreg.REG_SZ, subprocess.list2cmdline(args))
                else:
                    try:
                        winreg.DeleteValue(key, identity(config_path))
                    except FileNotFoundError:
                        pass
        else:
            path = registration_path(config_path)
            if not enabled:
                path.unlink(missing_ok=True)
            elif sys.platform == 'darwin':
                log = Path(config_path).resolve().parent/'logs'/'autostart.log'
                log.parent.mkdir(parents=True, exist_ok=True)
                content = plistlib.dumps({'Label':identity(config_path), 'ProgramArguments':args,
                    'WorkingDirectory':str(Path(config_path).resolve().parent),
                    'RunAtLoad':True, 'ProcessType':'Background',
                    'StandardOutPath':str(log), 'StandardErrorPath':str(log)})
                _write(path, content)
            else:
                content = '[Desktop Entry]\nType=Application\nName=Observatório do Pico dos Dias\nComment=Monitor de aquisições científicas e backup no Drive\n'
                content += 'Exec='+' '.join(desktop_quote(arg) for arg in args)+'\nTerminal=false\nX-GNOME-Autostart-enabled=true\n'
                _write(path, content.encode('utf-8'))
        return status(config_path)


@contextmanager
def single_instance(database):
    """Hold an OS lock for this database, including manual and login starts."""
    path = Path(str(database)+'.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('O monitor já está em execução para este banco de dados.') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError('O monitor já está em execução para este banco de dados.') from exc
        yield
    finally:
        stream.close()
