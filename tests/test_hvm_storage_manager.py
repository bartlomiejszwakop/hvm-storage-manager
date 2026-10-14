"""Testy jednostkowe hvm_storage_manager.py — tylko stdlib (unittest + unittest.mock).

Uruchomienie z katalogu projektu:
    python -m unittest discover -s tests -v

Testy nie wykonują żadnych prawdziwych poleceń LVM/mount: warstwa `run` albo
`subprocess.run` jest zawsze podmieniana. Ścieżki tymczasowe są przekazywane
w postaci POSIX (bez litery dysku), dzięki czemu kod oparty o `posixpath`
działa także na Windows.
"""
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hvm_storage_manager as m  # noqa: E402

GIB = 1024 ** 3
UUID = '0f3c2a1b-1234-4abc-8def-0123456789ab'


def quiet():
    """Wycisza stdout (program dużo wypisuje)."""
    return contextlib.redirect_stdout(io.StringIO())


def posix_path(p):
    """C:\\Users\\x -> /Users/x (na Windows ścieżka bez litery dysku odnosi się
    do dysku bieżącego katalogu). Na POSIX zwraca ścieżkę bez zmian."""
    p = os.path.abspath(p)
    drive, rest = os.path.splitdrive(p)
    if drive:
        cwd_drive = os.path.splitdrive(os.getcwd())[0]
        if drive.lower() != cwd_drive.lower():
            raise unittest.SkipTest('katalog tymczasowy jest na innym dysku niż bieżący katalog')
    return rest.replace('\\', '/')


def make_tmpdir(testcase):
    tmp = tempfile.mkdtemp(prefix='hvmtest-')
    testcase.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    return tmp


def vgs_json(*vgs):
    return json.dumps({'report': [{'vg': [
        {'vg_name': n, 'vg_size': str(size), 'vg_free': str(free)} for n, size, free in vgs]}]})


def lvs_json(*lvs):
    return json.dumps({'report': [{'lv': [{'lv_name': lv, 'vg_name': vg} for vg, lv in lvs]}]})


def cp(args, rc=0, out='', errt=''):
    return subprocess.CompletedProcess(list(args), rc, out, errt)


# --- czyste funkcje walidujące ------------------------------------------------------

class VerifyNameTests(unittest.TestCase):
    def test_valid(self):
        for name in ('vm-storage', 'a', 'A', 'vm_data', 'x+y.z-1', 'Snapshot', 'a' * 64):
            with self.subTest(name=name):
                self.assertTrue(m.verify_name(name))

    def test_reserved(self):
        for name in ('snapshot', 'pvmove', '.', '..', 'my_rimage', 'x_tmeta1', 'a_cdata',
                     'data_mlog', 'v_vorigin', 'z_pmspare'):
            with self.subTest(name=name):
                self.assertFalse(m.verify_name(name))

    def test_length(self):
        self.assertTrue(m.verify_name('a' * 64))
        self.assertFalse(m.verify_name('a' * 65))
        self.assertFalse(m.verify_name(''))

    def test_bad_chars_and_start(self):
        for name in ('1abc', '-a', '_a', '.a', '+a', 'a b', 'a/b', 'a\\b', 'zażółć', 'ab\n', 'a:b',
                     'a$b', 'a;rm'):
            with self.subTest(name=name):
                self.assertFalse(m.verify_name(name))

    def test_non_string(self):
        for value in (None, 123, b'abc', ['a']):
            with self.subTest(value=value):
                self.assertFalse(m.verify_name(value))


