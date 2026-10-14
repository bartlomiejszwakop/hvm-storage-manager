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
import stat
import subprocess
import sys
import tempfile
import types
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
    """lvs: krotki (vg, lv) albo (vg, lv, lv_attr)."""
    return json.dumps({'report': [{'lv': [
        {'lv_name': lv[1], 'vg_name': lv[0], 'lv_attr': lv[2] if len(lv) > 2 else '-wi-a-----',
         'lv_size': str(GIB)} for lv in lvs]}]})


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


# --- ścieżki urządzeń i konflikt /dev ----------------------------------------------------

class DevPathsTests(unittest.TestCase):
    def test_dm_name(self):
        self.assertEqual(m.dm_name('ubuntu-vg', 'vm-storage'), 'ubuntu--vg-vm--storage')
        self.assertEqual(m.dm_name('vg0', 't1'), 'vg0-t1')
        self.assertEqual(m.dm_name('a--b', 'c-'), 'a----b-c--')
        self.assertEqual(m.dm_name('vg_x.y', 'lv+1'), 'vg_x.y-lv+1')

    def test_dev_paths(self):
        with mock.patch.object(m, 'DEV_ROOT', '/dev'):
            self.assertEqual(m.dev_paths('ubuntu-vg', 'vm-storage'),
                             ('/dev/ubuntu-vg/vm-storage', '/dev/mapper/ubuntu--vg-vm--storage'))
        with mock.patch.object(m, 'DEV_ROOT', '/tmp/fake'):
            self.assertEqual(m.dev_paths('vg0', 't1'), ('/tmp/fake/vg0/t1', '/tmp/fake/mapper/vg0-t1'))


def fake_stat(mode):
    return lambda p: types.SimpleNamespace(st_mode=mode, st_rdev=0)


class DevConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(self)
        self.lvs = lvs_json(('vg0', 'root'))
        self.run = mock.Mock(side_effect=lambda *a, **k: cp([], 0, self.lvs))
        for p in (mock.patch.object(m, 'DEV_ROOT', posix_path(self.tmp)), mock.patch.object(m, 'DRY_RUN', False),
                  mock.patch.object(m, 'run', self.run)):
            p.start()
            self.addCleanup(p.stop)
        self.lvdir = os.path.join(self.tmp, 'vg0', 'vm-storage')

    def check(self, answers=None, **kw):
        side = answers if answers is not None else AssertionError('input() nie powinno być wołane')
        exc = None
        with quiet() as out, mock.patch('builtins.input', side_effect=side):
            try:
                m.check_dev_conflict('vg0', 'vm-storage', **kw)
            except ValueError as e:
                exc = e
        self.out = out.getvalue()
        return exc

    def test_free_paths_ok(self):
        self.assertIsNone(self.check())

    def test_empty_dir_confirmed_is_removed(self):
        os.makedirs(self.lvdir)
        self.assertIsNone(self.check(['t']))
        self.assertFalse(os.path.exists(self.lvdir))
        self.assertIn('device not cleared', self.out)
        self.assertIn('Usunięto pusty katalog', self.out)

    def test_empty_dir_declined_default_no(self):
        for answer in ('', 'n', 'nie', 'TAK!'):
            os.makedirs(self.lvdir, exist_ok=True)
            with self.subTest(answer=answer):
                self.assertIsNotNone(self.check([answer]))
                self.assertTrue(os.path.isdir(self.lvdir))

    def test_empty_dir_yes_mode_refuses(self):
        os.makedirs(self.lvdir)
        exc = self.check(assume_yes=True)
        self.assertIn('--fix-dev-conflict', str(exc))
        self.assertTrue(os.path.isdir(self.lvdir))

    def test_empty_dir_yes_with_fix_flag(self):
        os.makedirs(self.lvdir)
        self.assertIsNone(self.check(assume_yes=True, fix=True))
        self.assertFalse(os.path.exists(self.lvdir))

    def test_non_empty_dir_refused(self):
        os.makedirs(self.lvdir)
        Path(self.lvdir, 'x').write_bytes(b'x')
        for kw in ({}, {'assume_yes': True, 'fix': True}):
            exc = self.check(**kw)
            self.assertIn('niepusty', str(exc))
        self.assertTrue(os.path.exists(os.path.join(self.lvdir, 'x')))

    def test_mountpoint_dir_refused(self):
        os.makedirs(self.lvdir)
        with mock.patch.object(m.os.path, 'ismount', side_effect=lambda p: p.endswith('/vm-storage')):
            exc = self.check(fix=True)
        self.assertIn('punktem montowania', str(exc))
        self.assertTrue(os.path.isdir(self.lvdir))

    def test_regular_file_refused(self):
        os.makedirs(os.path.dirname(self.lvdir))
        Path(self.lvdir).write_bytes(b'')
        self.assertIn('zwykły plik', str(self.check(fix=True)))
        self.assertTrue(os.path.isfile(self.lvdir))

    def test_stale_symlink_refused(self):
        with mock.patch.object(m.os, 'lstat', fake_stat(stat.S_IFLNK | 0o777)), \
                mock.patch.object(m.os, 'readlink', return_value='../dm-7'):
            exc = self.check(fix=True)
        self.assertIn('../dm-7', str(exc))
        self.assertIn('nieaktualny', str(exc))

    def test_stale_block_device_refused(self):
        with mock.patch.object(m.os, 'lstat', fake_stat(stat.S_IFBLK | 0o660)):
            exc = self.check(fix=True)
        self.assertIn('urządzenie blokowe', str(exc))

    def test_mapper_path_conflict(self):
        os.makedirs(os.path.join(self.tmp, 'mapper'))
        Path(self.tmp, 'mapper', 'vg0-vm--storage').write_bytes(b'')
        exc = self.check()
        self.assertIn('mapper/vg0-vm--storage', str(exc))

    def test_mapper_empty_dir_also_needs_confirmation(self):
        os.makedirs(os.path.join(self.tmp, 'mapper', 'vg0-vm--storage'))
        self.assertIsNotNone(self.check(assume_yes=True))
        self.assertIsNone(self.check(['t']))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'mapper', 'vg0-vm--storage')))

    def test_existing_lv_skips_checks(self):
        self.lvs = lvs_json(('vg0', 'vm-storage'))
        os.makedirs(self.lvdir)
        self.assertIsNone(self.check())
        self.assertTrue(os.path.isdir(self.lvdir))

    def test_dry_run_does_not_remove(self):
        os.makedirs(self.lvdir)
        with mock.patch.object(m, 'DRY_RUN', True):
            self.assertIsNone(self.check(['t']))
        self.assertTrue(os.path.isdir(self.lvdir))
        self.assertIn('[DRY-RUN] rmdir', self.out)

    def test_reverified_after_rmdir(self):
        os.makedirs(self.lvdir)
        with mock.patch.object(m, 'remove_dir'):  # „rmdir” bez efektu → katalog nadal jest
            exc = self.check(['t'])
        self.assertIn('nadal istnieje', str(exc))


class WaitDeviceTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(m, 'DEV_ROOT', '/dev')
        p.start()
        self.addCleanup(p.stop)
        self.mapper = '/dev/mapper/vg0-t1'

    def test_mapper_present(self):
        run = mock.Mock(return_value=cp([], 0))
        with mock.patch.object(m, 'run', run), mock.patch.object(m, '_is_block', lambda p: p == self.mapper):
            self.assertEqual(m.wait_device('vg0', 't1'), self.mapper)
        self.assertEqual(run.call_args_list, [mock.call(['udevadm', 'settle', '--timeout=30'], check=False)])

    def test_vgscan_fallback(self):
        state = {'ready': False}

        def fake_run(args, check=True, mutating=False):
            if args[0] == 'vgscan':
                state['ready'] = True
            return cp(args, 0)
        run = mock.Mock(side_effect=fake_run)
        with mock.patch.object(m, 'run', run), quiet(), \
                mock.patch.object(m, '_is_block', lambda p: state['ready'] and p == self.mapper):
            self.assertEqual(m.wait_device('vg0', 't1'), self.mapper)
        self.assertIn(mock.call(['vgscan', '--mknodes'], check=False, mutating=True), run.call_args_list)

    def test_missing_raises(self):
        with mock.patch.object(m, 'run', return_value=cp([], 0, '', 'boom')), quiet(), \
                mock.patch.object(m, '_is_block', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'nie pojawiło się'):
                m.wait_device('vg0', 't1')


