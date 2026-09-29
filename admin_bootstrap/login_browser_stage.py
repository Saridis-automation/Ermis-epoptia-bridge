"""Pinned, non-executing Chromium staging. All source traversal uses directory FDs."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager

BASE = Path('/opt/ermis/epoptia-browser')
REVISION = '1243'
VERSION = '1.63.0'
CHROMIUM = '153.0.8010.12'
LAYOUT = 'chrome-linux64/chrome'
EXECUTABLE = str(BASE / ('chromium-' + REVISION) / LAYOUT)
NAME = re.compile(r'[A-Za-z0-9_.+-]+')
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME


class PathInvariant(ValueError):
    def __init__(self, invariant, component='0', mode=None):
        self.invariant, self.component = invariant, str(component)
        # Fixed structural labels only: never expose a user-supplied path/name.
        self.component_label = {'0': 'filesystem-root', '1': 'home-parent',
                                '2': 'source-home', '3': 'source-cache-parent'}.get(
                                    self.component, 'tree-component')
        self.observed_mode = 'unknown' if mode is None else format(stat.S_IMODE(mode), '04o')
        self.mode_category = ('unknown' if mode is None else
                              'group-writable' if mode & 0o020 else
                              'world-writable' if mode & 0o002 else 'disallowed-mode')
        self.remediation = ('remove-group-write' if mode is not None and mode & 0o020
                            else 'review-mode')
        super().__init__(invariant)


def path_check(info, uid, ordinal, directory=False):
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        raise PathInvariant('unsafe-link', ordinal)
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise PathInvariant('special', ordinal)
    if directory and not stat.S_ISDIR(mode):
        raise PathInvariant('non-directory', ordinal)
    if info.st_uid != uid or (uid == 0 and info.st_gid != 0):
        raise PathInvariant('owner', ordinal)
    if mode & 0o6000:
        raise PathInvariant('suid-sgid', ordinal)
    if mode & 0o1022:
        raise PathInvariant('mode', ordinal, mode)
    if stat.S_ISREG(mode) and info.st_nlink != 1:
        raise PathInvariant('unsafe-link', ordinal)


def parents(path, uid=0):
    fd = open_path(path.parent, uid)
    os.close(fd)


def open_path(path, uid=0, flags=FLAGS, ancestry=None):
    """Root-owned / and /home, login-owned source home; installed stays root.

    Retain ancestor FDs until all directory entries have been rechecked.
    """
    if not path.is_absolute() or '..' in path.parts:
        raise PathInvariant('prefix-escape')
    if uid and path.parts[:3] != ('/', 'home', 'ermis'):
        raise PathInvariant('prefix-escape')
    chain, fd, ordinal = [], None, 0
    try:
        fd = os.open('/', flags | os.O_DIRECTORY)
        root_info = os.fstat(fd)
        path_check(root_info, 0, 0, True)
        if ancestry is not None:
            ancestry.append(('ancestor-0', signature(root_info)))
        caps(fd)
        for ordinal, part in enumerate(path.parts[1:], 1):
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            owner = uid if uid and ordinal >= 2 else 0
            path_check(before, owner, ordinal, ordinal < len(path.parts) - 1)
            child = os.open(part, flags, dir_fd=fd)
            chain.append((fd, part, before))
            fd = child
            if signature(before) != signature(os.fstat(fd)):
                raise PathInvariant('inode-device-swap', ordinal)
            caps(fd)
        for index, (parent, part, before) in enumerate(chain, 1):
            if signature(before) != signature(os.stat(part, dir_fd=parent, follow_symlinks=False)):
                raise PathInvariant('inode-device-swap', index)
            if ancestry is not None:
                ancestry.append(('ancestor-' + str(index), signature(before)))
        result, fd = fd, None
        return result
    except OSError as error:
        category = {errno.ENOENT: 'missing', errno.EACCES: 'access-denied',
                    errno.EPERM: 'access-denied', errno.ELOOP: 'unsafe-link',
                    errno.ENOTDIR: 'non-directory'}.get(error.errno, 'io-error')
        raise PathInvariant(category, ordinal) from None
    except PathInvariant as error:
        if error.invariant in ('caps', 'caps-unknown'):
            raise PathInvariant(error.invariant, ordinal) from None
        raise
    finally:
        if fd is not None:
            os.close(fd)
        for parent, _, _ in chain:
            os.close(parent)


def caps(fd):
    try:
        os.getxattr(fd, 'security.capability')
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise PathInvariant('caps-unknown') from None
    except (AttributeError, TypeError, NotImplementedError):
        raise PathInvariant('caps-unknown') from None
    else:
        raise PathInvariant('caps')


def signature(s):
    return tuple(getattr(s, k) for k in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def tree(root, uid, destination=None, *, flags=FLAGS, identities=None):
    """Hash normalized records; optionally copy the same opened bytes, never links.

    Reject links entirely, including hardlinks. Directory FD traversal protects
    against user renames/replacements between inspection and opening children.
    """
    records = []
    executable_hash = None
    def walk(fd, relative, out):
        try:
            walk_node(fd, relative, out)
        except OSError as error:
            category = {errno.ENOENT: 'missing', errno.EACCES: 'access-denied',
                        errno.EPERM: 'access-denied', errno.ELOOP: 'unsafe-link',
                        errno.ENOTDIR: 'non-directory'}.get(error.errno, 'io-error')
            raise PathInvariant(category, relative or 'tree-root') from None

    def walk_node(fd, relative, out):
        nonlocal executable_hash
        before = os.fstat(fd)
        if identities is not None:
            identities.append((relative, signature(before)))
        path_check(before, uid, relative or 'tree-root')
        try:
            caps(fd)
        except PathInvariant as error:
            raise PathInvariant(error.invariant, relative or 'tree-root') from None
        if relative == LAYOUT and not stat.S_ISREG(before.st_mode):
            raise PathInvariant('non-regular', relative)
        if before.st_uid != uid or before.st_mode & 0o7022 or (uid == 0 and before.st_gid != 0):
            raise PathInvariant('mode', relative or 'tree-root')
        directory = stat.S_ISDIR(before.st_mode)
        if directory:
            mode = 0o755
            if uid == 0 and stat.S_IMODE(before.st_mode) & ~mode:
                raise PathInvariant('mode', relative or 'tree-root')
            records.append([relative, 'd', mode, ''])
            names = sorted(os.listdir(fd))
            for name in names:
                if not NAME.fullmatch(name) or name in ('.', '..'):
                    raise PathInvariant('name', relative or 'tree-root')
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                path_check(info, uid, relative + '/' + name if relative else name)
                child = os.open(name, flags | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0), dir_fd=fd)
                try:
                    if signature(info) != signature(os.fstat(child)):
                        raise PathInvariant('inode-device-swap', relative or 'tree-root')
                    target = out / name if out else None
                    if target and stat.S_ISDIR(info.st_mode):
                        target.mkdir(mode=0o700)
                    walk(child, relative + '/' + name if relative else name, target)
                    if signature(info) != signature(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                        raise PathInvariant('inode-device-swap', relative or 'tree-root')
                finally:
                    os.close(child)
            if names != sorted(os.listdir(fd)):
                raise PathInvariant('inode-device-swap', relative or 'tree-root')
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            mode = 0o755 if before.st_mode & 0o111 else 0o644
            if uid == 0 and stat.S_IMODE(before.st_mode) != mode:
                raise PathInvariant('mode', relative or 'tree-root')
            digest = hashlib.sha256()
            output = out.open('xb') if out else None
            first = True
            try:
                while block := os.read(fd, 1048576):
                    if first and relative == LAYOUT and (block[:4] != b'\x7fELF' or not before.st_mode & 0o100):
                        raise PathInvariant('elf', LAYOUT)
                    first = False
                    digest.update(block)
                    if output:
                        output.write(block)
                if relative == LAYOUT:
                    if first:
                        raise PathInvariant('elf', LAYOUT)
                    executable_hash = digest.hexdigest()
                if output:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output:
                    output.close()
            records.append([relative, 'f', mode, digest.hexdigest()])
        else:
            raise PathInvariant('mode', relative or 'tree-root')
        if signature(before) != signature(os.fstat(fd)):
            raise PathInvariant('inode-device-swap', relative or 'tree-root')
        if out:
            os.chown(out, 0, 0, follow_symlinks=False)
            os.chmod(out, mode, follow_symlinks=False)
            caps_fd = os.open(out, flags)
            try:
                caps(caps_fd)
                os.fsync(caps_fd)
            finally:
                os.close(caps_fd)
    ancestry = []
    fd = open_path(root, uid, flags=flags, ancestry=ancestry)
    if identities is not None:
        identities.extend(ancestry)
    try:
        walk(fd, '', destination)
        current_ancestry = []
        current = open_path(root, uid, flags=flags, ancestry=current_ancestry)
        try:
            if ancestry != current_ancestry or signature(os.fstat(fd)) != signature(os.fstat(current)):
                raise PathInvariant('inode-device-swap', 'tree-root')
        finally:
            os.close(current)
    finally:
        os.close(fd)
    if executable_hash is None:
        raise PathInvariant('layout', 'tree-root')
    manifest = json.dumps(sorted(records), separators=(',', ':'), ensure_ascii=True).encode()
    return dict(schema=2, executable=EXECUTABLE, revision=REVISION, layout=LAYOUT,
                playwright=VERSION, chromium=CHROMIUM, sha256=executable_hash,
                tree_sha256=hashlib.sha256(manifest).hexdigest())


def source(root):
    account = pwd.getpwnam('ermis')
    home = Path(account.pw_dir)
    if not account.pw_uid or not re.fullmatch(r'/home/[A-Za-z0-9_-]+', str(home)):
        raise ValueError('source-invalid')
    def read(relative):
        path = root / relative
        parents(path, account.pw_uid)
        fd = open_path(path, account.pw_uid)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid not in (0, account.pw_uid) or s.st_mode & 0o7022 or s.st_size > 1048576:
                raise ValueError('metadata-invalid')
            caps(fd)
            with os.fdopen(os.dup(fd), 'rb') as stream:
                data = stream.read(1048577)
            if (len(data) > 1048576 or signature(s) != signature(os.fstat(fd)) or
                    signature(s) != signature(path.lstat())):
                raise ValueError('metadata-invalid')
            return json.loads(data)
        finally:
            os.close(fd)
    for package in ('playwright', 'playwright-core'):
        if read('node_modules/' + package + '/package.json')['version'] != VERSION:
            raise ValueError('metadata-invalid')
        if read('package-lock.json')['packages']['node_modules/' + package]['version'] != VERSION:
            raise ValueError('metadata-invalid')
    browsers = [b for b in read('node_modules/playwright-core/browsers.json')['browsers'] if b['name'] == 'chromium']
    if len(browsers) != 1 or browsers[0]['revision'] != REVISION or browsers[0]['browserVersion'] != CHROMIUM or browsers[0].get('revisionOverrides'):
        raise ValueError('metadata-invalid')
    candidates = []
    for cache in (root / '.cache/ms-playwright', home / '.cache/ms-playwright'):
        candidate = cache / ('chromium-' + REVISION)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        candidates.append(candidate)
    if not candidates:
        raise PathInvariant('missing', 'cache-candidate')
    if len(candidates) != 1:
        raise PathInvariant('ambiguous-cache', 'cache-candidate')
    return candidates[0], account.pw_uid


def identity():
    # Runtime readers are unprivileged; O_NOATIME requires ownership or privilege.
    return tree(BASE / ('chromium-' + REVISION), 0, flags=FLAGS & ~os.O_NOATIME)


def destination_state(expected):
    """Read-only target admission, including every existing parent."""
    target = BASE / ('chromium-' + REVISION)
    existing = BASE
    while True:
        try:
            existing.lstat()
            break
        except FileNotFoundError:
            existing = existing.parent
    ancestry = []
    fd = open_path(existing, ancestry=ancestry)
    try:
        path_check(os.fstat(fd), 0, 'target-parent', True)
    finally:
        os.close(fd)
    # Ignore directory timestamps changed by our own staging; retain identity
    # and safety of all pre-existing ancestors through the commit edge.
    ancestors = [(name, value[:5]) for name, value in ancestry]
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None, ancestors
    nodes = []
    if tree(target, 0, identities=nodes) != expected:
        raise ValueError('staging-conflict')
    return (signature(info), nodes), ancestors


def destination_unchanged(expected, bound):
    current, parents_now = destination_state(expected)
    parents_now = dict(parents_now)
    return current == bound[0] and all(parents_now.get(p) == value for p, value in bound[1])


def noreplace_support():
    # Symbol availability is advisory; each real commit must also succeed.
    return hasattr(ctypes.CDLL(None, use_errno=True), 'renameat2')


def object_binding(path, content=None):
    """No-follow inode/type/owner/mode plus validated content; rename-stable."""
    parents(path)
    before = path.lstat()
    identity = signature(before)[:6]
    if content is None:
        nodes = []
        metadata = tree(path, 0, identities=nodes)
        # Ancestors and directory times change on publication, node identities don't.
        nodes = [(name, value[:6]) for name, value in nodes if not name.startswith('ancestor-')]
        value = (metadata, nodes)
    else:
        fd = os.open(path, FLAGS)
        try:
            info = os.fstat(fd)
            path_check(info, 0, 'transaction-file')
            if not stat.S_ISREG(info.st_mode) or signature(info)[:6] != identity:
                raise ValueError('manual-recovery:file-identity')
            caps(fd)
            chunks = []
            remaining = len(content) + 1
            while remaining:
                block = os.read(fd, min(remaining, 1048576))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            if b''.join(chunks) != content or signature(info) != signature(os.fstat(fd)):
                raise ValueError('manual-recovery:file-content')
            value = hashlib.sha256(content).hexdigest()
        finally:
            os.close(fd)
    if signature(before) != signature(path.lstat()):
        raise ValueError('manual-recovery:object-changed')
    return identity, value


def check_binding(path, binding, content=None):
    if object_binding(path, content) != binding:
        raise ValueError('manual-recovery:binding-changed:' + path.name)


def commit_bound(source, target, binding, content=None):
    parents(target)
    check_binding(source, binding, content)
    rename_absent(source, target)
    check_binding(target, binding, content)


def rename_absent(source, target):
    """Linux atomic no-replace commit; no fallback to overwriting rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        raise OSError(ctypes.get_errno(), 'staging-commit-refused')