class NormalizeMountTests(unittest.TestCase):
    def test_normalizes(self):
        cases = {
            '/vm-storage': '/vm-storage',
            '/vm-storage/': '/vm-storage',
            '//x': '/x',
            '///srv//pool/': '/srv/pool',
            '/srv/./pool': '/srv/pool',
            '  /vm  ': '/vm',  # brzegowe białe znaki są obcinane
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(m.normalize_mount(raw), expected)

    def test_rejects(self):
        for raw in ('vm-storage', 'srv/x', '', '   ', None, 5, '/', '//', '///', '/./',
                    '/srv/../etc', '/..', '/a/..', '/vm data', '/vm\tdata', '/vm\x01', '/vm\x1b[0m',
                    '/vm\\data', '\\vm', '/vm\u00a0x'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    m.normalize_mount(raw)


class MountAllowedTests(unittest.TestCase):
    def test_allowed(self):
        for p in ('/vm', '/vm-storage', '/vm_data', '/vm2', '/srv/vm-storage', '/data/pool_1',
                  '/var/lib/libvirt/images/pula1'):
            with self.subTest(p=p):
                self.assertTrue(m.mount_allowed(p))

    def test_rejected(self):
        for p in ('/etc/x', '/boot/x', '/srv/a/b', '/srv', '/data', '/data/x/y', '/home/vm',
                  '/vmA', '/VM', '/srv/Pool', '/var/lib/libvirt/images', '/usr', '/', '/vm/sub',
                  '/srv/x\n', '/var/lib/libvirt/images/a/b'):
            with self.subTest(p=p):
                self.assertFalse(m.mount_allowed(p))

    def test_full_pipeline_rejects_traversal_attempts(self):
        for raw in ('/srv/../etc', '/vm/../../boot'):
            with self.assertRaises(ValueError):
                m.mount_allowed(m.normalize_mount(raw))


class FstabParsingTests(unittest.TestCase):
    TEXT = (
        '# /etc/fstab: static file system information.\n'
        '#UUID=11111111-2222-3333-4444-555555555555 /vm-old ext4 defaults 0 2\n'
        '\n'
        '   \n'
        '   # wcięty komentarz /indented\n'
        '/dev/mapper/vg0-root / ext4 errors=remount-ro 0 1\n'
        'UUID=AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE\t/srv/tabbed\text4\tdefaults\t0\t2\n'
        '  /dev/sdb1   /vm\\040data   ext4 defaults 0 2\n'
        '/dev/sdc1 /data/slash/ ext4 defaults 0 2\n'
        '/dev/disk/by-uuid/99999999-8888-7777-6666-555555555555 /data/byuuid ext4 defaults 0 2\n'
        'UUID="12345678-90ab-cdef-1234-567890abcdef" /data/quoted ext4 defaults 0 2\n'
        'PARTUUID=deadbeef-0000-1111-2222-333333333333 /data/part ext4 defaults 0 2\n'
        'onlyonefield\n'
        'none swap swap sw 0 0\n'
    )

    def test_has_mount(self):
        for mount in ('/', '/srv/tabbed', '/vm data', '/data/slash', '/data/slash/', '/data/byuuid'):
            with self.subTest(mount=mount):
                self.assertTrue(m.fstab_has_mount(self.TEXT, mount))

    def test_has_mount_negative(self):
        for mount in ('/vm-old', '/indented', '/vm\\040data', '/vm', '/srv', '/data',
                      'onlyonefield', '/srv/tabbed2'):
            with self.subTest(mount=mount):
                self.assertFalse(m.fstab_has_mount(self.TEXT, mount))

    def test_has_uuid(self):
        for uuid in ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',      # wpis wielkimi literami
                     'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE',
                     '99999999-8888-7777-6666-555555555555',      # /dev/disk/by-uuid/
                     '12345678-90AB-CDEF-1234-567890ABCDEF'):     # UUID="..."
            with self.subTest(uuid=uuid):
                self.assertTrue(m.fstab_has_uuid(self.TEXT, uuid))

    def test_has_uuid_negative(self):
        for uuid in ('11111111-2222-3333-4444-555555555555',      # tylko w komentarzu
                     'deadbeef-0000-1111-2222-333333333333',      # PARTUUID to nie UUID
                     UUID):
            with self.subTest(uuid=uuid):
                self.assertFalse(m.fstab_has_uuid(self.TEXT, uuid))

    def test_empty(self):
        self.assertFalse(m.fstab_has_mount('', '/vm'))
        self.assertFalse(m.fstab_has_uuid('', UUID))

    def test_fstab_line_format(self):
        line = m.fstab_line(UUID, '/vm-storage')
        self.assertEqual(line, f'UUID={UUID} /vm-storage ext4 defaults,nofail 0 2\n')
        self.assertTrue(m.fstab_has_uuid(line, UUID))
        self.assertTrue(m.fstab_has_mount(line, '/vm-storage'))


class SizeTests(unittest.TestCase):
    def test_size_ok_boundaries(self):
        self.assertTrue(m.size_ok(1, 2 * GIB))
        self.assertFalse(m.size_ok(1, 2 * GIB - 1))
        self.assertFalse(m.size_ok(0, 100 * GIB))
        self.assertFalse(m.size_ok(-1, 100 * GIB))
        self.assertTrue(m.size_ok(9, 10 * GIB))
        self.assertFalse(m.size_ok(10, 10 * GIB))
        self.assertTrue(m.size_ok(10, 10 * GIB, reserve_gib=0))
        self.assertFalse(m.size_ok(1, 0))

    def test_parse_size(self):
        self.assertEqual(m.parse_size('1'), 1)
        self.assertEqual(m.parse_size(' 42 '), 42)
        self.assertEqual(m.parse_size(1000000), 1000000)
        for bad in ('0', '1000001', '-1', '1.5', 'abc', '', '1e3', '0x10', '\uff15'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    m.parse_size(bad)


# --- odczyt LVM ---------------------------------------------------------------------

class LvmParsingTests(unittest.TestCase):
    def test_list_vgs(self):
        stdout = json.dumps({'report': [{'vg': [
            {'vg_name': 'ubuntu-vg', 'vg_size': '53687091200', 'vg_free': '10737418240'},
            {'vg_name': 'data', 'vg_size': ' 2147483648 ', 'vg_free': '<1073741824.00'},
            {'vg_name': 'full', 'vg_size': '4194304', 'vg_free': '0'},
        ]}], 'log': []})
        with mock.patch.object(m, 'run', return_value=cp([], 0, stdout)) as run:
            vgs = m.list_vgs()
        args = run.call_args[0][0]
        self.assertEqual(args[0], 'vgs')
        for flag in ('--reportformat', 'json', '--units', 'b', '--nosuffix'):
            self.assertIn(flag, args)
        self.assertEqual(vgs, [
            {'vg_name': 'ubuntu-vg', 'vg_size': 50 * GIB, 'vg_free': 10 * GIB},
            {'vg_name': 'data', 'vg_size': 2 * GIB, 'vg_free': GIB},
            {'vg_name': 'full', 'vg_size': 4194304, 'vg_free': 0},
        ])

    def test_list_vgs_empty(self):
        for stdout in ('', '{}', json.dumps({'report': [{'vg': []}]})):
            with mock.patch.object(m, 'run', return_value=cp([], 0, stdout)):
                self.assertEqual(m.list_vgs(), [])

    def test_list_vgs_propagates_failure(self):
        with mock.patch.object(m, 'run', side_effect=subprocess.CalledProcessError(5, ['vgs'])):
            with self.assertRaises(subprocess.CalledProcessError):
                m.list_vgs()

    def test_lv_exists(self):
        stdout = lvs_json(('vg0', 'root'), ('vg0', 'swap_1'), ('vg1', 'data'))
        with mock.patch.object(m, 'run', return_value=cp([], 0, stdout)):
            self.assertTrue(m.lv_exists('vg0', 'root'))
            self.assertTrue(m.lv_exists('vg1', 'data'))
            self.assertFalse(m.lv_exists('vg0', 'data'))   # inna grupa
            self.assertFalse(m.lv_exists('vg2', 'root'))
            self.assertFalse(m.lv_exists('vg0', 'roo'))

    def test_check_new_name(self):
        stdout = lvs_json(('vg0', 'root'))
        with mock.patch.object(m, 'run', return_value=cp([], 0, stdout)):
            self.assertEqual(m.check_new_name('vg0', 'new'), 'new')
            with self.assertRaises(ValueError):
                m.check_new_name('vg0', 'root')
            with self.assertRaises(ValueError):
                m.check_new_name('vg0', 'bad name')


# --- run() ---------------------------------------------------------------------------

class RunTests(unittest.TestCase):
    def test_dry_run_mutating_not_executed(self):
        with mock.patch.object(m, 'DRY_RUN', True), \
                mock.patch.object(m.subprocess, 'run') as sp, quiet() as out:
            p = m.run(['lvcreate', '-L', '1G', '-n', 'x', 'vg0'], mutating=True)
        sp.assert_not_called()
        self.assertEqual(p.returncode, 0)
        self.assertIn('[DRY-RUN] lvcreate -L 1G -n x vg0', out.getvalue())

    def test_dry_run_read_only_executed(self):
        with mock.patch.object(m, 'DRY_RUN', True), \
                mock.patch.object(m.subprocess, 'run', return_value=cp(['vgs'], 0, 'ok')) as sp:
            p = m.run(['vgs'])
        sp.assert_called_once()
        self.assertEqual(p.stdout, 'ok')

    def test_mutating_executed_without_dry_run(self):
        with mock.patch.object(m, 'DRY_RUN', False), \
                mock.patch.object(m.subprocess, 'run', return_value=cp([], 0)) as sp:
            m.run(['mount', '/vm'], mutating=True)
        sp.assert_called_once()

    def test_safe_environment_and_stdin(self):
        with mock.patch.object(m, 'DRY_RUN', False), \
                mock.patch.object(m.subprocess, 'run', return_value=cp([], 0)) as sp:
            m.run(['lsblk', 5])
        args, kwargs = sp.call_args
        self.assertEqual(args[0], ['lsblk', '5'])  # argumenty zamienione na str, bez powłoki
        self.assertIs(kwargs['env'], m.ENV)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', kwargs)

    def test_missing_binary_returns_127(self):
        with mock.patch.object(m.subprocess, 'run', side_effect=FileNotFoundError('no such file')):
            p = m.run(['nonexistent-tool'], check=False)
        self.assertEqual(p.returncode, 127)
        self.assertIn('no such file', p.stderr)

    def test_missing_binary_with_check_raises(self):
        with mock.patch.object(m.subprocess, 'run', side_effect=FileNotFoundError('x')):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                m.run(['nonexistent-tool'])
        self.assertEqual(ctx.exception.returncode, 127)

    def test_check_raises(self):
        with mock.patch.object(m.subprocess, 'run', return_value=cp(['false'], 3, 'o', 'boom')):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                m.run(['false'])
            self.assertEqual(m.run(['false'], check=False).returncode, 3)
        e = ctx.exception
        self.assertEqual((e.returncode, e.cmd, e.stderr), (3, ['false'], 'boom'))
        self.assertIn('boom', m._describe(e))


# --- zapis plików --------------------------------------------------------------------

class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(self)
        self.path = Path(self.tmp) / 'fstab'

    def test_replaces_content(self):
        self.path.write_bytes(b'old\n')
        with mock.patch.object(m, 'DRY_RUN', False):
            m.atomic_write(self.path, 'line1\nline2\n')
        self.assertEqual(self.path.read_bytes(), b'line1\nline2\n')
        self.assertEqual(os.listdir(self.tmp), ['fstab'])  # brak plików tymczasowych

    def test_creates_missing_file(self):
        with mock.patch.object(m, 'DRY_RUN', False):
            m.atomic_write(self.path, 'x\n')
        self.assertEqual(self.path.read_bytes(), b'x\n')
        self.assertEqual(os.listdir(self.tmp), ['fstab'])

    def test_non_utf8_bytes_preserved(self):
        raw = b'# komentarz \xff\xfe latin1 \xe9\n/dev/sda1 / ext4 defaults 0 1\n'
        self.path.write_bytes(raw)
        with mock.patch.object(m, 'FSTAB', self.path), mock.patch.object(m, 'DRY_RUN', False):
            text = m.read_fstab()
            m.atomic_write(self.path, text + 'nowa\n')
        self.assertEqual(self.path.read_bytes(), raw + b'nowa\n')

    def test_dry_run_does_not_write(self):
        self.path.write_bytes(b'old\n')
        with mock.patch.object(m, 'DRY_RUN', True), quiet():
            m.atomic_write(self.path, 'new\n')
        self.assertEqual(self.path.read_bytes(), b'old\n')
        self.assertEqual(os.listdir(self.tmp), ['fstab'])

    def test_failure_leaves_original_and_no_temp(self):
        self.path.write_bytes(b'old\n')
        with mock.patch.object(m, 'DRY_RUN', False), \
                mock.patch.object(m.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                m.atomic_write(self.path, 'new\n')
        self.assertEqual(self.path.read_bytes(), b'old\n')
        self.assertEqual(os.listdir(self.tmp), ['fstab'])

    @unittest.skipUnless(os.name == 'posix', 'bity uprawnień POSIX nie mają znaczenia na Windows')
    def test_mode_preserved(self):
        self.path.write_bytes(b'old\n')
        os.chmod(self.path, 0o640)
        with mock.patch.object(m, 'DRY_RUN', False):
            m.atomic_write(self.path, 'new\n')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)


# --- walidacja z I/O -------------------------------------------------------------------

class CheckMountTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(self)
        self.ptmp = posix_path(self.tmp)

    def test_nonexistent_ok(self):
        m.check_mount_target(self.ptmp + '/nope/deeper')
        self.assertEqual(m.missing_dirs(self.ptmp + '/nope/deeper'),
                         [self.ptmp + '/nope', self.ptmp + '/nope/deeper'])

    def test_empty_dir_ok(self):
        os.mkdir(os.path.join(self.tmp, 'empty'))
        m.check_mount_target(self.ptmp + '/empty')
        self.assertEqual(m.missing_dirs(self.ptmp + '/empty'), [])

    def test_non_empty_rejected(self):
        os.mkdir(os.path.join(self.tmp, 'full'))
        Path(self.tmp, 'full', 'disk.qcow2').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'nie jest pusty'):
            m.check_mount_target(self.ptmp + '/full')

    def test_file_rejected(self):
        Path(self.tmp, 'file').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'nie jest katalogiem'):
            m.check_mount_target(self.ptmp + '/file')

    def test_already_mountpoint_rejected(self):
        os.mkdir(os.path.join(self.tmp, 'mnt'))
        target = self.ptmp + '/mnt'
        with mock.patch.object(m.os.path, 'ismount', side_effect=lambda p: p == target):
            with self.assertRaisesRegex(ValueError, 'punktem montowania'):
                m.check_mount_target(target)

    def _symlink(self, src, dst):
        try:
            os.symlink(src, dst, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f'os.symlink niedostępne (Windows bez uprawnień/Developer Mode?): {exc}')

    def test_symlink_target_rejected(self):
        os.mkdir(os.path.join(self.tmp, 'real'))
        self._symlink(os.path.join(self.tmp, 'real'), os.path.join(self.tmp, 'link'))
        with self.assertRaisesRegex(ValueError, 'dowiązaniem'):
            m.check_mount_target(self.ptmp + '/link')

    def test_symlink_in_path_rejected(self):
        os.mkdir(os.path.join(self.tmp, 'real'))
        self._symlink(os.path.join(self.tmp, 'real'), os.path.join(self.tmp, 'link'))
        with self.assertRaisesRegex(ValueError, 'dowiązaniem'):
            m.check_mount_target(self.ptmp + '/link/new')