class SignatureTests(unittest.TestCase):
    def detect(self, wipefs=(0, ''), blkid=(2, '')):
        def fake(args, check=True, mutating=False):
            rc, out = wipefs if args[0] == 'wipefs' else blkid
            return cp(args, rc, out)
        with mock.patch.object(m, 'run', side_effect=fake) as run:
            result = m.detect_signature('/dev/x')
        for c in run.call_args_list:  # wyłącznie odczyt
            self.assertNotIn('-a', c[0][0])
            self.assertFalse(c[1].get('mutating'))
        return result[0]

    def test_cases(self):
        self.assertEqual(self.detect(), 'empty')
        self.assertEqual(self.detect((0, 'ext4\n'), (0, f'TYPE=ext4\nUUID={UUID}\n')), 'ext4')
        self.assertEqual(self.detect((0, 'xfs\n'), (0, 'TYPE=xfs\n')), 'other')
        self.assertEqual(self.detect((0, 'gpt\nPMBR\n'), (0, 'PTTYPE=gpt\n')), 'other')
        self.assertEqual(self.detect((0, 'ext4\nLVM2_member\n'), (0, 'TYPE=ext4\n')), 'other')
        self.assertEqual(self.detect((0, ''), (8, '')), 'other')       # niejednoznaczne
        self.assertEqual(self.detect((0, 'swap\n'), (2, '')), 'other')  # wipefs widzi, blkid nie

    def test_wipefs_failure_raises(self):
        with mock.patch.object(m, 'run', return_value=cp([], 1, '', 'Permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                m.detect_signature('/dev/x')


class FstabCheckTests(unittest.TestCase):
    BASE = '/dev/mapper/vg0-root / ext4 defaults 0 1\n'

    def test_missing(self):
        self.assertEqual(m.fstab_check(self.BASE, '/vm-storage', UUID), 'missing')
        self.assertEqual(m.fstab_check(self.BASE, '/vm-storage', None), 'missing')
        self.assertEqual(m.fstab_check(self.BASE + f'#UUID={UUID} /vm-storage ext4 d 0 2\n', '/vm-storage', UUID),
                         'missing')

    def test_ok(self):
        for src in (f'UUID={UUID}', f'UUID={UUID.upper()}', f'UUID="{UUID}"', f'/dev/disk/by-uuid/{UUID}'):
            with self.subTest(src=src):
                text = self.BASE + f'{src} /vm-storage/ ext4 defaults,nofail 0 2\n'
                self.assertEqual(m.fstab_check(text, '/vm-storage', UUID), 'ok')

    def test_conflicts(self):
        other = '11111111-2222-3333-4444-555555555555'
        cases = {
            'inne źródło': f'UUID={other} /vm-storage ext4 defaults 0 2\n',
            'urządzenie zamiast UUID': '/dev/mapper/vg0-vm--storage /vm-storage ext4 defaults 0 2\n',
            'duplikat': f'UUID={UUID} /vm-storage ext4 defaults 0 2\nUUID={UUID} /vm-storage ext4 defaults 0 2\n',
            'UUID gdzie indziej': f'UUID={UUID} /srv/other ext4 defaults 0 2\n',
            'UUID w dwóch miejscach': f'UUID={UUID} /vm-storage ext4 defaults 0 2\nUUID={UUID} /srv/x ext4 d 0 2\n',
        }
        for label, extra in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, 'ręczny przegląd'):
                    m.fstab_check(self.BASE + extra, '/vm-storage', UUID)
        with self.assertRaises(ValueError):  # pusty LV, a punkt już w fstab
            m.fstab_check(self.BASE + f'UUID={UUID} /vm-storage ext4 d 0 2\n', '/vm-storage', None)


# --- transakcja i rollback ------------------------------------------------------------

class FakeSystem:
    """Stanowa atrapa poleceń LVM/blkid/wipefs/findmnt/mount. `fail` (napis lub krotka) wybiera
    kroki, które mają się nie udać: lvcreate, lvcreate_partial, blkid_sig, mkfs, uuid,
    verify_after, mount, verify, lvremove."""
    last = None

    def __init__(self, fail=None, vg='vg0', free=50 * GIB, uuid=UUID, existing=(('vg0', 'root'),),
                 lv='t1', fs=None, mounted=None, attr=None):
        self.fails = set(fail if isinstance(fail, (tuple, list, set)) else [fail])
        self.vg, self.free, self.uuid, self.lv = vg, free, uuid, lv
        self.lvs = [tuple(x) for x in existing]
        self.attr = dict(attr or {})                         # (vg, lv) -> lv_attr
        self.blockdevs = {m.dev_paths(v, n)[1] for v, n in self.lvs}
        self.fs = dict(fs or {})                             # urządzenie -> ext4 / xfs / gpt...
        self.mounted = dict(mounted or {})                   # punkt montowania -> urządzenie
        self.calls = []
        self.verify_calls = 0
        FakeSystem.last = self

    def __call__(self, args, check=True, mutating=False):
        args = [str(a) for a in args]
        self.calls.append(args)
        rc, out = self._respond(args)
        if check and rc:
            raise subprocess.CalledProcessError(rc, args, out, 'symulowany błąd')
        return subprocess.CompletedProcess(args, rc, out, '')

    def _respond(self, a):
        cmd, last, f = a[0], a[-1], self.fails
        if cmd == 'vgs':
            return 0, vgs_json((self.vg, 100 * GIB, self.free))
        if cmd == 'lvs':
            return 0, lvs_json(*[(v, n, self.attr.get((v, n), '-wi-a-----')) for v, n in self.lvs])
        if cmd == 'lvcreate':
            if 'lvcreate' in f:
                return 5, ''
            self.lvs.append((last, a[a.index('-n') + 1]))
            self.blockdevs.add(m.dev_paths(last, a[a.index('-n') + 1])[1])
            return (5, '') if 'lvcreate_partial' in f else (0, '')
        if cmd == 'lvremove':
            if 'lvremove' in f:
                return 5, ''
            v, n = last.split('/')[-2:]
            self.lvs.remove((v, n))
            self.blockdevs.discard(m.dev_paths(v, n)[1])
            return 0, ''
        if cmd == 'mkfs.ext4':
            if 'mkfs' in f:
                return 1, ''
            self.fs[last] = 'ext4'
            return 0, ''
        if cmd == 'wipefs':
            return 0, (self.fs[last] + '\n') if last in self.fs else ''
        if cmd == 'blkid':
            fs = self.fs.get(last)
            if 'UUID' in a:
                return 0, ('garbage' if 'uuid' in f else self.uuid) + '\n'
            if 'TYPE' in a:
                return (0, fs + '\n') if fs else (2, '')
            if 'export' in a:
                if not fs:
                    return 2, ''
                return 0, 'PTTYPE=gpt\n' if fs == 'gpt' else f'TYPE={fs}\nUUID={self.uuid}\n'
            return (0 if 'blkid_sig' in f or fs else 2), ''
        if cmd == 'findmnt':
            if '--verify' in a:
                self.verify_calls += 1
                if self.verify_calls >= 2 and 'verify_after' in f:
                    return 1, '[E] unreachable source'
                return 0, ''
            if '-S' in a:
                targets = [t for t, d in self.mounted.items() if d == last]
                return (0 if targets else 1), '\n'.join(targets)
            if 'SOURCE' in a:
                return 0, ('/dev/sda1' if 'verify' in f else self.mounted.get(last, '')) + '\n'
            if 'UUID' in a:
                dev = self.mounted.get(last)
                return 0, (self.uuid if dev in self.fs and 'verify' not in f else '') + '\n'
            if '-M' in a:
                return (0 if last in self.mounted else 1), ''
            return 0, ''
        if cmd == 'mount':
            if 'mount' in f:
                return 32, ''
            self.mounted[last] = m.dev_paths(self.vg, self.lv)[1]
            return 0, ''
        if cmd == 'umount':
            self.mounted.pop(last, None)
            return 0, ''
        return 0, ''  # udevadm, systemctl, df, vgscan

    def names(self):
        return [c[0] for c in self.calls]

    def index(self, pred):
        return next(i for i, c in enumerate(self.calls) if pred(c))


class FakeEnvMixin:
    """Wspólne przygotowanie: tymczasowy fstab, DEV_ROOT, allowlista wyłączona, _is_block z atrapy."""
    ORIGINAL = (b'# /etc/fstab \xff niespodzianka spoza UTF-8\n'
                b'/dev/mapper/vg0-root / ext4 errors=remount-ro 0 1\n'
                b'UUID=11111111-2222-3333-4444-555555555555 /boot ext4 defaults 0 2')  # bez \n na końcu

    def setup_env(self):
        self.tmp = make_tmpdir(self)
        self.fstab = Path(self.tmp) / 'fstab'
        self.fstab.write_bytes(self.ORIGINAL)
        self.root = os.path.join(self.tmp, 'vmroot')
        self.mount = posix_path(self.tmp) + '/vmroot/pool1'
        FakeSystem.last = None
        # mount_allowed: ścieżka tymczasowa nie pasuje do allowlisty (testowana osobno)
        patches = [mock.patch.object(m, 'FSTAB', self.fstab),
                   mock.patch.object(m, 'DRY_RUN', False),
                   mock.patch.object(m, 'DEV_ROOT', posix_path(self.tmp) + '/dev'),
                   mock.patch.object(m, '_is_block', lambda p: FakeSystem.last is not None
                                     and p in FakeSystem.last.blockdevs),
                   mock.patch.object(m, 'mount_allowed', lambda p: True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def backups(self):
        return [n for n in os.listdir(self.tmp) if n.startswith('fstab.backup-hvm-')]

    def call(self, fake, fn, answers):
        with mock.patch.object(m, 'run', fake), quiet() as out:
            if answers is None:
                with mock.patch('builtins.input', side_effect=AssertionError('input() nie powinno być wołane')):
                    result = fn()
            else:
                with mock.patch('builtins.input', side_effect=answers) as inp:
                    result = fn()
                self.assertEqual(inp.call_count, len(answers))
        self.output = out.getvalue()
        return result


class TransactionTests(FakeEnvMixin, unittest.TestCase):
    def setUp(self):
        self.setup_env()
        self.mapper = m.dev_paths('vg0', 't1')[1]
        self.devdir = os.path.join(self.tmp, 'dev', 'vg0', 't1')

    def create(self, fake, assume_yes=True, answers=None, fix_dev=False):
        return self.call(fake, lambda: m.create_volume('vg0', 't1', 2, self.mount, assume_yes=assume_yes,
                                                       fix_dev=fix_dev), answers)

    def test_happy_path(self):
        fake = FakeSystem()
        self.assertTrue(self.create(fake))
        expected_line = f'UUID={UUID} {self.mount} ext4 defaults,nofail 0 2\n'.encode()
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + expected_line)
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'pool1')))
        self.assertIn(['lvcreate', '-L', '2G', '-n', 't1', 'vg0'], fake.calls)
        self.assertIn(['mkfs.ext4', '-F', '-m', '1', '-L', 't1', self.mapper], fake.calls)
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
        settle = fake.index(lambda c: c[0] == 'udevadm')
        self.assertTrue(order[1] < settle < order[2])  # czekanie na urządzenie po lvcreate
        verify_idx = [i for i, c in enumerate(fake.calls) if c[0] == 'findmnt' and '--verify' in c]
        self.assertEqual(len(verify_idx), 2)  # przed zmianą i po zapisie fstab
        self.assertLess(verify_idx[0], order[1])
        self.assertTrue(order[5] < verify_idx[1] < order[6])
        for forbidden in ('lvremove', 'umount', 'vgscan'):
            self.assertNotIn(forbidden, fake.names())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual((Path(self.tmp) / self.backups()[0]).read_bytes(), self.ORIGINAL)
        self.assertIn(f'findmnt -M {self.mount}', self.output)  # ostrzeżenie o nofail

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
        self.assertIn('Wszystkie zmiany wycofane — stan zweryfikowany.' if lv_created
                      else 'Nie wprowadzono żadnych zmian — stan zweryfikowany.', self.output)
        self.assertNotIn('RÓŻNI SIĘ', self.output)

    def test_failure_points_roll_back(self):
        # (krok, czy LV powstał, czy zamontowano)
        for step, lv_created, mounted in (('lvcreate', False, False),
                                          ('lvcreate_partial', True, False),
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
                self.assertNotIn(('vg0', 't1'), fake.lvs)
                if step in ('verify_after', 'mount', 'verify'):
                    # daemon-reload po zapisie i ponownie po przywróceniu fstab
                    self.assertEqual(fake.names().count('systemctl'), 2)

    def test_lvcreate_partial_failure_removes_lv(self):
        fake = FakeSystem(fail='lvcreate_partial')
        self.assertFalse(self.create(fake))
        self.assertIn('częściowo', self.output)
        self.assertNotIn('mkfs.ext4', fake.names())
        lvc = fake.index(lambda c: c[0] == 'lvcreate')
        self.assertEqual(fake.calls[lvc + 1][0], 'lvs')  # sprawdzenie LV zaraz po błędzie
        self.assertIn(['lvremove', '-y', '/dev/vg0/t1'], fake.calls)

    def test_lvcreate_failure_without_lv_verified_clean(self):
        fake = FakeSystem(fail='lvcreate')
        self.assertFalse(self.create(fake))
        self.assertNotIn('lvremove', fake.names())
        self.assertIn('Nie wprowadzono żadnych zmian — stan zweryfikowany.', self.output)
        self.assertNotIn('nic do wycofania', self.output)
        lvc = fake.index(lambda c: c[0] == 'lvcreate')
        self.assertIn('lvs', [c[0] for c in fake.calls[lvc:]])

    def test_leftover_dev_dir_after_failure_not_claimed_clean(self):
        """Odpowiednik incydentu: po nieudanym lvcreate w /dev zostaje katalog — diagnoza to wykrywa."""
        fake = FakeSystem(fail='lvcreate')
        orig = fake.__call__

        def lvcreate_leaves_dir(args, check=True, mutating=False):
            if args[0] == 'lvcreate':
                os.makedirs(self.devdir)
            return orig(args, check, mutating)
        self.assertFalse(self.call(lvcreate_leaves_dir, lambda: m.create_volume('vg0', 't1', 2, self.mount,
                                                                                 assume_yes=True), None))
        self.assertNotIn('stan zweryfikowany', self.output)
        self.assertIn('RÓŻNI SIĘ', self.output)
        self.assertIn('ls -la', self.output)

    def test_failed_lvremove_reported_by_diagnosis(self):
        fake = FakeSystem(fail=('mkfs', 'lvremove'))
        self.assertFalse(self.create(fake))
        self.assertNotIn('stan zweryfikowany', self.output)
        self.assertIn('RÓŻNI SIĘ', self.output)
        self.assertIn('lvremove /dev/vg0/t1', self.output)

    def test_empty_dev_dir_with_yes_refuses(self):
        os.makedirs(self.devdir)
        fake = FakeSystem()
        self.assertFalse(self.create(fake))
        self.assertIn('--fix-dev-conflict', self.output)
        self.assertNotIn('lvcreate', fake.names())
        self.assertTrue(os.path.isdir(self.devdir))
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def test_empty_dev_dir_fix_flag(self):
        os.makedirs(self.devdir)
        fake = FakeSystem()
        self.assertTrue(self.create(fake, fix_dev=True))
        self.assertFalse(os.path.exists(self.devdir))
        self.assertIn('lvcreate', fake.names())

    def test_empty_dev_dir_interactive(self):
        os.makedirs(self.devdir)
        fake = FakeSystem()
        self.assertFalse(self.create(fake, assume_yes=False, answers=['']))  # domyślnie NIE
        self.assertTrue(os.path.isdir(self.devdir))
        self.assertNotIn('lvcreate', fake.names())
        fake = FakeSystem()
        self.assertTrue(self.create(fake, assume_yes=False, answers=['t', m.CONFIRM_PHRASE]))
        self.assertFalse(os.path.exists(self.devdir))
        self.assertIn('lvcreate', fake.names())

    def test_dev_dir_appearing_before_lvcreate_is_caught(self):
        fake = FakeSystem()
        orig = fake.__call__

        def sneaky(args, check=True, mutating=False):
            if args[0] == 'findmnt' and '-M' in args and '-o' not in args:  # snapshot tuż przed _execute
                os.makedirs(self.devdir, exist_ok=True)
            return orig(args, check, mutating)
        self.assertFalse(self.call(sneaky, lambda: m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True),
                                   None))
        self.assertNotIn('lvcreate', fake.names())
        self.assertIn('--fix-dev-conflict', self.output)
        self.assertIn('RÓŻNI SIĘ', self.output)  # katalog pojawił się w trakcie — zgłoszone, nie przemilczane

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
        self.assertIn('RÓŻNI SIĘ', self.output)
        self.assertNotIn('stan zweryfikowany', self.output)

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
        self.assertNotIn('stan zweryfikowany', text)  # punkt nadal zamontowany

    def test_fstab_changed_during_run_aborts(self):
        fake = FakeSystem()
        orig_call = fake.__call__

        def sneaky(args, check=True, mutating=False):
            if args[0] == 'mkfs.ext4':  # ktoś dopisał ten sam punkt montowania w trakcie
                with open(self.fstab, 'ab') as f:
                    f.write(f'\n/dev/sdz1 {self.mount} ext4 defaults 0 2\n'.encode())
            return orig_call(args, check, mutating)
        with mock.patch.object(m, 'run', sneaky), quiet() as out, \
                mock.patch('builtins.input', side_effect=AssertionError):
            self.assertFalse(m.create_volume('vg0', 't1', 2, self.mount, assume_yes=True))
        self.assertIn('lvremove', [c[0] for c in fake.calls])
        self.assertNotIn('mount', [c[0] for c in fake.calls])
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(self.backups(), [])
        self.assertIn('ZMIENIONY', out.getvalue())  # fstab zmieniony przez kogoś innego — uczciwie zgłoszone

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
        with quiet() as out:
            self.assertTrue(m.rollback([]))
        self.assertNotIn('nic do wycofania', out.getvalue())  # bez diagnozy nie twierdzimy, że stan jest czysty
        self.assertIn('sprawdzam', out.getvalue())

    def test_reverse_order_and_clear(self):
        done = []
        undo = [(f's{i}', f'manual{i}', (lambda i=i: done.append(i))) for i in range(3)]
        with quiet() as out:
            self.assertTrue(m.rollback(undo, assume_yes=True))
        self.assertEqual(done, [2, 1, 0])
        self.assertEqual(undo, [])
        self.assertNotIn('stan zweryfikowany', out.getvalue())

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


