"""In-memory kernel fixtures only: never read host policy or invoke commands."""
from contextlib import ExitStack
import io
import errno
import inspect
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from admin_bootstrap import login_diagnose as d


def kernel_fixture(raw, rows, stream_factory=None, close_error=None, metadata=None, record_reader=None):
    info = SimpleNamespace(**{key: 0 for key in ('st_dev', 'st_ino', 'st_gid',
        'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_atime_ns')},
        st_mode=stat.S_IFREG | 0o444, st_uid=0, st_nlink=1)
    class Entries:
        def __enter__(self):
            return (SimpleNamespace(name=str(i)) for i in range(len(rows)))
        def __exit__(self, *args):
            pass
    def read(path, **kwargs):
        row = rows[int(path.parent.name)]
        return (row[('name', 'mode', 'attach').index(path.name)] + '\n').encode()
    stream = None
    def opened(*args, **kwargs):
        nonlocal stream
        assert args == (d.PROFILES,) and kwargs == {'terminal_flags': d.KERNEL_FLAGS}
        stream = stream_factory() if stream_factory else io.BytesIO(raw)
        return 123
    def direct_read(fd, size):
        assert fd == 123 and 0 < size <= 65536
        return stream.read(size)
    with ExitStack() as stack:
        for obj, name, kwargs in (
            (d, 'opened', dict(side_effect=opened)),
            (d.os, 'fstat', dict(side_effect=metadata) if metadata else dict(return_value=info)),
            (d.Path, 'lstat', dict(side_effect=AssertionError('post-open path lookup'))),
            (d.os, 'read', dict(side_effect=direct_read)),
            (d.os, 'fdopen', dict(side_effect=AssertionError('buffering forbidden'))),
            (d.os, 'pread', dict(side_effect=AssertionError('pread forbidden'))),
            (d.os, 'lseek', dict(side_effect=AssertionError('seek forbidden'))),
            (d.os, 'close', dict(side_effect=close_error)),
            (d.os, 'scandir', dict(side_effect=lambda *a: Entries())),
            (d, 'read', dict(side_effect=record_reader or read)),
            (d.subprocess, 'run', dict(side_effect=AssertionError('unexpected command'))),
        ):
            stack.enter_context(patch.object(obj, name, **kwargs))
        return d.kernel_probe()


def os_flags_forbidden():
    return d.os.O_NONBLOCK | d.os.O_PATH


