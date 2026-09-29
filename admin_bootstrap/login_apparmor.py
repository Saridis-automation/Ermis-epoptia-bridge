"""Exact Chromium userns exception. No browser launch or global policy changes."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
from contextlib import contextmanager, ExitStack

# Loaded only after the bootstrap source manifest has been validated.
def staging():
    source = Path(globals().get('__file__', '/home/ermis/projects/epoptia-bridge/admin_bootstrap/login_apparmor.py')).with_name('login_browser_stage.py')
    data = diagnostic_read(source)
    manifest = json.loads(diagnostic_read(source.with_name('login_manifest.json')))
    if hashlib.sha256(data).hexdigest() != manifest['admin_bootstrap/login_browser_stage.py']:
        raise ValueError()
    namespace = {'__name__': 'login_browser_stage', '__file__': str(source)}
    exec(compile(data, '<login-browser-stage>', 'exec'), namespace)
    return namespace

PROFILE = Path('/etc/apparmor.d/ermis-epoptia-login-chromium')
RECEIPT = Path('/usr/local/libexec/.ermis-epoptia-login-apparmor-receipt')
PARSER = '/usr/sbin/apparmor_parser'
PROFILES = Path('/sys/kernel/security/apparmor/profiles')
GRAMMAR = re.compile(r'/opt/ermis/epoptia-browser/chromium-(1243)/chrome-linux64/chrome')
LEGACY = re.compile(r'/home/ermis/(?:\.cache|projects/epoptia-bridge/\.cache)/ms-playwright/chromium-([1-9][0-9]*)/chrome-linux(?:64)?/chrome')


# Diagnose is deliberately independent of collect(), parser(), and transaction().
# In particular, it never executes project JavaScript or creates staged files.
STAGES = frozenset('''apparmor-disabled apparmor-parser-missing
apparmor-source-invalid apparmor-staging-missing apparmor-staging-invalid apparmor-profile-syntax-invalid apparmor-profile-not-installed
apparmor-profile-content-mismatch apparmor-profile-installed-not-loaded
apparmor-profile-loaded-identity-mismatch apparmor-receipt-missing
apparmor-receipt-mismatch apparmor-partial-install-recoverable apparmor-ready
apparmor-unknown'''.split())
RECOVERIES = frozenset('''no-retry source-fix-required reinstall-safe
rollback-required manual-review-required'''.split())
ENABLED = Path('/sys/module/apparmor/parameters/enabled')


def diagnostic_read(path, owner=None, mode=None, limit=1048576, digest_only=False):
    # No atime updates, links, special files, or unbounded reads.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                (owner is not None and before.st_uid != owner) or
                (mode is not None and (before.st_gid != owner or stat.S_IMODE(before.st_mode) != mode))):
            raise ValueError()
        # Kernel observation files are never copied or executed.
        if path.parts[:2] != ('/', 'sys'):
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
        if digest_only:
            digest, size = hashlib.sha256(), 0
            while block := stream.read(min(1048576, limit + 1 - size)):
                size += len(block)
                if size > limit:
                    raise ValueError()
                digest.update(block)
            data = digest.hexdigest()
        else:
            data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if len(data) > limit or any(getattr(before, k) != getattr(after, k) or
                              getattr(after, k) != getattr(current, k)
                              for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise ValueError()
    return data


def diagnostic_parents(path, uid=0):
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) or info.st_mode & 0o022:
            raise ValueError()


def diagnostic_identity(root):
    return staging()['identity']()


def diagnostic_parser(data):
    diagnostic_parents(Path(PARSER))
    info = Path(PARSER).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o022 or not info.st_mode & 0o111):
        raise ValueError()
    # Use the manifest-verified, write-confined two-stage parser for every
    # diagnostic entrypoint, including the legacy public AppArmor API.
    source = Path(__file__).with_name('login_diagnose.py')
    code = diagnostic_read(source)
    manifest = json.loads(diagnostic_read(source.with_name('login_manifest.json')))
    if hashlib.sha256(code).hexdigest() != manifest['admin_bootstrap/login_diagnose.py']:
        raise ValueError('diagnostic-source-mismatch')
    module = {'__name__': 'login_diagnose', '__file__': str(source)}
    exec(compile(code, '<verified-diagnose>', 'exec'), module)
    try:
        module['parser'](data)
    except module['Failure'] as error:
        if error.result[0] == 'apparmor-profile-syntax-invalid':
            return False
        raise ValueError('parser-environment-unknown') from None
    return True


def diagnostic_attachment(record):
    # The flat profiles list proves name/mode, not a separately specified
    # attachment. Require the kernel's attachment attribute for this exact name.
    directory = PROFILES.parent / 'policy/profiles'
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        names = os.listdir(fd)
    finally:
        os.close(fd)
    if len(names) > 4096:
        raise ValueError()
    matches = []
    for name in names:
        entry = directory / name
        if not stat.S_ISDIR(entry.lstat().st_mode):
            raise ValueError()
        if diagnostic_read(entry / 'name', limit=4096).strip() == profile_name(record).encode():
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError()
    return (diagnostic_read(matches[0] / 'attach', limit=4096).strip() == record['executable'].encode()
            and diagnostic_read(matches[0] / 'mode', limit=64).strip() == b'unconfined')


def diagnose(root):
    """One bounded primary stage and recovery; no mutation or raw exceptions.

    Backups in the existing transaction have random shared names and no durable
    ownership receipt. Never enumerate, read, or infer ownership of those files.
    Only a coherent owned profile/receipt pair proves rollback material.
    """
    try:
        if os.geteuid() != 0:
            raise ValueError()
        enabled = diagnostic_read(ENABLED, limit=16).strip()
        if enabled == b'N':
            return 'apparmor-disabled', 'no-retry'
        if enabled != b'Y':
            raise ValueError()
        manifest = json.loads(diagnostic_read(root / 'admin_bootstrap/login_manifest.json'))
        for relative in ('admin_bootstrap/bootstrap.py', 'admin_bootstrap/login_apparmor.py',
                         'epoptia_login_apparmor.cjs', 'epoptia_browser_runtime.cjs',
                         'admin_bootstrap/login_browser_stage.py'):
            if hashlib.sha256(diagnostic_read(root / relative)).hexdigest() != manifest[relative]:
                return 'apparmor-unknown', 'source-fix-required'
        try:
            Path(PARSER).lstat()
        except FileNotFoundError:
            return 'apparmor-parser-missing', 'source-fix-required'
        try:
            record = diagnostic_identity(root)
            expected = profile(record)
        except (FileNotFoundError, ValueError) as error:
            if not isinstance(error, FileNotFoundError) and getattr(error, 'invariant', None) != 'missing':
                return 'apparmor-staging-invalid', 'manual-review-required'
            try:
                module = staging()
                candidate, uid = module['source'](root)
                module['tree'](candidate, uid)
            except (ValueError, OSError, KeyError, TypeError):
                return 'apparmor-source-invalid', 'source-fix-required'
            return 'apparmor-staging-missing', 'manual-review-required'
        except (ValueError, OSError, KeyError, TypeError):
            return 'apparmor-staging-invalid', 'manual-review-required'
        if not diagnostic_parser(expected):
            return 'apparmor-profile-syntax-invalid', 'source-fix-required'
        artifacts = []
        for path in (PROFILE, RECEIPT):
            diagnostic_parents(path)
            try:
                artifacts.append(diagnostic_read(path, 0, 0o644, 16384))
            except FileNotFoundError:
                artifacts.append(None)
        installed, receipt = artifacts
        lines = diagnostic_read(PROFILES).decode('ascii').splitlines()
        entries = [line for line in lines if line.startswith(profile_name(record) + ' (')]
        if entries and entries != [profile_name(record) + ' (unconfined)']:
            return 'apparmor-profile-loaded-identity-mismatch', 'manual-review-required'
        active = bool(entries)
        if active and not diagnostic_attachment(record):
            return 'apparmor-profile-loaded-identity-mismatch', 'manual-review-required'
        expected_receipt = receipt_record(record)
        receipt_matches = False
        rollback = False
        if receipt is not None:
            try:
                old = json.loads(receipt)
                receipt_matches = old == expected_receipt
                identity = {k: v for k, v in old.items() if k not in ('profile_sha256', 'policy_identity', 'policy_sha256')}
                old_data = profile(identity)
                rollback = (set(old) == set(expected_receipt) and installed == old_data and
                            old['profile_sha256'] == hashlib.sha256(old_data).hexdigest() and
                            lines.count(profile_name(identity) + ' (unconfined)') == 1 and
                            diagnostic_attachment(identity))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        if installed is not None and b'owned v1\n' in installed:
            try:
                legacy = legacy_profile(installed)
                coherent = receipt is None or json.loads(receipt) == {**legacy, 'profile_sha256': hashlib.sha256(installed).hexdigest()}
                active_legacy = any(line.startswith(legacy['executable'] + ' (') for line in lines)
                if coherent and not active_legacy and not active:
                    return 'apparmor-partial-install-recoverable', 'manual-review-required'
            except (ValueError, KeyError, TypeError):
                pass
            return 'apparmor-profile-content-mismatch', 'manual-review-required'
        if installed is not None and installed != expected:
            return 'apparmor-profile-content-mismatch', 'rollback-required' if rollback else 'manual-review-required'
        if receipt is not None and not receipt_matches:
            return 'apparmor-receipt-mismatch', 'manual-review-required'
        if installed is None:
            if active or receipt is not None:
                return 'apparmor-partial-install-recoverable', 'manual-review-required'
            return 'apparmor-profile-not-installed', 'manual-review-required'
        if not active:
            return 'apparmor-profile-installed-not-loaded', 'manual-review-required'
        if receipt is None:
            return 'apparmor-receipt-missing', 'manual-review-required'
        return 'apparmor-ready', 'no-retry'
    except Exception:
        return 'apparmor-unknown', 'manual-review-required'


def validate_identity(record):
    if set(record) != {'schema', 'executable', 'revision', 'layout', 'playwright', 'chromium', 'sha256', 'tree_sha256'}:
        raise ValueError()
    match = GRAMMAR.fullmatch(record['executable'])
    if (record['schema'] != 2 or not match or match[1] != record['revision'] or
            record['layout'] != 'chrome-linux64/chrome' or record['playwright'] != '1.63.0' or
            record['chromium'] != '153.0.8010.12' or
            any(not re.fullmatch(r'[0-9a-f]{64}', record[k]) for k in ('sha256', 'tree_sha256'))):
        raise ValueError()
    return record


def canonical_policy(record):
    # Hash attachment, ABI, mode and exact rules before inserting the name.
    return ('abi <abi/4.0>,\nattachment "' + record['executable'] +
            '" flags=(unconfined) {\n  userns,\n}\n').encode()


def profile_name(record):
    return ('ermis-epoptia-login-chromium-' + record['revision'] + '-' +
            hashlib.sha256(canonical_policy(record)).hexdigest())


def receipt_record(record):
    return {**record, 'profile_sha256': hashlib.sha256(profile(record)).hexdigest(),
            'policy_identity': profile_name(record),
            'policy_sha256': hashlib.sha256(canonical_policy(record)).hexdigest()}



def profile(record):
    validate_identity(record)
    return ('abi <abi/4.0>,\n# ermis-epoptia-login owned v2\n' +
            '# playwright=' + record['playwright'] + ' chromium=' + record['chromium'] +
            ' revision=' + record['revision'] + ' sha256=' + record['sha256'] +
            ' tree_sha256=' + record['tree_sha256'] + '\nprofile "' + profile_name(record) +
            '" "' + record['executable'] + '" flags=(unconfined) {\n  userns,\n}\n').encode()


def collect(root):
    return validate_identity(staging()['identity']())


def parser(action, source):
    if action == 'validate':
        if not diagnostic_parser(diagnostic_read(source)):
            raise ValueError('candidate-syntax-invalid')
        return
    # Never use parser caches or reload a directory of unrelated policy.
    flags = {'load': ['--add']}[action]
    result = subprocess.run([PARSER, '--config-file=/dev/null', '--skip-cache', *flags, str(source)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=20, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C'})
    if type(result.returncode) is not int or result.returncode != 0:
        raise ValueError('kernel-add-unproven')
    return 0


def kernel_entries():
    directory = PROFILES.parent / 'policy/profiles'
    rows = []
    pending = [(directory, None)]
    total = 0
    while pending:
        current, parent = pending.pop()
        fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
        total += len(names)
        if total > 4096:
            raise ValueError('kernel-inventory-unknown')
        for name in names:
            entry = current / name
            if not stat.S_ISDIR(entry.lstat().st_mode):
                raise ValueError('kernel-inventory-unknown')
            fields = []
            for key in ('name', 'mode', 'attach'):
                value = diagnostic_read(entry / key, limit=4096).decode('ascii')
                # Kernel attributes may have one terminal newline, never partial or
                # whitespace-normalized records. Keep every record, including duplicates.
                value = value.removesuffix('\n')
                if not value or value != value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value):
                    raise ValueError('kernel-inventory-unknown')
                fields.append(value)
            if fields[1] not in ('enforce', 'complain', 'kill', 'unconfined'):
                raise ValueError('kernel-inventory-unknown')
            fields[0] = fields[0] if parent is None else parent + '//' + fields[0]
            rows.append(tuple(fields))
            nested = entry / 'profiles'
            try:
                nested_info = nested.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(nested_info.st_mode):
                raise ValueError('kernel-inventory-unknown')
            pending.append((nested, fields[0]))
    listing = diagnostic_read(PROFILES).decode('ascii')
    lines = listing.removesuffix('\n').split('\n') if listing else []
    if sorted(lines) != sorted(name + ' (' + mode + ')' for name, mode, _ in rows):
        raise ValueError('kernel-inventory-unknown')
    return rows


def loaded(record):
    try:
        expected = (profile_name(record), 'unconfined', record['executable'])
        rows = kernel_entries()
        # Broad conflict detection only; success requires exactly one full tuple.
        # Include alternate names bound to this executable and ambiguous patterns.
        relevant = [row for row in rows if
                    any(marker in field for field in row for marker in
                        ('ermis-epoptia-login-chromium', record['executable'], '/ms-playwright/'))
                    or any(c in row[2] for c in '*?[]{}^\\@')]
        if relevant != [expected]:
            raise ValueError('kernel-identity-mismatch')
    except Exception as error:
        raise ValueError('manual-recovery:kernel-loaded-unproven') from error


def legacy_profile(data):
    # Exact old generator, never parser-load or dry-parse a user-cache attachment.
    expression = (rb'abi <abi/4.0>,\n# ermis-epoptia-login owned v1\n'
                  rb'# playwright=(\d+\.\d+\.\d+) chromium=(\d+(?:\.\d+){3}) revision=([1-9][0-9]*) sha256=([0-9a-f]{64})'
                  rb'\nprofile "([^"\n]+)" flags=\(unconfined\) {\n  userns,\n}\n')
    match = re.fullmatch(expression, data)
    if not match:
        raise ValueError()
    version, browser, revision, digest, executable = (v.decode('ascii') for v in match.groups())
    path = LEGACY.fullmatch(executable)
    if not path or path[1] != revision:
        raise ValueError()
    return dict(schema=1, executable=executable, revision=revision, playwright=version, chromium=browser, sha256=digest)


def previous(api):
    def read(path):
        try:
            return api['owned_read'](path, 0o644)
        except FileNotFoundError:
            return None
    receipt, data = read(RECEIPT), read(PROFILE)
    if data is None:
        if receipt is None:
            return None
        record = json.loads(receipt)
        checksum = record.pop('profile_sha256')
        # Receipt-only legacy partial installs are recognized using the exact old
        # generator. Never write/load this reconstructed attachment.
        if set(record) != {'schema', 'executable', 'revision', 'playwright', 'chromium', 'sha256'} or record['schema'] != 1:
            raise ValueError()
        legacy = ('abi <abi/4.0>,\n# ermis-epoptia-login owned v1\n# playwright=' + record['playwright'] +
                  ' chromium=' + record['chromium'] + ' revision=' + record['revision'] + ' sha256=' + record['sha256'] +
                  '\nprofile "' + record['executable'] + '" flags=(unconfined) {\n  userns,\n}\n').encode()
        if legacy_profile(legacy) != record or hashlib.sha256(legacy).hexdigest() != checksum:
            raise ValueError()
        if any(line.startswith(record['executable'] + ' (') for line in PROFILES.read_text().splitlines()):
            raise ValueError()
        return record, None, receipt, False
    if b'owned v1\n' in data:
        record = legacy_profile(data)
        if receipt is not None and json.loads(receipt) != {**record, 'profile_sha256': hashlib.sha256(data).hexdigest()}:
            raise ValueError()
        # Restoring a loaded unsafe attachment is forbidden. Unknown kernel state
        # needs manual review; the known failed, unloaded partial install is safe.
        if any(line.startswith(record['executable'] + ' (') for line in PROFILES.read_text().splitlines()):
            raise ValueError()
        return record, data, receipt, False
    if receipt is None:
        record = collect(api['ROOT'])
        if profile(record) != data:
            raise ValueError()
    else:
        record = json.loads(receipt)
        saved = dict(record)
        checksum = record.pop('profile_sha256')
        record.pop('policy_identity', None)
        record.pop('policy_sha256', None)
        if saved != receipt_record(record):
            raise ValueError()
        if checksum != hashlib.sha256(data).hexdigest() or profile(record) != data:
            raise ValueError()
    lines = [line for line in PROFILES.read_text().splitlines() if line.startswith(profile_name(record) + ' (')]
    if lines and lines != [profile_name(record) + ' (unconfined)']:
        raise ValueError()
    if lines:
        loaded(record)
    return record, data, receipt, bool(lines)


@contextmanager
def transaction(api, action, before_publish=None, verified_source=None):
    # Prepare policy against the unpublished tree; its publisher runs only after
    # staged() has completed offline validation and atomically renamed the tree.
    with api['login_stage']('B_LOGIN_APPARMOR'), ExitStack() as stack:
        if action == 'install':
            prepared = []
            readiness = []
            def prepare(record):
                if verified_source is not None:
                    verified_source['check_bound']()
                    if profile(record) != verified_source['profile']:
                        raise ValueError('profile-changed')
                prepared.append(stack.enter_context(policy_transaction(api, action, record)))
                # Construct the success marker from staged receipt bytes, before
                # invalidating admission or publishing the browser/policy.
                data = json.dumps({'source': api['fingerprint'](), 'receipts': {
                    str(api['RECEIPT']): hashlib.sha256(api['receipt_bytes'](api['artifacts']())).hexdigest(),
                    str(api['POLICY_RECEIPT']): hashlib.sha256(prepared[0].receipt).hexdigest(),
                }}, sort_keys=True).encode()
                pending = stack.enter_context(api['candidate'](api['READY'], data, 0o644))
                readiness.append((pending, data))
            def admit():
                if verified_source is not None:
                    verified_source['check_bound']()
                prepared[0].validate()
                api['validate_candidate'](*readiness[0], 0o644)
                if before_publish is not None:
                    before_publish()
            options = {'verified_source': verified_source} if verified_source is not None else {}
            ownership = {}
            browser = staging()['staged'](api['ROOT'], prepare=prepare, before_publish=admit, ownership=ownership, **options)
            # Separate nesting keeps policy recovery inside browser recovery.
            with browser:
                prepared[0].tree_check = ownership['verify']
                def files_check():
                    if before_publish is not None:
                        before_publish.validate()
                    pending, data = readiness[0]
                    fs = staging()
                    binding = api['CANDIDATE_BINDINGS'][pending]
                    if os.path.lexists(pending):
                        if os.path.lexists(api['READY']):
                            raise ValueError('manual-recovery:ready-conflict')
                        fs['check_binding'](pending, binding, data)
                    else:
                        fs['check_binding'](api['READY'], binding, data)
                prepared[0].files_check = files_check
                if verified_source is not None:
                    prepared[0].final_recheck = verified_source['final_recheck']
                    prepared[0].residue = ownership['residue']
                # READY is non-admitting metadata until its bound policy receipt
                # exists. That receipt is the sole final success commit.
                pending, ready_data = readiness[0]
                fs = staging()
                fs['commit_bound'](pending, api['READY'], api['CANDIDATE_BINDINGS'][pending], ready_data)
                api['sync_parent'](api['READY'])
                with prepared[0]() as verify:
                    verify.pending_ready = readiness[0][0]
                    yield verify
                ownership['success'] = True
                if before_publish is not None:
                    before_publish.success = True
        else:
            with policy_transaction(api, action) as publish:
                with publish():
                    yield


@contextmanager
def policy_transaction(api, action, prepared=None):
    """First install only. Retain all artifacts on failure; never undo kernel work."""
    if action != 'install' or prepared is None:
        raise ValueError('manual-review:first-install-only')
    fs = staging()
    if not fs['noreplace_support']():
        raise ValueError('manual-review:noreplace-unsupported')
    new = validate_identity(prepared)
    data = profile(new)
    receipt = json.dumps(receipt_record(new), sort_keys=True).encode()

    def absent(path):
        try:
            path.lstat()
        except FileNotFoundError:
            return
        raise ValueError('manual-review:first-install-only:' + path.name)

    def kernel_absent():
        # Failure to read is not absence. Include legacy attachments/conflicts.
        rows = kernel_entries()
        if any(attach == new['executable'] or any(c in attach for c in '*?[]{}^\\@')
               for _, _, attach in rows):
            raise ValueError('manual-recovery:kernel-conflict')
        lines = diagnostic_read(PROFILES).decode('ascii').splitlines()
        if any('ermis-epoptia-login-chromium' in line or new['executable'] in line or
               '/ms-playwright/' in line for line in lines):
            raise ValueError('manual-recovery:kernel-conflict')

    absent(PROFILE)
    try:
        stale = diagnostic_read(RECEIPT, 0, 0o644, 16384)
    except FileNotFoundError:
        stale = None
    stale_binding = fs['object_binding'](RECEIPT, stale) if stale is not None else None
    def receipt_unchanged():
        if stale_binding is None:
            absent(RECEIPT)
        else:
            fs['check_binding'](RECEIPT, stale_binding, stale)

    kernel_absent()
    diagnostic_parents(Path(PARSER))
    parser_fd = fs['open_path'](Path(PARSER))
    try:
        fs['caps'](parser_fd)
    finally:
        os.close(parser_fd)
    info = Path(PARSER).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o111:
        raise ValueError('manual-review:parser')
    for target in (PROFILE, RECEIPT):
        api['prepare_parent'](target, create=True)
    with ExitStack() as stack:
        pending = stack.enter_context(api['candidate'](PROFILE, data, 0o644))
        pending_receipt = stack.enter_context(api['candidate'](RECEIPT, receipt, 0o644))
        profile_binding = fs['object_binding'](pending, data)
        receipt_binding = fs['object_binding'](pending_receipt, receipt)
        parser('validate', pending)
        def validate():
            fs['check_binding'](pending, profile_binding, data)
            fs['check_binding'](pending_receipt, receipt_binding, receipt)
            absent(PROFILE)
            receipt_unchanged()
            kernel_absent()
        validate()
        @contextmanager
        def publish():
            boundary = False
            phase = 'filesystem-verification'
            try:
                validate()
                target = fs['BASE'] / ('chromium-' + new['revision'])
                publish.tree_check()
                tree_binding = fs['object_binding'](target)
                if tree_binding[1][0] != new:
                    raise ValueError('manual-recovery:tree-content')
                phase = 'profile-commit'
                fs['commit_bound'](pending, PROFILE, profile_binding, data)
                api['sync_parent'](PROFILE)
                archive = None
                if stale_binding is not None:
                    import uuid
                    archive = RECEIPT.with_name('.ermis-login-archive-' + uuid.uuid4().hex)
                    phase = 'stale-receipt-archive'
                    fs['commit_bound'](RECEIPT, archive, stale_binding, stale)
                    api['sync_parent'](archive)
                def verify_files():
                    publish.files_check()
                    publish.tree_check()
                    fs['check_binding'](target, tree_binding)
                    fs['check_binding'](PROFILE, profile_binding, data)
                    fs['check_binding'](pending_receipt, receipt_binding, receipt)
                    if archive is not None:
                        fs['check_binding'](archive, stale_binding, stale)
                    absent(RECEIPT)
                verify_files()
                # Parse the exact committed candidate, then rebind every file.
                parser('validate', PROFILE)
                verify_files()
                kernel_absent()
                # install() still holds its exclusive lock. Only local phase
                # assignments may follow this fresh full gate before add-only load.
                if publish.final_recheck is None:
                    raise ValueError('final-gate-required')
                publish.final_recheck({
                    'verify': verify_files,
                    'residue': (*publish.residue, pending_receipt,
                                *((archive,) if archive is not None else ())),
                })
                boundary = True
                phase = 'kernel-add'
                outcome = parser('load', PROFILE)
                if type(outcome) is not int or outcome != 0:
                    raise ValueError('kernel-add-unproven')
                phase = 'postload-verification'
                loaded(new)
                verify_files()
                def verify():
                    nonlocal phase
                    phase = 'final-verification'
                    verify_files()
                    loaded(new)
                yield verify
                verify()
                verify_files()
                phase = 'receipt-commit'
                # All checks precede this NOREPLACE rename. No post-rename read,
                # fsync, callback or verification can turn success into failure.
                fs['rename_absent'](pending_receipt, RECEIPT)
            except BaseException as error:
                boundary_name = 'after-kernel-boundary' if boundary else 'before-kernel-boundary'
                raise ValueError('manual-recovery:' + boundary_name + ':' + phase) from error
        publish.final_recheck = None
        publish.residue = ()
        publish.tree_check = lambda: None
        publish.files_check = lambda: None
        publish.validate = validate
        publish.receipt = receipt
        yield publish