# --- resume: dokończenie konfiguracji istniejącego LV -----------------------------------------

class ResumeTests(FakeEnvMixin, unittest.TestCase):
    def setUp(self):
        self.setup_env()
        self.dev = m.dev_paths('vg0', 'r1')[1]
        self.line = m.fstab_line(UUID, self.mount).encode()

    def fake(self, **kw):
        kw.setdefault('existing', (('vg0', 'root'), ('vg0', 'r1')))
        return FakeSystem(lv='r1', **kw)

    def resume(self, fake, assume_yes=True, format_empty=False, answers=None):
        return self.call(fake, lambda: m.resume_volume('vg0', 'r1', self.mount, assume_yes=assume_yes,
                                                       format_empty=format_empty), answers)

    def assert_untouched(self, fake):
        for forbidden in ('mkfs.ext4', 'mount', 'umount', 'lvremove', 'lvcreate'):
            self.assertNotIn(forbidden, fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.root))

    def test_empty_lv_formats_after_phrase(self):
        fake = self.fake()
        self.assertTrue(self.resume(fake, assume_yes=False, answers=['t', 'FORMATUJ r1']))
        self.assertIn(['mkfs.ext4', '-F', '-m', '1', '-L', 'r1', self.dev], fake.calls)
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)
        self.assertEqual(fake.mounted.get(self.mount), self.dev)
        self.assertLess(fake.index(lambda c: c[0] == 'wipefs'), fake.index(lambda c: c[0] == 'mkfs.ext4'))
        for forbidden in ('lvremove', 'lvcreate', 'umount'):
            self.assertNotIn(forbidden, fake.names())
        self.assertIn('nofail', self.output)
        self.assertIn(f'findmnt -M {self.mount}', self.output)

    def test_empty_lv_wrong_phrase_or_decline(self):
        for answers in (['t', 'FORMATUJ'], ['t', 'formatuj r1'], ['']):
            with self.subTest(answers=answers):
                fake = self.fake()
                self.assertFalse(self.resume(fake, assume_yes=False, answers=answers))
                self.assert_untouched(fake)

    def test_empty_lv_yes_requires_format_empty(self):
        fake = self.fake()
        self.assertFalse(self.resume(fake))
        self.assertIn('--format-empty', self.output)
        self.assert_untouched(fake)

    def test_empty_lv_yes_with_format_empty(self):
        fake = self.fake()
        self.assertTrue(self.resume(fake, format_empty=True))
        self.assertIn('mkfs.ext4', fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)

    def test_ext4_skips_mkfs(self):
        fake = self.fake(fs={self.dev: 'ext4'})
        self.assertTrue(self.resume(fake))
        self.assertNotIn('mkfs.ext4', fake.names())
        self.assertIn('Wykryto ext4 — formatowanie pominięte', self.output)
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)
        self.assertIn(['mount', self.mount], fake.calls)
        self.assertEqual(len(self.backups()), 1)

    def test_other_signatures_refused(self):
        for fs in ('xfs', 'gpt', 'LVM2_member', 'crypto_LUKS'):
            with self.subTest(fs=fs):
                fake = self.fake(fs={self.dev: fs})
                self.assertFalse(self.resume(fake, format_empty=True))
                self.assert_untouched(fake)
                self.assertIn('odmowa', self.output)

    def test_already_mounted_and_fstab_ok_nothing_to_do(self):
        os.makedirs(os.path.join(self.root, 'pool1'))
        self.fstab.write_bytes(self.ORIGINAL + b'\n' + self.line)
        fake = self.fake(fs={self.dev: 'ext4'}, mounted={self.mount: self.dev},
                         attr={('vg0', 'r1'): '-wi-ao----'})  # otwarty, ale przez NASZ punkt montowania
        self.assertTrue(self.resume(fake, assume_yes=False))  # bez kroków = bez pytań
        self.assertIn('Nic do zrobienia', self.output)
        for forbidden in ('mkfs.ext4', 'mount', 'umount', 'systemctl'):
            self.assertNotIn(forbidden, fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)
        self.assertEqual(self.backups(), [])

    def test_mounted_correctly_adds_only_fstab(self):
        os.makedirs(os.path.join(self.root, 'pool1'))
        fake = self.fake(fs={self.dev: 'ext4'}, mounted={self.mount: self.dev}, attr={('vg0', 'r1'): '-wi-ao----'})
        self.assertTrue(self.resume(fake))
        self.assertNotIn('mount', fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)

    def test_mounted_other_device_refused(self):
        os.makedirs(os.path.join(self.root, 'pool1'))
        fake = self.fake(fs={self.dev: 'ext4'}, mounted={self.mount: '/dev/sdz1'})
        self.assertFalse(self.resume(fake))
        self.assertIn('INNE urządzenie', self.output)
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def test_lv_mounted_elsewhere_refused(self):
        fake = self.fake(fs={self.dev: 'ext4'}, mounted={'/mnt/other': self.dev}, attr={('vg0', 'r1'): '-wi-ao----'})
        self.assertFalse(self.resume(fake))
        self.assertIn('/mnt/other', self.output)
        self.assert_untouched(fake)

    def test_open_lv_not_mounted_refused(self):
        fake = self.fake(fs={self.dev: 'ext4'}, attr={('vg0', 'r1'): '-wi-ao----'})
        self.assertFalse(self.resume(fake))
        self.assertIn('otwarty', self.output)
        self.assert_untouched(fake)

    def test_inactive_lv_refused(self):
        fake = self.fake(attr={('vg0', 'r1'): '-wi-------'})
        self.assertFalse(self.resume(fake))
        self.assertIn('lvchange -ay', self.output)
        self.assert_untouched(fake)

    def test_lv_missing_refused(self):
        fake = self.fake(existing=(('vg0', 'root'),))
        self.assertFalse(self.resume(fake))
        self.assertIn('create', self.output)
        self.assert_untouched(fake)

    def test_fstab_already_correct_not_mounted(self):
        self.fstab.write_bytes(self.ORIGINAL + b'\n' + self.line)
        fake = self.fake(fs={self.dev: 'ext4'})
        self.assertTrue(self.resume(fake))
        self.assertIn(['mount', self.mount], fake.calls)
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL + b'\n' + self.line)
        self.assertEqual(self.backups(), [])
        self.assertIn('już poprawny', self.output)

    def test_conflicting_fstab_refused(self):
        other = '11111111-2222-3333-4444-5555555555ff'
        for label, extra in (('inne UUID', f'UUID={other} {self.mount} ext4 defaults 0 2\n'),
                             ('duplikat', self.line.decode() * 2),
                             ('nasz UUID gdzie indziej', f'UUID={UUID} /srv/elsewhere ext4 defaults 0 2\n')):
            with self.subTest(label=label):
                text = self.ORIGINAL + b'\n' + extra.encode()
                self.fstab.write_bytes(text)
                fake = self.fake(fs={self.dev: 'ext4'})
                self.assertFalse(self.resume(fake))
                self.assertIn('ręczny przegląd', self.output)
                self.assertEqual(self.fstab.read_bytes(), text)
                self.assertNotIn('mount', fake.names())

    def test_empty_lv_with_existing_fstab_entry_refused(self):
        text = self.ORIGINAL + b'\n' + self.line
        self.fstab.write_bytes(text)
        fake = self.fake()
        self.assertFalse(self.resume(fake, format_empty=True))
        self.assertNotIn('mkfs.ext4', fake.names())
        self.assertEqual(self.fstab.read_bytes(), text)

    def test_rollback_never_lvremoves_and_reports_mkfs(self):
        for step, mounted in (('verify_after', False), ('mount', False), ('verify', True)):
            with self.subTest(step=step):
                self.fstab.write_bytes(self.ORIGINAL)
                fake = self.fake(fail=step)
                self.assertFalse(self.resume(fake, format_empty=True))
                self.assertNotIn('lvremove', fake.names())
                self.assertIn(('vg0', 'r1'), fake.lvs)
                self.assertEqual('umount' in fake.names(), mounted)
                self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
                self.assertFalse(os.path.exists(self.root))
                self.assertIn('mkfs.ext4 na', self.output)
                self.assertIn('RÓŻNI SIĘ', self.output)  # ext4 został — nie udajemy pełnego wycofania
                self.assertNotIn('Wszystkie zmiany wycofane', self.output)

    def test_rollback_ext4_verified_clean(self):
        fake = self.fake(fail='mount', fs={self.dev: 'ext4'})
        self.assertFalse(self.resume(fake))
        self.assertNotIn('lvremove', fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
        self.assertFalse(os.path.exists(self.root))
        self.assertIn('Wszystkie zmiany wycofane — stan zweryfikowany.', self.output)

    def test_mkfs_failure_no_lvremove(self):
        fake = self.fake(fail='mkfs')
        self.assertFalse(self.resume(fake, format_empty=True))
        self.assertNotIn('lvremove', fake.names())
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)

    def test_idempotent_second_run(self):
        fake = self.fake()
        self.assertTrue(self.resume(fake, format_empty=True))
        after_first = self.fstab.read_bytes()
        calls_before = len(fake.calls)
        self.assertTrue(self.resume(fake, format_empty=True))
        self.assertIn('Nic do zrobienia', self.output)
        second = [c[0] for c in fake.calls[calls_before:]]
        for forbidden in ('mkfs.ext4', 'mount', 'umount', 'systemctl'):
            self.assertNotIn(forbidden, second)
        self.assertEqual(self.fstab.read_bytes(), after_first)
        self.assertEqual(len(self.backups()), 1)

    def test_dry_run_resume_changes_nothing(self):
        """Prawdziwe run() w trybie dry-run; subprocess.run obsługuje tylko odczyty."""
        fake = self.fake()
        executed = []

        def sp_run(args, **kwargs):
            executed.append(args[0])
            rc, out = fake._respond(args)
            return subprocess.CompletedProcess(args, rc, out, '')
        with mock.patch.object(m, 'DRY_RUN', True), mock.patch.object(m.subprocess, 'run', sp_run), \
                mock.patch('builtins.input', side_effect=AssertionError), quiet() as out:
            self.assertTrue(m.resume_volume('vg0', 'r1', self.mount, assume_yes=True, format_empty=True))
        self.output = out.getvalue()
        self.assertEqual(sorted(set(executed)), ['blkid', 'findmnt', 'lvs', 'udevadm', 'vgs', 'wipefs'])
        self.assertEqual(self.fstab.read_bytes(), self.ORIGINAL)
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(self.backups(), [])
        self.assertIn('[DRY-RUN] mkfs.ext4', self.output)