class StreamingKernel(unittest.TestCase):
    def test_thirteen_policy_reader_guards_have_bounded_details(self):
        original_read = d.read
        info = dict(st_dev=1, st_ino=2, st_gid=0, st_size=0,
                    st_mtime_ns=0, st_ctime_ns=0, st_atime_ns=0,
                    st_mode=stat.S_IFREG | 0o444, st_uid=0, st_nlink=1)
        cases = (
            ('pre-type', 'before', {'st_mode': stat.S_IFDIR | 0o444}, 'fstat-before', 'type'),
            ('links', 'before', {'st_nlink': 2}, 'fstat-before', 'invalid'),
            ('owner', 'before', {'st_uid': 12345}, 'fstat-before', 'permission'),
            ('suid', 'before', {'st_mode': stat.S_IFREG | 0o4444}, 'fstat-before', 'permission'),
            ('sgid', 'before', {'st_mode': stat.S_IFREG | 0o2444}, 'fstat-before', 'permission'),
            ('sticky', 'before', {'st_mode': stat.S_IFREG | 0o1444}, 'fstat-before', 'permission'),
            ('group-write', 'before', {'st_mode': stat.S_IFREG | 0o464}, 'fstat-before', 'permission'),
            ('other-write', 'before', {'st_mode': stat.S_IFREG | 0o446}, 'fstat-before', 'permission'),
            ('post-type', 'after', {'st_mode': stat.S_IFDIR | 0o444}, 'fstat-after', 'type'),
            ('device', 'after', {'st_dev': 12345}, 'identity', 'identity'),
            ('inode', 'after', {'st_ino': 12345}, 'identity', 'identity'),
            ('metadata', 'after', {'st_mtime_ns': 12345}, 'identity', 'identity'),
            ('path-object', 'path', {'st_ino': 12345}, 'identity', 'identity'),
        )
        self.assertEqual(len(cases), 13)
        for label, location, change, stage, category in cases:
            with self.subTest(guard=label):
                before, after, path_info = [SimpleNamespace(**info) for _ in range(3)]
                vars({'before': before, 'after': after, 'path': path_info}[location]).update(change)
                def record_read(path, **kwargs):
                    # Run the real guard sequence entirely against in-memory IO.
                    with patch.object(d, 'opened', return_value=456), \
                            patch.object(d.os, 'fstat', side_effect=[before, after]) as stats, \
                            patch.object(d.os, 'fdopen', return_value=io.BytesIO(b'PRIVATE synthetic record')) as opened, \
                            patch.object(d.Path, 'lstat', return_value=path_info) as path_stat:
                        try:
                            return original_read(path, **kwargs)
                        finally:
                            self.assertEqual(stats.call_count, 1 if location == 'before' else 2)
                            self.assertEqual(opened.call_count, 0 if location == 'before' else 1)
                            self.assertEqual(path_stat.call_count, 1 if location == 'path' else 0)
                result = kernel_fixture(b'', [('PRIVATE', 'enforce', 'none')], record_reader=record_read)
                self.assertEqual(result, ('unknown', 'read-error'))
                details = d.kernel_read_details(result)
                self.assertEqual(details, {'kernel-read-stage': stage, 'kernel-read-category': category})
                self.assertIn(stage, d.KERNEL_READ_STAGES)
                self.assertIn(category, d.KERNEL_READ_CATEGORIES)
                public = repr((tuple(result), details))
                self.assertNotRegex(public, r'PRIVATE|synthetic|12345|st_|/|[0-9]|[a-f0-9]{40}')

    def test_generic_policy_validation_and_helper_contract(self):
        def invalid_record(*args, **kwargs):
            raise ValueError('PRIVATE /synthetic/record 12345')
        result = kernel_fixture(b'', [('PRIVATE', 'enforce', 'none')], record_reader=invalid_record)
        self.assertEqual(result, ('unknown', 'read-error'))
        self.assertEqual(d.kernel_read_details(result),
                         {'kernel-read-stage': 'parse', 'kernel-read-category': 'invalid'})
        with patch.object(d, 'kernel_summary', side_effect=ValueError('PRIVATE /synthetic 12345')):
            result = d.kernel_probe()
        self.assertEqual(result, ('unknown', 'read-error'))
        self.assertEqual(d.kernel_read_details(result),
                         {'kernel-read-stage': 'helper', 'kernel-read-category': 'invalid'})
        # Old tuple and three-argument result fixtures retain their exact shape.
        for result in (('unknown', 'read-error'), d.KernelProbeResult('unknown', 'read-error', 'unknown')):
            self.assertEqual(d.kernel_read_details(result),
                             {'kernel-read-stage': 'unknown', 'kernel-read-category': 'unknown'})

    def test_chunk_sizes_short_reads_and_eof(self):
        raw = b'private synthetic (enforce)\nsecond (complain)\n'
        expected = [('private synthetic', 'enforce'), ('second', 'complain')]
        for size in (1, 7, 4096, 65536):
            for short in (1, 7, size):
                stream = io.BytesIO(raw)
                calls = []
                def reading(fd, requested):
                    self.assertEqual((fd, requested), (123, size))
                    chunk = stream.read(min(short, requested))
                    calls.append(chunk)
                    return chunk
                with patch.object(d, 'KERNEL_CHUNK', size), patch.object(d.os, 'read', side_effect=reading):
                    self.assertEqual(list(d.kernel_lines(123)), expected)
                self.assertEqual(calls[-1], b'')
                self.assertNotIn(b'', calls[:-1])
        with patch.object(d.os, 'read', return_value=b'') as read:
            self.assertEqual(list(d.kernel_lines(123)), [])
            read.assert_called_once_with(123, d.KERNEL_CHUNK)

    def test_errno_sanitized_before_cleanup(self):
        for symbol in ('EAGAIN', 'EBADF', 'EIO', 'ENOMEM', 'EFAULT'):
            for chunks in ([], [b'private (enforce)\n', b'partial-secret']):
                events = chunks + [OSError(getattr(errno, symbol), 'PRIVATE /secret profile')]
                class Stream:
                    def read(self, size):
                        event = events.pop(0)
                        if isinstance(event, Exception):
                            raise event
                        return event
                result = kernel_fixture(b'', [], Stream, close_error=OSError(errno.EBADF, 'cleanup'))
                self.assertEqual(result, ('unknown', 'profiles-read-error-' + symbol))
                self.assertFalse(events)
        with patch.object(d.os, 'read', side_effect=[OSError(errno.EINTR, 'private'),
                b'x (en', OSError(errno.EINTR, 'private'), b'force)\n', b'']) as read:
            self.assertEqual(list(d.kernel_lines(123)), [('x', 'enforce')])
            self.assertEqual(read.call_count, 5)
        with patch.object(d.os, 'read', side_effect=OSError(99999, 'private')):
            with self.assertRaisesRegex(d.KernelReadError, '^profiles-read-error-OTHER$'):
                list(d.kernel_lines(123))

    def test_policy_record_errno_is_separate(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0, st_dev=1, st_ino=2)
        for symbol in ('EAGAIN', 'EBADF', 'EIO', 'ENOMEM', 'EFAULT'):
            with patch.object(d, 'opened', return_value=123), \
                    patch.object(d.os, 'fstat', return_value=info), \
                    patch.object(d.os, 'read', return_value=b''), \
                    patch.object(d.os, 'close'), \
                    patch.object(d.os, 'scandir', side_effect=OSError(getattr(errno, symbol), 'private')):
                self.assertEqual(d.kernel_probe(), ('unknown', 'policy-record-read-error-' + symbol))

    def test_failure_never_requires_eof(self):
        for raw, reason in ((b'bad\n', 'invalid-line'),
                (b'x' * (d.KERNEL_LINE_BYTES + 1), 'per-line-limit')):
            with patch.object(d.os, 'read', side_effect=[raw, AssertionError('must stop')]) as read:
                with self.assertRaisesRegex(d.KernelReadError, '^' + reason + '$'):
                    list(d.kernel_lines(123))
                self.assertEqual(read.call_count, 1)
        with patch.object(d.os, 'read', side_effect=[b'x (enforce)', OSError(errno.EIO, 'private')]):
            with self.assertRaisesRegex(d.KernelReadError, '^profiles-read-error-EIO$'):
                list(d.kernel_lines(123))

    def test_large_listing_and_matches(self):
        rows = [(f'synthetic.snap.application.{i:06d}.worker', 'enforce', 'none') for i in range(24000)]
        exact = (d.expected_profile(), 'unconfined', d.PINNED['executable'])
        for extra, expected in (([], 'absent'), ([exact], 'present-exact'),
                ([(str(d.BASE) + '/old', 'enforce', 'none')], 'present-conflict'),
                ([('synthetic', 'enforce', d.PINNED['executable'])], 'present-conflict')):
            records = rows + extra
            raw = ''.join(f'{name} ({mode})\n' for name, mode, _ in records).encode()
            self.assertGreater(len(raw), 1048576)
            self.assertLess(len(raw), d.KERNEL_BYTES)
            self.assertEqual(kernel_fixture(raw, records)[0], expected)

    def test_limits_and_boundaries(self):
        raw = b'synthetic (enforce)\n'
        for key, limit, reason in (('KERNEL_BYTES', len(raw)-1, 'total-byte-limit'),
                ('KERNEL_LINES', 0, 'line-count-limit'),
                ('KERNEL_LINE_BYTES', len(raw)-1, 'per-line-limit')):
            with patch.object(d, key, limit):
                self.assertEqual(kernel_fixture(raw, []), ('unknown', reason))
        with patch.object(d, 'KERNEL_BYTES', len(raw)), patch.object(d, 'KERNEL_LINES', 1), \
                patch.object(d, 'KERNEL_LINE_BYTES', len(raw)):
            self.assertEqual(kernel_fixture(raw, [('synthetic', 'enforce', 'none')])[0], 'absent')
        self.assertGreaterEqual(d.KERNEL_BYTES, 16 * 1024 * 1024)
        self.assertGreaterEqual(d.KERNEL_LINES, 131072)

    def test_actual_hard_limits(self):
        # Consume without retaining records; isolate each independent hard bound.
        class Repeated:
            def __init__(self, line, count):
                self.line, self.count = line, count
            def read(self, size):
                self.count -= 1
                return self.line[:size] if self.count >= 0 else b''
        for stream, reason in ((Repeated(b'x (enforce)\n', d.KERNEL_LINES + 1), 'line-count-limit'),
                (Repeated(b'x' * 1000 + b' (enforce)\n', 40000), 'total-byte-limit'),
                (Repeated(b'x' * (d.KERNEL_LINE_BYTES + 1), 1), 'per-line-limit')):
            with patch.object(d.os, 'read', side_effect=lambda fd, size: stream.read(size)), self.assertRaises(d.KernelReadError) as caught:
                for _ in d.kernel_lines(123):
                    pass
            self.assertEqual(str(caught.exception), reason)

    def test_strict_errors_and_duplicates(self):
        for raw, reason in ((b'x (future)\n', 'invalid-mode'), (b'x\x00 (enforce)\n', 'invalid-nul'),
                (b'\xff (enforce)\n', 'invalid-encoding'), (b'x (enforce)', 'invalid-line'),
                (b'x (enforce)\r', 'invalid-line'), (b'\n', 'invalid-line'),
                (b'x (enforce) junk\n', 'invalid-line')):
            self.assertEqual(kernel_fixture(raw, []), ('unknown', reason))
        self.assertEqual(kernel_fixture(b'', []), ('absent', 'complete-listing'))
        self.assertEqual(kernel_fixture(b'x (enforce)\nx (enforce)\n', []), ('ambiguous', 'duplicate-name'))
        self.assertEqual(kernel_fixture(b'x (enforce)\ny (enforce)\n', [('x', 'enforce', 'none')]*2),
                         ('ambiguous', 'duplicate-name'))

    def test_read_and_close_failures(self):
        class Broken(io.BytesIO):
            def read(self, size):
                if self.tell():
                    raise OSError(errno.EIO, 'private fixture')
                return super().read(size)
        self.assertEqual(kernel_fixture(b'', [], lambda: Broken(b'x (enforce)\n')), ('unknown', 'profiles-read-error-EIO'))
        self.assertEqual(kernel_fixture(b'', [], close_error=OSError('private fixture')), ('unknown', 'fd-close-error-OTHER'))

    def test_late_failure_and_bounded_reads(self):
        class Bounded(io.BytesIO):
            def read(self, size):
                if size != d.KERNEL_CHUNK:
                    raise AssertionError('unbounded line read')
                return super().read(size)
        row = (d.expected_profile(), 'unconfined', d.PINNED['executable'])
        valid = (row[0] + ' (unconfined)\n').encode()
        self.assertEqual(kernel_fixture(b'', [row], lambda: Bounded(valid))[0], 'present-exact')
        self.assertEqual(kernel_fixture(b'', [row], lambda: Bounded(valid + b'invalid\n')),
                         ('unknown', 'invalid-line'))
        with patch.object(d, 'KERNEL_BYTES', len(valid)):
            self.assertEqual(kernel_fixture(b'', [row], lambda: Bounded(valid + b'x (enforce)\n')),
                             ('unknown', 'total-byte-limit'))
        self.assertEqual(kernel_fixture(b'', [row], lambda: Bounded(valid), close_error=OSError('private')),
                         ('unknown', 'fd-close-error-OTHER'))

    def test_overflow_guards_and_fixed_storage(self):
        limit = (1 << 64) - 1
        self.assertEqual(d.kernel_count(limit - 1, 1, limit, 'limit'), limit)
        for value, increment in ((limit, 1), (limit+1, 0), (-1, 1), (0, -1), (0, 1 << 100)):
            with self.assertRaises(d.KernelReadError):
                d.kernel_count(value, increment, limit, 'limit')
        summary = d.KernelSummary()
        size = len(summary.table)
        for i in range(100):
            summary.add((f'synthetic-{i}', 'enforce'))
        self.assertEqual(len(summary.table), size)
        self.assertFalse(any(isinstance(v, (list, dict, set, str)) for v in vars(summary).values()))

    def test_listing_consistency(self):
        self.assertEqual(kernel_fixture(b'x (enforce)\n', []), ('unknown', 'listing-mismatch'))
        with patch.object(d, 'kernel_summary', side_effect=[(0, 0, 0, ('absent', 'complete-listing')), (1, 1, 1, ('absent', 'complete-listing'))]):
            self.assertEqual(d.kernel_probe(), ('ambiguous', 'listing-changed'))

    def test_virtual_metadata_and_fd_identity(self):
        before = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o444,
            st_uid=0, st_size=0, st_mtime_ns=0, st_ctime_ns=0, st_nlink=1)
        after = SimpleNamespace(**dict(vars(before), st_size=900, st_mtime_ns=8,
            st_ctime_ns=9, st_nlink=0))
        raw, rows = b'synthetic (enforce)\n', [('synthetic', 'enforce', 'none')]
        self.assertEqual(kernel_fixture(raw, rows, metadata=[before, after]*2),
                         ('absent', 'complete-listing'))
        for change in ({'st_ino': 3}, {'st_dev': 2}, {'st_mode': stat.S_IFDIR | 0o444}):
            changed = SimpleNamespace(**dict(vars(after), **change))
            self.assertEqual(kernel_fixture(raw, rows, metadata=[before, changed]),
                             ('unknown', 'fd-identity-error'))
        for change, reason in (({'st_uid': 1}, 'owner-error'),
                ({'st_mode': stat.S_IFIFO | 0o444}, 'type-error'),
                ({'st_mode': stat.S_IFREG | 0o666}, 'permission-error')):
            changed = SimpleNamespace(**dict(vars(before), **change))
            self.assertEqual(kernel_fixture(raw, rows, metadata=[changed]), ('unknown', reason))
        self.assertEqual(kernel_fixture(raw, rows, metadata=[OSError(errno.EIO, 'private')]),
                         ('unknown', 'stat-error-EIO'))

    def test_open_and_helper_errors(self):
        for error, reason in ((PermissionError(), 'open-permission-denied'),
                (OSError(errno.EIO, 'private'), 'open-error'),
                (d.InvariantError('unsafe-link'), 'open-symlink-error'),
                (d.InvariantError('owner'), 'open-owner-error')):
            with patch.object(d, 'opened', side_effect=error):
                self.assertEqual(d.kernel_probe(), ('unknown', reason))
        with patch.object(d, 'opened', side_effect=FileNotFoundError()):
            for rc in (0, 2, 23):
                with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=rc)):
                    self.assertEqual(d.kernel_probe(), ('probe-error', 'subsystem-status'))
            with patch.object(d.subprocess, 'run', side_effect=OSError(errno.EIO, 'private')):
                self.assertEqual(d.kernel_probe(), ('probe-error', 'status-tool-error'))

    def test_no_follow_path_substitution(self):
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o444)
        link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        for observed in (regular, link):
            with patch.object(d.os, 'fstat', return_value=directory), \
                    patch.object(d.os, 'stat', return_value=observed) as precheck, \
                    patch.object(d.os, 'open', side_effect=[123, OSError(errno.ELOOP, 'private')]) as opening, \
                    patch.object(d.os, 'close'):
                with self.assertRaises((OSError, d.InvariantError)):
                    d.opened(d.Path('/synthetic'), terminal_flags=d.KERNEL_FLAGS)
                self.assertFalse(precheck.call_args.kwargs['follow_symlinks'])
                if observed is regular:
                    flags = opening.call_args.args[1]
                    self.assertEqual(flags & os_flags_forbidden(), 0)
                    self.assertEqual(flags & d.os.O_ACCMODE, d.os.O_RDONLY)
                    self.assertEqual(flags & (d.os.O_NOFOLLOW | d.os.O_CLOEXEC),
                                     d.os.O_NOFOLLOW | d.os.O_CLOEXEC)

    def test_concurrent_profile_and_add_only_contract(self):
        from admin_bootstrap import login_bootstrap as login, login_apparmor as aa
        # An intervening profile changes the second full observation: never absent.
        with patch.object(d, 'kernel_summary', side_effect=[
                (0, 0, 0, ('absent', 'complete-listing')),
                (1, 1, 1, ('present-conflict', 'related-identity-or-attachment'))]):
            self.assertEqual(d.kernel_probe(), ('ambiguous', 'listing-changed'))
        install = inspect.getsource(login.install)
        self.assertLess(install.index('fcntl.flock(lock, fcntl.LOCK_EX'),
                        install.index('verified_source = atomic_preflight(files)'))
        self.assertLess(install.index('verified_source = atomic_preflight(files)'),
                        install.index('prepare_parent(path, create='))
        transaction = inspect.getsource(aa.policy_transaction)
        publish = transaction[transaction.index('def publish():'):]
        self.assertLess(publish.index('validate()'), publish.index("fs['commit_bound']"))
        self.assertIn('kernel_absent()', transaction[transaction.index('def validate():'):])
        parser = inspect.getsource(aa.parser)
        self.assertIn("'load': ['--add']", parser)
        self.assertNotIn('--replace', parser)

        # A concurrent add rejected by the kernel cannot become a successful load.
        for rc in (1, 23, None):
            with patch.object(aa.subprocess, 'run', return_value=SimpleNamespace(returncode=rc)) as run:
                with self.assertRaisesRegex(ValueError, '^kernel-add-unproven$'):
                    aa.parser('load', d.Path('/synthetic'))
                self.assertIn('--add', run.call_args.args[0])
                self.assertNotIn('--replace', run.call_args.args[0])
