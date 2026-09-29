"""Fixed login installer; service operations are limited to the private login socket."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import subprocess
from contextlib import contextmanager, ExitStack

ROOT = Path('/home/ermis/projects/epoptia-bridge')
RECEIPT = Path('/usr/local/libexec/.ermis-epoptia-login-receipt')
READY = Path('/usr/local/libexec/.ermis-epoptia-login-ready')
POLICY_RECEIPT = Path('/usr/local/libexec/.ermis-epoptia-login-apparmor-receipt')
UNIT_NAME = 'ermis-epoptia-login.service'
SOCKET_NAME = 'ermis-epoptia-login.socket'
RUNTIME = '/run/ermis-epoptia-login'
SOCKET_PATH = RUNTIME + '/control.sock'
RUNTIME_HELPER_PATH = '/usr/local/libexec/ermis-epoptia-login-runtime'
# Executed as root by the socket unit: standalone, isolated, installed root-owned.
# Never import or execute code from the user-writable project at boot.
RUNTIME_HELPER = '''#!/usr/bin/python3 -I
import os
import pwd
import stat
import sys
from pathlib import Path
try:
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise ValueError()
    account = pwd.getpwnam('ermis')
    if account.pw_uid == 0:
        raise ValueError()
    for parent in (Path('/'), Path('/run')):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise ValueError()
    runtime = Path('/run/ermis-epoptia-login')
    try:
        runtime.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        try:
            os.chown(runtime, account.pw_uid, account.pw_gid)
        except BaseException:
            runtime.rmdir()
            raise
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_gid != account.pw_gid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError()
except Exception:
    sys.exit(1)
'''
SOCKET_UNIT = '''[Unit]
Description=Private Epoptia login control socket
[Socket]
ListenStream=/run/ermis-epoptia-login/control.sock
Accept=no
SocketUser=ermis
SocketGroup=ermis
SocketMode=0600
DirectoryMode=0700
ExecStartPre=/usr/local/libexec/ermis-epoptia-login-runtime
RemoveOnStop=yes
Service=ermis-epoptia-login.service
[Install]
WantedBy=sockets.target
'''
UNIT = '''[Unit]
Description=Private Epoptia login supervisor
Requires=ermis-epoptia-login.socket
After=ermis-epoptia-login.socket
[Service]
Type=simple
User=ermis
Group=ermis
WorkingDirectory=/home/ermis/projects/epoptia-bridge
ExecStart=/usr/local/libexec/ermis-epoptia-login-supervisor
RuntimeDirectory=ermis-epoptia-login/enrollment
RuntimeDirectoryMode=0700
Environment=EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE=/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome
Environment=EPOPTIA_LOGIN_BROWSER_HOME=/run/ermis-epoptia-login/enrollment
Environment=HOME=/run/ermis-epoptia-login/enrollment
Environment=XDG_CONFIG_HOME=/run/ermis-epoptia-login/enrollment
Environment=XDG_CACHE_HOME=/run/ermis-epoptia-login/enrollment
Environment=XDG_DATA_HOME=/run/ermis-epoptia-login/enrollment
Environment=XDG_STATE_HOME=/run/ermis-epoptia-login/enrollment
Environment=XDG_RUNTIME_DIR=/run/ermis-epoptia-login/enrollment
Environment=TMPDIR=/run/ermis-epoptia-login/enrollment
UMask=0077
KillMode=control-group
TimeoutStopSec=10
Restart=no
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ermis/.local/state/epoptia-browser /run/ermis-epoptia-login/enrollment
RestrictAddressFamilies=AF_UNIX AF_INET
StandardOutput=null
StandardError=null
'''
SOURCE_FILES = (
    'epoptia_login_policy.cjs', 'epoptia_login_backend.cjs',
    'epoptia_login_daemon.cjs', 'epoptia_browser_login.cjs',
    'epoptia_login_sandbox.cjs', 'epoptia_login_sandbox_worker.cjs',
    'epoptia_login_apparmor.cjs', 'epoptia_browser_launch.cjs',
    'epoptia_browser_access_check.cjs', 'chromium_runtime_smoke.cjs',
    'epoptia_browser_session.cjs', 'epoptia_browser_scheduler.cjs',
    'epoptia_browser_policy.cjs', 'epoptia_browser_runtime.cjs',
)


def verify_source(root=ROOT):
    # Isolated script execution has no project import path. This module is pure
    # at load time and is independently pinned by the diagnostic entrypoint.
    import runpy
    verifier = runpy.run_path(str(Path(__file__).with_name('login_diagnose.py')))
    return verifier['verify_source'](root)


def source_blockers(root=ROOT):
    blockers = []
    groups = (
        ('approved_origin_missing', ('epoptia_login_policy.cjs',)),
        ('authenticated_landing_signal_missing', ('epoptia_login_policy.cjs',)),
        ('persistent_loopback_backend_missing', (*SOURCE_FILES[1:], 'epoptia_browser.py')),
        ('fixed_bootstrap_installer_missing', ('admin_bootstrap/login_bootstrap.py',
                                               'admin_bootstrap/login_assets.json',
                                               'admin_bootstrap/login_apparmor.py',
                                               'admin_bootstrap/login_browser_stage.py',
                                               'admin_bootstrap/bootstrap.sh',
                                               'admin_bootstrap/bootstrap.py',
                                               'admin_bootstrap/login_diagnose.py')),
    )
    try:
        verify_source(root)
    except ValueError as error:
        entries = getattr(error, 'entries', ())
        return [name for name, files in groups if not entries or set(entries).intersection(files)]

    return blockers


def artifacts():
    # Wrapper arguments are fixed; callers cannot supply commands, URLs or binds.
    base = '#!/bin/sh\nset -eu\n[ "$#" -eq 0 ] || exit 2\n'
    check = '/usr/bin/python3 -I -B /home/ermis/projects/epoptia-bridge/admin_bootstrap/login_bootstrap.py check\n'
    output = {}
    output[Path('/usr/local/libexec/ermis-epoptia-login-supervisor')] = (
        (base + check.replace(' check\n', ' installed-check\n') + 'exec /usr/bin/node /home/ermis/projects/epoptia-bridge/epoptia_login_daemon.cjs\n').encode(), 0o755)
    for action in ('start', 'status', 'finalize', 'stop'):
        output[Path('/usr/local/bin/epoptia-login-' + action)] = (
            (base + check + 'exec /usr/bin/node /home/ermis/projects/epoptia-bridge/epoptia_browser_login.cjs ' + action + '\n').encode(), 0o755)
    output[Path(RUNTIME_HELPER_PATH)] = (RUNTIME_HELPER.encode(), 0o755)
    output[Path('/etc/systemd/system') / UNIT_NAME] = (UNIT.encode(), 0o644)
    output[Path('/etc/systemd/system') / SOCKET_NAME] = (SOCKET_UNIT.encode(), 0o644)
    return output


def validate_source(root=ROOT):
    if root.resolve() != ROOT or source_blockers(root):
        raise ValueError('source_mismatch')
    # Digest validation pins the reviewed listener arguments, not caller input.
    backend = (root / 'epoptia_login_backend.cjs').read_text()
    if "'127.0.0.1:6091', '127.0.0.1:5991'" not in backend or "'-listen', '127.0.0.1'" not in backend:
        raise ValueError('unsafe_bind')
    expected = json.loads((root / 'admin_bootstrap/login_assets.json').read_text())
    actual = {path.name: hashlib.sha256(data).hexdigest()
              for path, (data, _) in artifacts().items()}
    if expected != actual:
        raise ValueError('artifact_source_mismatch')



def fingerprint():
    return hashlib.sha256((ROOT / 'admin_bootstrap/login_manifest.json').read_bytes()).hexdigest()


def component(path):
    if path == RECEIPT:
        return 'login-receipt'
    if path.name == UNIT_NAME:
        return 'login-unit'
    if path.name == SOCKET_NAME:
        return 'login-socket-unit'
    if path.name == 'ermis-epoptia-login-runtime':
        return 'login-runtime-helper'
    if path.name == 'ermis-epoptia-login-supervisor':
        return 'login-supervisor'
    return 'login-client'


def owned_read(path, mode, owner=0):
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError('unsafe_parent')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or
                info.st_gid != owner or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode or
                info.st_size > 1048576):
            raise ValueError('unsafe_artifact')
        try:
            os.getxattr(stream.fileno(), 'security.capability')
        except OSError as error:
            import errno
            if error.errno != errno.ENODATA:
                raise ValueError('caps-unknown') from None
        except (AttributeError, TypeError, NotImplementedError):
            raise ValueError('caps-unknown') from None
        else:
            raise ValueError('caps')
        return stream.read(1048577)


def receipt_bytes(files):
    return json.dumps({'source': fingerprint(), 'files': {
        str(path): hashlib.sha256(data).hexdigest() for path, (data, _) in files.items()
    }}, sort_keys=True).encode()


def installed_blockers():
    problems = []
    files = artifacts()
    for path, (data, mode) in files.items():
        try:
            if owned_read(path, mode) != data:
                problems.append(component(path) + '-source-installed-mismatch')
        except FileNotFoundError:
            problems.append(component(path) + '-missing')
        except (OSError, ValueError):
            problems.append(component(path) + '-unsafe')
    try:
        if owned_read(RECEIPT, 0o644) != receipt_bytes(files):
            problems.append('login-receipt-source-installed-mismatch')
    except FileNotFoundError:
        problems.append('login-receipt-missing')
    except (OSError, ValueError):
        problems.append('login-receipt-unsafe')
    return list(dict.fromkeys(problems))


def configuration_blockers():
    import pwd
    try:
        uid = pwd.getpwnam('ermis').pw_uid
        state = Path('/home/ermis/.local/state/epoptia-browser')
        for parent in (state, *state.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise ValueError()
            if parent == state and (info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError()
    except (OSError, ValueError, KeyError):
        return ['login-state-unsafe']
    return []


def runtime_blockers():
    """Metadata only: never open enrollment or saved-session material."""
    import pwd
    try:
        account = pwd.getpwnam('ermis')
        if account.pw_uid == 0:
            raise ValueError()
        for name, mode, kind in ((RUNTIME, 0o700, stat.S_ISDIR),
                                 (SOCKET_PATH, 0o600, stat.S_ISSOCK)):
            info = Path(name).lstat()
            if (not kind(info.st_mode) or info.st_uid != account.pw_uid or
                    info.st_gid != account.pw_gid or stat.S_IMODE(info.st_mode) != mode):
                raise ValueError()
    except (OSError, ValueError, KeyError):
        return ['login-socket-unsafe-or-missing']
    return []


def prepare_runtime():
    """Fixed socket ExecStartPre, also run by install; no caller paths."""
    import pwd
    if os.geteuid() != 0:
        raise ValueError()
    account = pwd.getpwnam('ermis')
    if account.pw_uid == 0:
        raise ValueError()
    runtime = Path(RUNTIME)
    prepare_parent(runtime)
    try:
        runtime.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        try:
            os.chown(runtime, account.pw_uid, account.pw_gid)
        except BaseException:
            runtime.rmdir()
            raise
    info = runtime.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or
            info.st_gid != account.pw_gid or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError()


def socket_operation(action):
    # This mapping is the complete mutation policy. Never accept a unit or argv.
    commands = {
        'reload': ('/usr/bin/systemctl', 'daemon-reload'),
        'enable': ('/usr/bin/systemctl', 'enable', '--now', 'ermis-epoptia-login.socket'),
        'disable': ('/usr/bin/systemctl', 'disable', '--now', 'ermis-epoptia-login.socket'),
        'stop': ('/usr/bin/systemctl', 'disable', '--now', 'ermis-epoptia-login.service'),
    }
    if action not in commands:
        raise ValueError()
    with login_stage('B_LOGIN_SOCKET_' + action.upper()):
        result = subprocess.run(commands[action], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=20, check=False, shell=False,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        if result.returncode:
            raise ValueError()


def verify_unit_origin(kind):
    """Refuse aliases, drop-ins and same-named units from other locations."""
    name = {'socket': SOCKET_NAME, 'service': UNIT_NAME}[kind]
    result = subprocess.run(
        ['/usr/bin/systemctl', 'show', name, '--no-pager',
         '--property=FragmentPath,DropInPaths'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=5, check=False, shell=False,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError()
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
    if fields != {'FragmentPath': '/etc/systemd/system/' + name, 'DropInPaths': ''}:
        raise ValueError()


def cleanup_runtime():
    import pwd
    import re
    import shutil
    runtime = Path(RUNTIME)
    if not runtime.exists() and not runtime.is_symlink():
        return
    prepare_runtime()
    uid = pwd.getpwnam('ermis').pw_uid
    children = list(runtime.iterdir())
    for child in children:
        info = child.lstat()
        if info.st_uid != uid:
            raise ValueError()
        if child.name == 'control.sock':
            if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError()
        elif not ((child.name == 'enrollment' or re.fullmatch(r'surface-[A-Za-z0-9]{6}', child.name)) and
                  stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700):
            raise ValueError()
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError()
    for child in children:
        if child.name == 'control.sock':
            child.unlink()
        else:
            shutil.rmtree(child)
    runtime.rmdir()


def socket_blockers():
    """Fixed, read-only probes; return component codes, never command output."""
    problems = runtime_blockers()
    if problems:
        return problems
    try:
        verify_unit_origin('socket')
        verify_unit_origin('service')
        result = subprocess.run(
            ['/usr/bin/systemctl', 'show', 'ermis-epoptia-login.socket', '--no-pager',
             '--property=LoadState,ActiveState,SubState,UnitFileState,Listen'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False, shell=False,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError()
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
        substate = fields.pop('SubState', None)
        if substate not in ('listening', 'running') or fields != {
                'LoadState': 'loaded', 'ActiveState': 'active',
                'UnitFileState': 'enabled', 'Listen': SOCKET_PATH + ' (Stream)'}:
            return ['login-socket-not-enabled-listening']
        # A filesystem socket alone does not prove a listening AF_UNIX stream.
        entries = Path('/proc/net/unix').read_text().splitlines()[1:]
        if not any(len(f := line.split()) == 8 and f[7] == SOCKET_PATH and
                   f[3:6] == ['00010000', '0001', '01'] for line in entries):
            return ['login-socket-not-listening']
        result = subprocess.run(
            ['/usr/bin/ss', '-H', '-ltn', '( sport = :5991 or sport = :6091 )'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False, shell=False,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 5 or fields[0] != 'LISTEN':
                raise ValueError()
            if fields[3] not in ('127.0.0.1:5991', '127.0.0.1:6091'):
                return ['login-unsafe-listener']
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return ['login-socket-inspection-unavailable']
    return []


def dependency_blockers():
    # Offline checks only: never launch a browser or connect to Epoptia.
    missing = []
    for name in ('node', 'Xvfb', 'x11vnc', 'websockify'):
        path = Path('/usr/bin') / name
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or not os.access(path, os.X_OK):
                raise ValueError()
        except (OSError, ValueError):
            missing.append('login-dependency-' + name.lower())
    try:
        info = Path('/usr/share/novnc/vnc.html').stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError()
    except (OSError, ValueError):
        missing.append('login-dependency-novnc')
    if not missing:
        try:
            result = subprocess.run(['/usr/bin/node', '-e',
                "const lock=require('./package-lock.json');"
                "for(const p of ['playwright','playwright-core'])"
                "if(require(p+'/package.json').version!==lock.packages['node_modules/'+p].version)process.exit(1);"
                "const {resolveExecutable}=require('./epoptia_browser_runtime.cjs');"
                "process.exit(resolveExecutable(require('playwright').chromium).status ? 1 : 0)"],
                cwd=ROOT, env={'PATH': '/usr/bin:/bin', 'HOME': '/home/ermis'},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
                **({'user': 'ermis', 'group': 'ermis', 'extra_groups': []} if os.geteuid() == 0 else {}))
            if result.returncode:
                missing.append('login-dependency-chromium')
        except (OSError, subprocess.TimeoutExpired):
            missing.append('login-dependency-chromium')
    return missing


def validate_artifacts(files, owner=0):
    for path, (data, mode) in files.items():
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode or path.read_bytes() != data:
                raise ValueError('installed_mismatch')


class LoginInstallError(ValueError):
    """Contains only a fixed component/stage identifier, never exception details."""


@contextmanager
def login_stage(identifier):
    try:
        yield
    except LoginInstallError:
        raise
    except Exception as error:
        import re
        message = str(error)
        suffix = ''
        if message.startswith(('manual-recovery:', 'manual-review:')):
            # Only bounded enum tokens; never arbitrary exception/parser text.
            tokens = message.split(':')
            if all(re.fullmatch('[a-z-]{1,64}', token) for token in tokens):
                suffix = '-' + '-'.join(tokens).upper().replace('-', '_')
            else:
                suffix = '-MANUAL_RECOVERY'

        raise LoginInstallError(identifier + suffix) from None


def prepare_parent(path, create=False):
    # Walk from trusted ancestors; never follow symlinks or repair unsafe modes.
    for parent in reversed((path.parent, *path.parent.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                raise
            parent.mkdir(mode=0o755)
            os.chown(parent, 0, 0)
            info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_gid != 0 or info.st_mode & 0o022):
            raise ValueError('unsafe_parent')


CANDIDATE_BINDINGS = {}


def validate_candidate(path, data, mode):
    if path in CANDIDATE_BINDINGS:
        transaction_fs()['check_binding'](path, CANDIDATE_BINDINGS[path], data)
    if owned_read(path, mode) != data:
        raise ValueError('candidate_mismatch')
    try:
        os.getxattr(path, 'security.capability', follow_symlinks=False)
    except OSError as error:
        import errno
        if error.errno != errno.ENODATA:
            raise
    else:
        raise ValueError('candidate_capabilities')


@contextmanager
def candidate(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.ermis-login-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            created = transaction_fs()['signature'](os.fstat(stream.fileno()))[:6]
        validate_candidate(Path(name), data, mode)
        binding = transaction_fs()['object_binding'](Path(name), data)
        if binding[0] != created:
            raise ValueError('manual-recovery:temporary-replaced')
        CANDIDATE_BINDINGS[Path(name)] = binding
        yield Path(name)
    finally:
        # Retain filesystem artifacts; discard only this in-memory binding.
        CANDIDATE_BINDINGS.pop(Path(name), None)


def sync_parent(path):
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def transaction_fs():
    return policy_module()['staging']()


@contextmanager
def first_install_artifacts(files, before_publish=None):
    fs = transaction_fs()
    with ExitStack() as stack:
        pending = {}
        for path, (data, mode) in files.items():
            if os.path.lexists(path):
                raise ValueError('manual-review:first-install-only:' + component(path))
            source = stack.enter_context(candidate(path, data, mode))
            pending[path] = (source, fs['object_binding'](source, data))
        committed = set()
        def validate():
            for path, (source, binding) in pending.items():
                fs['check_binding'](path if path in committed else source, binding, files[path][0])
                if path not in committed and os.path.lexists(path):
                    raise ValueError('manual-recovery:destination-conflict:' + component(path))
        def publish():
            validate()
            if before_publish is not None:
                before_publish()
            for path, (source, binding) in pending.items():
                fs['commit_bound'](source, path, binding, files[path][0])
                committed.add(path)
                sync_parent(path)
            validate()
        publish.validate = validate
        try:
            yield publish
            if not getattr(publish, 'success', False):
                validate()
        except BaseException as error:
            if isinstance(error, LoginInstallError) or str(error).startswith(('manual-recovery:', 'manual-review:')):
                raise
            raise ValueError('manual-recovery:installed-artifacts') from error


@contextmanager
def artifact_transaction(files, action, before_publish=None):
    if action != 'install':
        raise ValueError('manual-review:first-install-only')
    with first_install_artifacts(files, before_publish) as publish:
        yield publish



def publish_artifacts(files, action):
    with artifact_transaction(files, action) as publish:
        publish()


def ready_bytes(source=None):
    return json.dumps({'source': fingerprint() if source is None else source, 'receipts': {
        str(path): hashlib.sha256(owned_read(path, 0o644)).hexdigest()
        for path in (RECEIPT, POLICY_RECEIPT)
    }}, sort_keys=True).encode()


def policy_module():
    import runpy
    return runpy.run_path(str(ROOT / 'admin_bootstrap/login_apparmor.py'))


def installed_bindings():
    policy = policy_module()
    record = policy['collect'](ROOT)
    data = policy['profile'](record)
    expected = policy['receipt_record'](record)
    if (owned_read(policy['PROFILE'], 0o644) != data or
            json.loads(owned_read(POLICY_RECEIPT, 0o644)) != expected):
        raise ValueError('installed_binding_mismatch')
    # Kernel policy is checked at privileged commit/recovery. Runtime readers
    # rehash the installed bindings without requiring privileged securityfs access.
    if os.geteuid() == 0:
        policy['loaded'](record)
    return expected


def ready_blockers():
    # The exclusive installer lock prevents readers accepting intermediate state.
    import fcntl
    try:
        fd = os.open(RECEIPT.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            validate_source()
            installed_bindings()
            if installed_blockers() or owned_read(READY, 0o644) != ready_bytes():
                raise ValueError()
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return ['login-not-ready']
    return []


@contextmanager
def readiness_transaction():
    # First-install admission never invalidates or restores a foreign marker.
    if os.path.lexists(READY):
        raise ValueError('manual-review:ready-present')
    try:
        yield
    except BaseException as error:
        if isinstance(error, LoginInstallError) or str(error).startswith(('manual-recovery:', 'manual-review:')):
            raise
        raise ValueError('manual-recovery:readiness') from error


def commit_ready(pending=None):
    validate_source()
    installed_bindings()
    if installed_blockers() or configuration_blockers():
        raise ValueError('installed_mismatch')
    data = ready_bytes()
    with ExitStack() as stack:
        if pending is None:
            pending = stack.enter_context(candidate(READY, data, 0o644))
        validate_candidate(pending, data, 0o644)
        fs = transaction_fs()
        binding = CANDIDATE_BINDINGS[pending]
        fs['commit_bound'](pending, READY, binding, data)
        sync_parent(READY)


def managed_files(files):
    """Only a root-owned receipt or exact legacy bytes authorize replacement."""
    try:
        record = json.loads(owned_read(RECEIPT, 0o644))
        allowed = {str(p) for p in files}
        legacy = allowed - {str(Path('/etc/systemd/system') / SOCKET_NAME), str(Path(RUNTIME_HELPER_PATH))}
        if set(record) != {'source', 'files'} or set(record['files']) not in (allowed, legacy):
            raise ValueError('receipt_mismatch')
    except FileNotFoundError:
        record = None
    for path, (data, mode) in files.items():
        try:
            installed = owned_read(path, mode)
        except FileNotFoundError:
            continue
        expected = record['files'].get(str(path), hashlib.sha256(data).hexdigest()) if record else hashlib.sha256(data).hexdigest()
        legacy = data.replace(b'/usr/bin/python3 -I -B ', b'/usr/bin/python3 ')
        if hashlib.sha256(installed).hexdigest() != expected and not (record is None and installed == legacy):
            raise ValueError(component(path) + '-unowned')
    return {**files, RECEIPT: (receipt_bytes(files), 0o644)}

def apparmor_transaction(action, before_publish=None, verified_source=None):
    import runpy
    module = policy_module()
    return module['transaction'](globals(), action, before_publish=before_publish, verified_source=verified_source)


def install_activity():
    """Fresh bounded unit/process observation; unknown never means inactive."""
    import runpy
    diagnostic = runpy.run_path(str(ROOT / 'admin_bootstrap/login_diagnose.py'))
    observed = diagnostic['activity_probe']()
    return {key: observed[key] for key in ('login-activity', 'browser-activity')}


def atomic_preflight(files):
    import pwd
    import runpy
    diagnostic = runpy.run_path(str(ROOT / 'admin_bootstrap/login_diagnose.py'))
    policy = policy_module()
    bound_destinations = []
    def destinations():
        # Finish installer-specific admission before any parent/candidate write.
        validate_source()
        if artifacts() != files:
            raise ValueError('source-artifacts-changed')
        snapshot = []
        account = pwd.getpwnam('ermis')
        state = Path('/home/ermis/.local/state/epoptia-browser')
        for directory in (Path('/home/ermis'), Path('/home/ermis/.local'), state.parent, state):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                snapshot.append((str(directory), None))
                continue
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or
                    info.st_mode & 0o022 or
                    (directory == state and stat.S_IMODE(info.st_mode) != 0o700)):
                raise ValueError('unsafe_state')
            snapshot.append((str(directory), info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        for path in (*files, RECEIPT, READY, policy['PROFILE'], POLICY_RECEIPT):
            existing = path.parent
            while not existing.exists():
                if existing.is_symlink():
                    raise ValueError('unsafe_parent')
                existing = existing.parent
            prepare_parent(existing / 'child', create=False)
            if path.is_symlink():
                raise ValueError('unsafe_destination')
            try:
                info = path.lstat()
                mode = files[path][1] if path in files else 0o644
                data = owned_read(path, mode)
                if path != POLICY_RECEIPT:
                    raise ValueError('manual-review:first-install-only:' + component(path))
                snapshot.append((str(path), info.st_dev, info.st_ino, hashlib.sha256(data).hexdigest()))
            except FileNotFoundError:
                snapshot.append((str(path), None))
        managed_files(files)
        bound_destinations[:] = snapshot[-(len(files) + 4):]
        return snapshot
    with login_stage('B_LOGIN_ATOMIC_RECHECK'):
        verified = diagnostic['atomic_install_recheck'](install_activity, destinations)
        if fingerprint() != verified['fingerprint']:
            raise ValueError('source-manifest-changed')
        expected_destinations = tuple(bound_destinations)
        def check_bound():
            validate_source()
            if fingerprint() != verified['fingerprint'] or artifacts() != files:
                raise ValueError('source-manifest-changed')
            current = []
            for path in (*files, RECEIPT, READY, policy['PROFILE'], POLICY_RECEIPT):
                try:
                    info = path.lstat()
                    data = owned_read(path, files[path][1] if path in files else 0o644)
                    current.append((str(path), info.st_dev, info.st_ino, hashlib.sha256(data).hexdigest()))
                except FileNotFoundError:
                    current.append((str(path), None))
            if tuple(current) != expected_destinations:
                raise ValueError('destination-changed')
        def final_recheck(prepared):
            def exact_artifacts():
                validate_source()
                if fingerprint() != verified['fingerprint'] or artifacts() != files:
                    raise ValueError('source-artifacts-changed')
                prepared['verify']()
            diagnostic['atomic_install_recheck'](
                install_activity, exact_artifacts, phase='prepared',
                expected=verified, prepared=prepared)
        verified['final_recheck'] = final_recheck
        verified['check_bound'] = check_bound
        check_bound()
        return verified


def install(action):
    with login_stage('B_LOGIN_SOURCE'):
        validate_source()
    if action == 'rollback':
        raise LoginInstallError('B_LOGIN_APPARMOR-MANUAL_REVIEW_FIRST_INSTALL_ONLY')
    if action != 'install' or os.geteuid() != 0:
        raise ValueError('invalid_install')
    import fcntl
    import pwd
    with login_stage('B_LOGIN_ACCOUNT'):
        account = pwd.getpwnam('ermis')
    files = artifacts()
    @contextmanager
    def directory_lock():
        with login_stage('B_LOGIN_LOCK'):
            # Pre-existing directory inode is the lock primitive; never O_CREAT.
            prepare_parent(Path('/usr/local/child'), create=False)
            fd = os.open(Path('/usr/local'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            try:
                os.close(fd)
            except OSError:
                pass  # Never report a failed install after the final receipt move.
    with directory_lock() as lock:
        with login_stage('B_LOGIN_LOCK'):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == 'install':
            verified_source = atomic_preflight(files)
        for path in (*files, RECEIPT, READY):
            with login_stage('B_LOGIN_PARENT_' + component(path).upper().replace('-', '_')):
                prepare_parent(path, create=action == 'install')
        with login_stage('B_LOGIN_OWNERSHIP'):
            files = managed_files(files)
        with login_stage('B_LOGIN_READY'), readiness_transaction():
            with login_stage('B_LOGIN_STATE'):
                # Only private session directory; preserve it on rollback to avoid data loss.
                state = Path('/home/ermis/.local/state/epoptia-browser')
                for directory in (Path('/home/ermis'), Path('/home/ermis/.local'), state.parent):
                    if not directory.exists():
                        directory.mkdir(mode=0o700)
                        os.chown(directory, account.pw_uid, account.pw_gid)
                    info = directory.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o022:
                        raise ValueError('unsafe_state_parent')
                if not state.exists():
                    state.mkdir(mode=0o700)
                    os.chown(state, account.pw_uid, account.pw_gid)
                info = state.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('unsafe_state')
            with login_stage('B_LOGIN_RUNTIME'):
                prepare_runtime()
            # Candidates/backups survive every enclosing publication and check.
            with artifact_transaction(files, action) as publish:
                with apparmor_transaction(action, before_publish=publish, verified_source=verified_source) as verify_policy:
                    socket_operation('reload')
                    with login_stage('B_LOGIN_UNIT_ORIGIN'):
                        verify_unit_origin('socket')
                        verify_unit_origin('service')
                    socket_operation('enable')
                    with login_stage('B_LOGIN_SOCKET_VERIFY'):
                        if socket_blockers():
                            raise ValueError()
                    with login_stage('B_LOGIN_READY'):
                        publish.validate()
                        if verify_policy is not None:
                            verify_policy()
                        # Policy context commits its receipt last on exit.


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['check']:
            validate_source()
            if ready_blockers() or configuration_blockers() or dependency_blockers():
                raise ValueError('installed_mismatch')
        elif sys.argv[1:] == ['installed-check']:
            validate_source()
            if ready_blockers() or configuration_blockers() or dependency_blockers():
                raise ValueError('installed_mismatch')
        elif sys.argv[1:] == ['ready-check']:
            if ready_blockers():
                raise ValueError('not_ready')
        elif sys.argv[1:] in (['install'], ['rollback']):
            install(sys.argv[1])
        else:
            raise ValueError()
    except Exception:
        print('login_bootstrap_refused', file=sys.stderr)
        sys.exit(1)