# --- pule libvirt ----------------------------------------------------------------------------

POOL_XML = "<pool type='{t}'><name>{n}</name><target><path>{p}</path></target></pool>"
POOL_INFO = ('Name:           x\nUUID:           abc\nState:          running\nPersistent:     yes\n'
             'Autostart:      yes\nCapacity:       1.17 TiB\nAllocation:     10.00 GiB\nAvailable:      1.16 TiB\n')


class PoolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(self)
        os.mkdir(os.path.join(self.tmp, 'vmstore'))
        self.mount = posix_path(self.tmp) + '/vmstore'
        self.pools = {
            'vm-pool': POOL_XML.format(t='dir', n='vm-pool', p=self.mount),
            'trailing': POOL_XML.format(t='dir', n='trailing', p=self.mount + '/'),
            'other-dir': POOL_XML.format(t='dir', n='other-dir', p='/var/lib/libvirt/images'),
            'lvm-pool': POOL_XML.format(t='logical', n='lvm-pool', p=self.mount),
            'broken': '<pool type="dir"><target>',
        }
        p = mock.patch.object(m, 'DRY_RUN', False)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, mounted=True, refresh_rc=0):
        self.calls = []

        def run(args, check=True, mutating=False):
            args = [str(a) for a in args]
            self.calls.append((args, mutating))
            rc, out = 0, ''
            if args[0] == 'findmnt':
                rc = 0 if mounted else 1
            elif args[:2] == ['virsh', 'pool-list']:
                out = '\n'.join(self.pools) + '\n'
            elif args[:2] == ['virsh', 'pool-dumpxml']:
                out = self.pools[args[-1]]
            elif args[:2] == ['virsh', 'pool-info']:
                out = POOL_INFO
            elif args[:2] == ['virsh', 'pool-refresh']:
                rc = refresh_rc
            if check and rc:
                raise subprocess.CalledProcessError(rc, args, out, 'błąd')
            return cp(args, rc, out)
        return run

    def virsh_cmds(self):
        return [a[1] for a, _ in self.calls if a[0] == 'virsh']

    def pools_cmd(self, answers=None, **kw):
        side = answers if answers is not None else AssertionError('input() nie powinno być wołane')
        with mock.patch.object(m, 'run', self.fake(**{k: kw.pop(k) for k in ('mounted', 'refresh_rc') if k in kw})), \
                mock.patch('builtins.input', side_effect=side), quiet() as out:
            result = m.pools_command(self.mount, **kw)
        self.output = out.getvalue()
        return result

    def test_parse_pool_xml(self):
        self.assertEqual(m.parse_pool_xml(POOL_XML.format(t='dir', n='a', p='/vm-storage')), ('dir', '/vm-storage'))
        self.assertEqual(m.parse_pool_xml(POOL_XML.format(t='logical', n='a', p='/dev/vg')), ('logical', '/dev/vg'))
        self.assertEqual(m.parse_pool_xml("<pool type='dir'><name>a</name></pool>"), ('dir', ''))
        self.assertIsNone(m.parse_pool_xml('<pool'))
        self.assertIsNone(m.parse_pool_xml(''))

    def test_parse_kv(self):
        info = m.parse_kv(POOL_INFO)
        self.assertEqual((info['State'], info['Capacity'], info['Autostart']), ('running', '1.17 TiB', 'yes'))

    def test_discovery_filters_by_type_and_path(self):
        with mock.patch.object(m, 'run', self.fake()), quiet() as out:
            found = m.discover_pools(self.mount)
        self.assertEqual([p['name'] for p in found], ['vm-pool', 'trailing'])
        self.assertEqual(found[0]['info']['Available'], '1.16 TiB')
        self.assertIn('broken', out.getvalue())
        self.assertTrue(all(not mut for _, mut in self.calls))  # wykrywanie tylko czyta

    def test_not_mountpoint_stops(self):
        self.assertFalse(self.pools_cmd(mounted=False, refresh=True, assume_yes=True))
        self.assertIn('STOP', self.output)
        self.assertIn('dysku systemowym', self.output)
        self.assertEqual(self.virsh_cmds(), [])

    def test_missing_dir_stops(self):
        self.mount += '/nope'
        self.assertFalse(self.pools_cmd(refresh=True, assume_yes=True))
        self.assertEqual(self.calls, [])

    def test_list_only_without_refresh_flag(self):
        self.assertTrue(self.pools_cmd())
        self.assertNotIn('pool-refresh', self.virsh_cmds())
        self.assertIn('running', self.output)
        self.assertIn('INNY, lokalny dysk', self.output)

    def test_refresh_only_after_confirm(self):
        self.assertTrue(self.pools_cmd(answers=['n'], refresh=True))
        self.assertNotIn('pool-refresh', self.virsh_cmds())
        self.assertTrue(self.pools_cmd(answers=['t'], refresh=True))
        refreshed = [(a[-1], mut) for a, mut in self.calls if a[:2] == ['virsh', 'pool-refresh']]
        self.assertEqual(refreshed, [('vm-pool', True), ('trailing', True)])

    def test_yes_refreshes_without_prompt_and_never_creates(self):
        self.assertTrue(self.pools_cmd(refresh=True, assume_yes=True))
        cmds = self.virsh_cmds()
        self.assertEqual(cmds.count('pool-refresh'), 2)
        for forbidden in ('pool-define', 'pool-define-as', 'pool-create', 'pool-start', 'pool-build',
                          'pool-delete', 'pool-undefine'):
            self.assertNotIn(forbidden, cmds)

    def test_refresh_failure_returns_false(self):
        self.assertFalse(self.pools_cmd(refresh=True, assume_yes=True, refresh_rc=1))

    def test_no_matching_pools(self):
        self.pools = {'other-dir': self.pools['other-dir']}
        self.assertTrue(self.pools_cmd(refresh=True, assume_yes=True))
        self.assertNotIn('pool-refresh', self.virsh_cmds())
        self.assertIn('NIE tworzy pul', self.output)