@contextmanager
def staged(root, prepare=None, before_publish=None, verified_source=None, ownership=None):
    candidate, uid = source(root)
    source_identity = signature(candidate.lstat())
    identities = []
    expected = tree(candidate, uid, identities=identities)
    if verified_source is not None and any(verified_source[key] != value for key, value in {
            'identity': source_identity, 'nodes': identities, 'record': expected}.items()):
        raise PathInvariant('inode-device-swap', 'preflight-source')
    if not noreplace_support():
        raise ValueError('manual-review:noreplace-unsupported')
    destination = destination_state(expected)
    if destination[0] is not None:
        raise ValueError('manual-review:first-install-only')
    if verified_source is not None and 'destination' in verified_source and destination != verified_source['destination']:
        raise ValueError('staging-destination-changed')
    # Only explicit fixed directories may be created, after ancestor validation.
    for directory in (BASE.parent, BASE):
        parents(directory)
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            pass
        parents(directory / 'child')
    target = BASE / ('chromium-' + REVISION)
    try:
        private = Path(tempfile.mkdtemp(prefix='.stage-', dir=BASE))
        private_identity = signature(private.lstat())[:5]
        temporary = private / 'tree'
        temporary.mkdir(mode=0o700)
        created = temporary.lstat()
        created_identity = (created.st_dev, created.st_ino, stat.S_IFMT(created.st_mode))
        def private_unchanged():
            if signature(private.lstat())[:5] != private_identity:
                raise ValueError('manual-recovery:private-directory-changed')
        private_unchanged()
        copied_identities = []
        if (signature(candidate.lstat()) != source_identity or
                tree(candidate, uid, temporary, identities=copied_identities) != expected or
                copied_identities != identities or
                signature(candidate.lstat()) != source_identity or tree(temporary, 0) != expected):
            raise ValueError('staging-drift')
        private_unchanged()
        copied = temporary.lstat()
        if (copied.st_dev, copied.st_ino, stat.S_IFMT(copied.st_mode)) != created_identity:
            raise ValueError('manual-recovery:staged-tree-replaced')
        binding = object_binding(temporary)
        # Profile/receipt staging and dry-parse must finish before publication.
        if prepare is not None:
            prepare(expected)
        if tree(temporary, 0) != expected:
            raise ValueError('staging-drift')
        check_binding(temporary, binding)
        # Root-only parent prevents unprivileged races. Never replace a tree.
        if before_publish is not None:
            before_publish()
        # Callbacks may take time: bind both ends again at the commit edge.
        copied_identities = []
        if (tree(candidate, uid, identities=copied_identities) != expected or
                copied_identities != identities or not destination_unchanged(expected, destination)):
            raise ValueError('staging-drift')
        check_binding(temporary, binding)
        if binding[1][0] != expected:
            raise ValueError('manual-recovery:staged-tree-changed')
        private_unchanged()
        commit_bound(temporary, target, binding)
        fd = os.open(BASE, FLAGS | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        check_binding(target, binding)
        if ownership is not None:
            def verify_owned():
                check_binding(target, binding)
                private_unchanged()
                if list(private.iterdir()):
                    raise ValueError('manual-recovery:private-directory-content')
            ownership['verify'] = verify_owned
            ownership['residue'] = (private,)
        yield expected
        if ownership is None or not ownership.get('success'):
            check_binding(target, binding)
    except BaseException as error:
        # Never delete by pathname, including before the kernel boundary. Retain
        # incomplete/private artifacts for explicit manual recovery.
        if str(error).startswith(('manual-recovery:', 'manual-review:')):
            raise
        raise ValueError('manual-recovery:browser-transaction') from error