# --- transakcja i rollback ------------------------------------------------------------

class FakeSystem:
    """Udaje polecenia LVM/blkid/findmnt/mount. `fail` wybiera krok, który ma się nie udać:
    lvcreate, blkid_sig, mkfs, uuid, verify_after, mount, verify."""

    def __init__(self, fail=None, vg='vg0', free=50 * GIB, uuid=UUID, existing=(('vg0', 'root'),)):
        self.fail, self.vg, self.free, self.uuid, self.existing = fail, vg, free, uuid, existing
        self.calls = []
        self.verify_calls = 0

    def __call__(self, args, check=True, mutating=False):
        args = [str(a) for a in args]
        self.calls.append(args)
        rc, out = self._respond(args)
        if check and rc:
            raise subprocess.CalledProcessError(rc, args, out, 'symulowany błąd')
        return subprocess.CompletedProcess(args, rc, out, '')

    def _respond(self, a):
        cmd = a[0]
        lvpath = f'/dev/{self.vg}/'
        if cmd == 'vgs':
            return 0, vgs_json((self.vg, 100 * GIB, self.free))
        if cmd == 'lvs':
            return 0, lvs_json(*self.existing)
        if cmd == 'lvcreate':
            return (5, '') if self.fail == 'lvcreate' else (0, '')
        if cmd == 'mkfs.ext4':
            return (1, '') if self.fail == 'mkfs' else (0, '')
        if cmd == 'blkid' and '-s' in a:
            return 0, ('garbage' if self.fail == 'uuid' else self.uuid) + '\n'
        if cmd == 'blkid':
            return (0 if self.fail == 'blkid_sig' else 2), ''
        if cmd == 'findmnt' and '--verify' in a:
            self.verify_calls += 1
            if self.verify_calls >= 2 and self.fail == 'verify_after':
                return 1, '[E] unreachable source'
            return 0, ''
        if cmd == 'findmnt' and 'SOURCE' in a:
            return 0, ('/dev/sda1' if self.fail == 'verify' else lvpath + 't1') + '\n'
        if cmd == 'findmnt' and 'UUID' in a:
            return 0, ('' if self.fail == 'verify' else self.uuid) + '\n'
        if cmd == 'mount':
            return (32, '') if self.fail == 'mount' else (0, '')
        return 0, ''  # udevadm, systemctl, umount, lvremove, df

    def names(self):
        return [c[0] for c in self.calls]

    def index(self, pred):
        return next(i for i, c in enumerate(self.calls) if pred(c))