class WizardTests(unittest.TestCase):
    def test_existing_lv_offers_resume(self):
        vgs = [{'vg_name': 'vg0', 'vg_size': 100 * GIB, 'vg_free': 0}]  # brak miejsca nie blokuje dokończenia
        with mock.patch.object(m, 'list_vgs', return_value=vgs), mock.patch.object(m, 'lv_exists', return_value=True), \
                mock.patch.object(m, 'output'), mock.patch.object(m, 'resume_volume') as rv, \
                mock.patch.object(m, 'create_volume') as cv, \
                mock.patch('builtins.input', side_effect=['', 'vm-storage', 't', '']), quiet():
            m.create_store()
        rv.assert_called_once_with('vg0', 'vm-storage', '/vm-storage')
        cv.assert_not_called()

    def test_existing_lv_decline_resume_asks_new_name(self):
        vgs = [{'vg_name': 'vg0', 'vg_size': 100 * GIB, 'vg_free': 0}]
        with mock.patch.object(m, 'list_vgs', return_value=vgs), mock.patch.object(m, 'lv_exists', return_value=True), \
                mock.patch.object(m, 'output'), mock.patch.object(m, 'resume_volume') as rv, \
                mock.patch.object(m, 'create_volume') as cv, \
                mock.patch('builtins.input', side_effect=['', 'vm-storage', '', 'q']), quiet() as out:
            m.create_store()
        rv.assert_not_called()
        cv.assert_not_called()
        self.assertIn('Anulowano', out.getvalue())

    def test_menu_text(self):
        with mock.patch('builtins.input', side_effect=['0']), quiet() as out:
            self.assertEqual(m.interactive(), 0)
        self.assertIn('2. Utwórz / dokończ datastore LVM', out.getvalue())
        self.assertIn('6. Wykryj i odśwież pule libvirt', out.getvalue())


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
        self.assertEqual(m.__version__, '0.4.0')

    def test_non_root_without_dry_run(self):
        with mock.patch.object(m, 'is_root', return_value=False), \
                mock.patch.object(m, 'create_volume') as cv, quiet() as out:
            self.assertEqual(m.main(['--log-file', self.logfile] + self.CREATE), 1)
        cv.assert_not_called()
        self.assertIn('administratora', out.getvalue())

    def test_argparse_errors_exit_2(self):
        for argv in (['create'], ['create', '--vg', 'v', '--name', 'n', '--size', 'abc', '--mount', '/vm'],
                     ['--bogus'], ['delete'], ['resume'], ['resume', '--vg', 'v', '--name', 'n'],
                     ['pools', '--bogus'], ['create', '--vg', 'v', '--name', 'n', '--size', '1', '--mount', '/vm',
                                            '--format-empty']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), quiet(), \
                    self.assertRaises(SystemExit) as ctx:
                m.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_dry_run_flag_positions(self):
        for argv in (['--dry-run', '--log-file', self.logfile] + self.CREATE,
                     ['--log-file', self.logfile] + self.CREATE + ['--dry-run']):
            seen = {}

            def fake_create(*args, **kwargs):
                seen['dry'] = m.DRY_RUN
                seen['args'] = args
                seen['kwargs'] = kwargs
                return True
            with self.subTest(argv=argv), mock.patch.object(m, 'is_root', return_value=False), \
                    mock.patch.object(m, 'acquire_lock', return_value=True), \
                    mock.patch.object(m, 'create_volume', side_effect=fake_create), quiet():
                self.assertEqual(m.main(argv), 0)
            self.assertTrue(seen['dry'])
            self.assertEqual(seen['args'], ('vg0', 't1', 2, '/vm-t1', True))
            self.assertEqual(seen['kwargs'], {'fix_dev': False})
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


    def _main(self, argv, **patches):
        with mock.patch.object(m, 'is_root', return_value=True), \
                mock.patch.object(m, 'acquire_lock', return_value=True), quiet():
            mocks = {}
            with contextlib.ExitStack() as stack:
                for name, rv in patches.items():
                    mocks[name] = stack.enter_context(mock.patch.object(m, name, return_value=rv))
                rc = m.main(['--log-file', self.logfile] + argv)
        self._reset_logging()
        return rc, mocks

    def test_create_fix_dev_conflict_flag(self):
        rc, mk = self._main(self.CREATE + ['--fix-dev-conflict'], create_volume=True)
        self.assertEqual(rc, 0)
        mk['create_volume'].assert_called_once_with('vg0', 't1', 2, '/vm-t1', True, fix_dev=True)

    def test_resume_dispatch(self):
        rc, mk = self._main(['resume', '--vg', 'vg0', '--name', 'r1', '--mount', '/vm-r1', '--yes', '--format-empty'],
                            resume_volume=True)
        self.assertEqual(rc, 0)
        mk['resume_volume'].assert_called_once_with('vg0', 'r1', '/vm-r1', True, format_empty=True)
        rc, mk = self._main(['resume', '--vg', 'vg0', '--name', 'r1', '--mount', '/vm-r1'], resume_volume=False)
        self.assertEqual(rc, 1)
        mk['resume_volume'].assert_called_once_with('vg0', 'r1', '/vm-r1', False, format_empty=False)

    def test_pools_dispatch(self):
        rc, mk = self._main(['pools'], pools_command=True)
        self.assertEqual(rc, 0)
        mk['pools_command'].assert_called_once_with('/vm-storage', False, False)
        rc, mk = self._main(['pools', '--mount', '/srv/x', '--refresh', '--yes'], pools_command=False)
        self.assertEqual(rc, 1)
        mk['pools_command'].assert_called_once_with('/srv/x', True, True)

    def test_dry_run_after_subcommand(self):
        for argv in (['resume', '--vg', 'v', '--name', 'n', '--mount', '/vm', '--dry-run'],
                     ['pools', '--dry-run']):
            seen = {}
            fn = 'resume_volume' if argv[0] == 'resume' else 'pools_command'
            with self.subTest(argv=argv), mock.patch.object(m, 'is_root', return_value=False), \
                    mock.patch.object(m, 'acquire_lock', return_value=True), quiet(), \
                    mock.patch.object(m, fn, side_effect=lambda *a, **k: seen.setdefault('dry', m.DRY_RUN)):
                m.main(['--log-file', self.logfile] + argv)
            self.assertTrue(seen['dry'])
            self._reset_logging()


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


class AuditTests(unittest.TestCase):
    setUp, fake, virsh_cmds = PoolsTests.setUp, PoolsTests.fake, PoolsTests.virsh_cmds

    def fake_audit(self, mounted=True, src='/dev/mapper/ubuntu--vg-vm--storage', size=str(1200 * 1024 ** 3)):
        base = self.fake(mounted=mounted)

        def run(args, check=True, mutating=False):
            args = [str(a) for a in args]
            if args[:4] == ['findmnt', '-n', '-o', 'SOURCE']:
                self.calls.append((args, mutating))
                return cp(args, 0 if mounted else 1, src + '\n' if mounted else '')
            if args[0] == 'lsblk':
                self.calls.append((args, mutating))
                return cp(args, 0, size + '\n')
            return base(args, check, mutating)
        return run

    def audit(self, host='hvm1', **kw):
        out = io.StringIO()
        with mock.patch.object(m, 'run', self.fake_audit(**kw)), \
                mock.patch.object(m.socket, 'gethostname', return_value=host), \
                contextlib.redirect_stdout(out):
            result = m.audit_command(self.mount)
        return result, out.getvalue()

    def test_suggest_name(self):
        g = m.GIB
        self.assertEqual(m.suggest_datastore_name('hvm1', 1200 * g), 'HVM1-Local-1200')
        self.assertEqual(m.suggest_datastore_name('NODE2.lab.local', 1200 * g), 'HVM2-Local-1200')
        self.assertEqual(m.suggest_datastore_name('srv-a', 500 * g), 'SRV-A-Local-500')
        self.assertEqual(m.suggest_datastore_name('hvm2'), 'HVM2-Local')
        self.assertEqual(m.suggest_datastore_name(''), 'HOST-Local')

    def test_audit_is_read_only(self):
        result, out = self.audit()
        self.assertTrue(result)
        self.assertIn('HVM1-Local-1200', out)
        self.assertIn('Nie zmieniono żadnych zasobów', out)
        self.assertFalse(any(mut for _, mut in self.calls))
        self.assertNotIn('pool-refresh', self.virsh_cmds())

    def test_audit_warns_on_pool_name_without_host(self):
        self.pools['hvm1-local'] = POOL_XML.format(t='dir', n='hvm1-local', p=self.mount)
        _, out = self.audit()
        lines = [line for line in out.splitlines() if line.strip().startswith('- ')]
        self.assertTrue(any('vm-pool' in line and 'może być identyczna' in line for line in lines))
        self.assertTrue(any('hvm1-local' in line and 'może być identyczna' not in line for line in lines))

    def test_audit_stops_when_not_mountpoint(self):
        result, out = self.audit(mounted=False)
        self.assertFalse(result)
        self.assertIn('STOP', out)
        self.assertIn('HVM1-Local', out)
        self.assertNotIn('pool-dumpxml', self.virsh_cmds())

    def test_cli_audit_dispatch(self):
        with mock.patch.object(m, 'is_root', return_value=True), \
                mock.patch.object(m, 'acquire_lock', return_value=True), \
                mock.patch.object(m, 'setup_logging'), \
                mock.patch.object(m, 'audit_command', return_value=True) as audit:
            self.assertEqual(m.main(['audit', '--mount', '/vm-storage']), 0)
        audit.assert_called_once_with('/vm-storage')


if __name__ == '__main__':
    unittest.main()