class TransactionTests(unittest.TestCase):
    ORIGINAL = (b'# /etc/fstab \xff niespodzianka spoza UTF-8\n'
                b'/dev/mapper/vg0-root / ext4 errors=remount-ro 0 1\n'
                b'UUID=11111111-2222-3333-4444-555555555555 /boot ext4 defaults 0 2')  # bez \n na końcu

    def setUp(self):
        self.tmp = make_tmpdir(self)
        self.fstab = Path(self.tmp) / 'fstab'
        self.fstab.write_bytes(self.ORIGINAL)
        self.root = os.path.join(self.tmp, 'vmroot')
        self.mount = posix_path(self.tmp) + '/vmroot/pool1'
        # mount_allowed: ścieżka tymczasowa nie pasuje do allowlisty (testowana osobno)
        patches = [mock.patch.object(m, 'FSTAB', self.fstab),
                   mock.patch.object(m, 'DRY_RUN', False),
                   mock.patch.object(m, 'mount_allowed', lambda p: True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def create(self, fake, assume_yes=True, answers=None):
        with mock.patch.object(m, 'run', fake), quiet() as out:
            if answers is None:
                with mock.patch('builtins.input', side_effect=AssertionError('input() nie powinno być wołane')):
                    result = m.create_volume('vg0', 't1', 2, self.mount, assume_yes=assume_yes)
            else:
                with mock.patch('builtins.input', side_effect=answers) as inp:
                    result = m.create_volume('vg0', 't1', 2, self.mount, assume_yes=assume_yes)
                self.assertEqual(inp.call_count, len(answers))
        self.output = out.getvalue()
        return result

    def backups(self):
        return [n for n in os.listdir(self.tmp) if n.startswith('fstab.backup-hvm-')]

    def test_happy_path(self):
        fake = FakeSystem()
        self.assertTrue(self.create(fake))
        expected_line = f'UUID={UUID} {self.mount} ext4 defaults,nofail 0 2\n'.encode()
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + expected_line)
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'pool1')))
        self.assertIn(['lvcreate', '-L', '2G', '-n', 't1', 'vg0'], fake.calls)
        self.assertIn(['mkfs.ext4', '-F', '-m', '1', '-L', 't1', '/dev/vg0/t1'], fake.calls)
        self.assertIn(['mount', self.mount], fake.calls)
        order = [
            fake.index(lambda c: c[0] == 'vgs'),
            fake.index(lambda c: c[0] == 'lvcreate'),
            fake.index(lambda c: c[:2] == ['blkid', '-p'] and len(c) == 3),
            fake.index(lambda c: c[0] == 'mkfs.ext4'),
            fake.index(lambda c: '-s' in c and c[0] == 'blkid'),
            fake.index(lambda c: c[0] == 'systemctl'),
            fake.index(lambda c: c[0] == 'mount'),
            fake.index(lambda c: c[0] == 'findmnt' and 'UUID' in c),
            fake.index(lambda c: c[0] == 'df'),
        ]
        self.assertEqual(order, sorted(order))
        verify_idx = [i for i, c in enumerate(fake.calls) if c[0] == 'findmnt' and '--verify' in c]
        self.assertEqual(len(verify_idx), 2)  # przed zmianą i po zapisie fstab
        self.assertLess(verify_idx[0], order[1])
        self.assertTrue(order[5] < verify_idx[1] < order[6])
        for forbidden in ('lvremove', 'umount'):
            self.assertNotIn(forbidden, fake.names())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual((Path(self.tmp) / self.backups()[0]).read_bytes(), self.ORIGINAL)

    def test_happy_path_with_confirmation_phrase(self):
        fake = FakeSystem()
        self.assertTrue(self.create(fake, assume_yes=False, answers=[m.CONFIRM_PHRASE]))
        self.assertIn('lvcreate', fake.names())

    def test_wrong_phrase_cancels(self):
        fake = FakeSystem()
        self.assertFalse(self.create(fake, assume_yes=False, answers=['tak']))
        self.assertNotIn('lvcreate', fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def _assert_rolled_back(self, fake, lv_created, mounted):
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
        self.assertFalse(os.path.exists(self.root), 'utworzone katalogi powinny zostać usunięte')
        self.assertEqual('lvremove' in fake.names(), lv_created)
        self.assertEqual('umount' in fake.names(), mounted)
        if lv_created:
            self.assertIn(['lvremove', '-y', '/dev/vg0/t1'], fake.calls)
        if mounted:
            self.assertLess(fake.index(lambda c: c[0] == 'umount'), fake.index(lambda c: c[0] == 'lvremove'))
        self.assertIn('Zmiany zostały wycofane' if lv_created else 'nic do wycofania', self.output)

    def test_failure_points_roll_back(self):
        # (krok, czy LV powstał, czy zamontowano)
        for step, lv_created, mounted in (('lvcreate', False, False),
                                          ('blkid_sig', True, False),
                                          ('mkfs', True, False),
                                          ('uuid', True, False),
                                          ('verify_after', True, False),
                                          ('mount', True, False),
                                          ('verify', True, True)):
            with self.subTest(step=step):
                self.fstab.write_bytes(self.ORIGINAL)
                fake = FakeSystem(fail=step)
                self.assertFalse(self.create(fake))
                self._assert_rolled_back(fake, lv_created, mounted)
                if step in ('verify_after', 'mount', 'verify'):
                    # daemon-reload po zapisie i ponownie po przywróceniu fstab
                    self.assertEqual(fake.names().count('systemctl'), 2)

    def test_rollback_prompt_accepts_default(self):
        fake = FakeSystem(fail='mount')
        self.assertFalse(self.create(fake, assume_yes=False, answers=[m.CONFIRM_PHRASE, '']))
        self._assert_rolled_back(fake, lv_created=True, mounted=False)

    def test_user_declines_rollback(self):
        fake = FakeSystem(fail='mount')
        self.assertFalse(self.create(fake, assume_yes=False, answers=[m.CONFIRM_PHRASE, 'n']))
        self.assertNotIn('lvremove', fake.names())
        self.assertNotIn('umount', fake.names())
        self.assertEqual(fake.names().count('systemctl'), 1)
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'pool1')))
        self.assertIn(f'UUID={UUID} {self.mount}'.encode(), self.fstab.read_bytes())
        self.assertIn('lvremove /dev/vg0/t1', self.output)  # instrukcje ręczne
        self.assertIn('Zmiany NIE zostały wycofane', self.output)

    def test_rollback_continues_after_failed_step(self):
        fake = FakeSystem(fail='verify')
        orig_call = fake.__call__

        def failing_umount(args, check=True, mutating=False):
            if args[0] == 'umount':
                fake.calls.append(list(args))
                raise subprocess.CalledProcessError(32, list(args), '', 'target is busy')
            return orig_call(args, check, mutating)
        with mock.patch.object(m, 'run', failing_umount), quiet() as out, \
                mock.patch('builtins.input', side_effect=AssertionError):
            self.assertFalse(m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True))
        text = out.getvalue()
        self.assertIn('Nie wszystkie zmiany udało się wycofać', text)
        self.assertIn('umount ' + self.mount, text)
        self.assertIn('lvremove', [c[0] for c in fake.calls])  # dalsze kroki wykonane mimo błędu
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def test_fstab_changed_during_run_aborts(self):
        fake = FakeSystem()
        orig_call = fake.__call__

        def sneaky(args, check=True, mutating=False):
            if args[0] == 'mkfs.ext4':  # ktoś dopisał ten sam punkt montowania w trakcie
                with open(self.fstab, 'ab') as f:
                    f.write(f'\n/dev/sdz1 {self.mount} ext4 defaults 0 2\n'.encode())
            return orig_call(args, check, mutating)
        with mock.patch.object(m, 'run', sneaky), quiet(), mock.patch('builtins.input', side_effect=AssertionError):
            self.assertFalse(m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True))
        self.assertIn('lvremove', [c[0] for c in fake.calls])
        self.assertNotIn('mount', [c[0] for c in fake.calls])
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(self.backups(), [])

    def test_validation_rejections(self):
        cases = [
            ('existing LV', dict(existing=(('vg0', 't1'),))),
            ('no space', dict(free=2 * GIB)),       # 2 GiB + 1 GiB rezerwy > 2 GiB
            ('missing VG', dict(vg='other')),
        ]
        for label, fake_kwargs in cases:
            with self.subTest(label=label):
                fake = FakeSystem(**fake_kwargs)
                self.assertFalse(self.create(fake))
                self.assertNotIn('lvcreate', fake.names())
                self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def test_validation_rejects_broken_fstab(self):
        fake = FakeSystem()

        def broken(args, check=True, mutating=False):
            if args[0] == 'findmnt' and '--verify' in args:
                fake.calls.append(list(args))
                return subprocess.CompletedProcess(args, 1, '', '')
            return fake(args, check, mutating)
        with mock.patch.object(m, 'run', broken), quiet() as out:
            self.assertFalse(m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True))
        self.assertIn('findmnt --verify', out.getvalue())
        self.assertNotIn('lvcreate', fake.names())

    def test_validation_rejects_mount_already_in_fstab(self):
        self.fstab.write_bytes(self.ORIGINAL + f'\n/dev/sdb1 {self.mount}/ xfs defaults 0 0\n'.encode())
        fake = FakeSystem()
        self.assertFalse(self.create(fake))
        self.assertNotIn('lvcreate', fake.names())

    def test_validation_rejects_non_empty_target(self):
        os.makedirs(os.path.join(self.root, 'pool1'))
        Path(self.root, 'pool1', 'vm.img').write_bytes(b'x')
        fake = FakeSystem()
        self.assertFalse(self.create(fake))
        self.assertNotIn('lvcreate', fake.names())
        self.assertTrue(os.path.exists(os.path.join(self.root, 'pool1', 'vm.img')))

    def test_validation_rejects_disallowed_mount(self):
        fake = FakeSystem()
        with mock.patch.object(m, 'mount_allowed', lambda p: False):
            self.assertFalse(self.create(fake))
        self.assertIn('nie jest na liście dozwolonych', self.output)
        self.assertNotIn('lvcreate', fake.names())

    def test_dry_run_end_to_end_changes_nothing(self):
        """Prawdziwe run() w trybie dry-run; subprocess.run obsługuje tylko odczyty."""
        fake = FakeSystem()
        executed = []

        def sp_run(args, **kwargs):
            executed.append(args)
            rc, out = fake._respond(args)
            return subprocess.CompletedProcess(args, rc, out, '')
        with mock.patch.object(m, 'DRY_RUN', True), mock.patch.object(m.subprocess, 'run', sp_run), \
                mock.patch('builtins.input', side_effect=AssertionError), quiet() as out:
            self.assertTrue(m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True))
        names = [a[0] for a in executed]
        self.assertEqual(sorted(set(names)), ['findmnt', 'lvs', 'vgs'])
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(self.backups(), [])
        text = out.getvalue()
        for expected in ('[DRY-RUN] lvcreate -L 2G -n t1 vg0', '[DRY-RUN] mkfs.ext4',
                         f'[DRY-RUN] mkdir {self.mount}', f'[DRY-RUN] mount {self.mount}',
                         '[DRY-RUN] zapis'):
            self.assertIn(expected, text)


class RollbackUnitTests(unittest.TestCase):
    def test_empty_undo(self):
        with quiet():
            self.assertTrue(m.rollback([]))

    def test_reverse_order_and_clear(self):
        done = []
        undo = [(f's{i}', f'manual{i}', (lambda i=i: done.append(i))) for i in range(3)]
        with quiet():
            self.assertTrue(m.rollback(undo, assume_yes=True))
        self.assertEqual(done, [2, 1, 0])
        self.assertEqual(undo, [])

    def test_eof_on_prompt_means_rollback(self):
        done = []
        with quiet(), mock.patch('builtins.input', side_effect=EOFError):
            self.assertTrue(m.rollback([('a', 'b', lambda: done.append(1))]))
        self.assertEqual(done, [1])

    def test_decline_variants(self):
        for answer in ('n', 'N', 'nie', ' no '):
            done = []
            with self.subTest(answer=answer), quiet(), mock.patch('builtins.input', return_value=answer):
                self.assertFalse(m.rollback([('a', 'b', lambda: done.append(1))]))
            self.assertEqual(done, [])


# --- main() ------------------------------------------------------------------------------

class MainTests(unittest.TestCase):
    CREATE = ['create', '--vg', 'vg0', '--name', 't1', '--size', '2', '--mount', '/vm-t1', '--yes']

    def setUp(self):
        self.tmp = make_tmpdir(self)
        self.logfile = os.path.join(self.tmp, 'test.log')
        p = mock.patch.object(m, 'DRY_RUN', False)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._reset_logging)

    @staticmethod
    def _reset_logging():
        for h in list(m.log.handlers):
            m.log.removeHandler(h)
            h.close()
        m.log.addHandler(logging.NullHandler())

    def test_version(self):
        with quiet() as out, self.assertRaises(SystemExit) as ctx:
            m.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(m.__version__, out.getvalue())
        self.assertEqual(m.__version__, '0.2.0')

    def test_non_root_without_dry_run(self):
        with mock.patch.object(m, 'is_root', return_value=False), \
                mock.patch.object(m, 'create_volume') as cv, quiet() as out:
            self.assertEqual(m.main(['--log-file', self.logfile] + self.CREATE), 1)
        cv.assert_not_called()
        self.assertIn('administratora', out.getvalue())

    def test_argparse_errors_exit_2(self):
        for argv in (['create'], ['create', '--vg', 'v', '--name', 'n', '--size', 'abc', '--mount', '/vm'],
                     ['--bogus'], ['delete']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), quiet(), \
                    self.assertRaises(SystemExit) as ctx:
                m.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_dry_run_flag_positions(self):
        for argv in (['--dry-run', '--log-file', self.logfile] + self.CREATE,
                     ['--log-file', self.logfile] + self.CREATE + ['--dry-run']):
            seen = {}

            def fake_create(*args):
                seen['dry'] = m.DRY_RUN
                seen['args'] = args
                return True
            with self.subTest(argv=argv), mock.patch.object(m, 'is_root', return_value=False), \
                    mock.patch.object(m, 'acquire_lock', return_value=True), \
                    mock.patch.object(m, 'create_volume', side_effect=fake_create), quiet():
                self.assertEqual(m.main(argv), 0)
            self.assertTrue(seen['dry'])
            self.assertEqual(seen['args'], ('vg0', 't1', 2, '/vm-t1', True))
            self._reset_logging()

    def test_create_failure_returns_1_and_lock_busy(self):
        with mock.patch.object(m, 'is_root', return_value=True), \
                mock.patch.object(m, 'acquire_lock', return_value=True), \
                mock.patch.object(m, 'create_volume', return_value=False), quiet():
            self.assertEqual(m.main(['--log-file', self.logfile] + self.CREATE), 1)
        self._reset_logging()
        with mock.patch.object(m, 'is_root', return_value=True), \
                mock.patch.object(m, 'acquire_lock', return_value=False), \
                mock.patch.object(m, 'create_volume') as cv, quiet():
            self.assertEqual(m.main(['--log-file', self.logfile] + self.CREATE), 1)
        cv.assert_not_called()


class RegressionTests(unittest.TestCase):
    def test_reserved_prefixes_and_new_suffixes(self):
        for bad in ('snapshot1', 'pvmove_x', 'a_cpool', 'a_cvol', 'a_imeta', 'a_iorig', 'a_wcorig'):
            self.assertFalse(m.verify_name(bad), bad)

    def test_vg_name_rules(self):
        for good in ('ubuntu-vg', '1vg', '_vg', 'vg.data'):
            self.assertTrue(m.verify_vg_name(good), good)
        for bad in ('-vg', '.', '..', 'vg/x', 'vg x', ''):
            self.assertFalse(m.verify_vg_name(bad), bad)

    def test_restore_keeps_crlf_bytes(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        raw = b'a / ext4 defaults 0 1\r\n# x\r\n'
        fstab, backup = d / 'fstab', d / 'backup'
        fstab.write_bytes(raw); backup.write_bytes(raw)
        with mock.patch.object(m, 'FSTAB', fstab), mock.patch.object(m, 'run'):
            m.atomic_write(fstab, m.read_fstab() + 'UUID=x /vm ext4 defaults 0 2\n')
            self.assertTrue(fstab.read_bytes().startswith(raw))
            m.restore_fstab(backup, '')
        self.assertEqual(fstab.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
