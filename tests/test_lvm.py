"""LVM (JSON), ścieżki urządzeń, konflikty /dev (przypadek A), spójność device-mapper (B), sygnatury."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import fakehost  # noqa: F401 — ustawia sys.path
from hvm import filesystem as fs, lvm, runner

GIB = lvm.GIB
UUID = '0f3c2a1b-1234-4abc-8def-0123456789ab'


def cp(args, rc=0, out='', errt=''):
    return subprocess.CompletedProcess(list(args), rc, out, errt)


class LvmParsingTests(unittest.TestCase):
    def test_list_vgs(self):
        stdout = json.dumps({'report': [{'vg': [
            {'vg_name': 'ubuntu-vg', 'vg_size': '53687091200', 'vg_free': '10737418240'},
            {'vg_name': 'data', 'vg_size': ' 2147483648 ', 'vg_free': '<1073741824.00'},
            {'vg_name': 'full', 'vg_size': '4194304', 'vg_free': '0'}]}], 'log': []})
        with mock.patch.object(runner, 'run', return_value=cp([], 0, stdout)) as run:
            vgs = lvm.list_vgs()
        args = run.call_args[0][0]
        self.assertEqual(args[0], 'vgs')
        for flag in ('--reportformat', 'json', '--units', 'b', '--nosuffix'):
            self.assertIn(flag, args)
        self.assertEqual(vgs, [{'vg_name': 'ubuntu-vg', 'vg_size': 50 * GIB, 'vg_free': 10 * GIB},
                               {'vg_name': 'data', 'vg_size': 2 * GIB, 'vg_free': GIB},
                               {'vg_name': 'full', 'vg_size': 4194304, 'vg_free': 0}])

    def test_empty_and_failure(self):
        for stdout in ('', '{}', json.dumps({'report': [{'vg': []}]})):
            with mock.patch.object(runner, 'run', return_value=cp([], 0, stdout)):
                self.assertEqual(lvm.list_vgs(), [])
        with mock.patch.object(runner, 'run', side_effect=subprocess.CalledProcessError(5, ['vgs'])):
            with self.assertRaises(subprocess.CalledProcessError):
                lvm.list_vgs()

    def test_list_lvs_and_find(self):
        stdout = json.dumps({'report': [{'lv': [
            {'lv_name': 'root', 'vg_name': 'vg0', 'lv_attr': '-wi-ao----', 'lv_size': str(10 * GIB),
             'lv_path': '/dev/vg0/root', 'lv_dm_path': '/dev/mapper/vg0-root', 'lv_kernel_major': '253',
             'lv_kernel_minor': '0'},
            {'lv_name': 'off', 'vg_name': 'vg0', 'lv_attr': '-wi-----', 'lv_size': str(GIB),
             'lv_kernel_major': '-1', 'lv_kernel_minor': '-1'}]}]})
        with mock.patch.object(runner, 'run', return_value=cp([], 0, stdout)):
            lvs = lvm.list_lvs()
            self.assertTrue(lvm.lv_exists('vg0', 'root'))
            self.assertFalse(lvm.lv_exists('vg1', 'root'))
            self.assertFalse(lvm.lv_exists('vg0', 'roo'))
        self.assertEqual(lvs[0]['majmin'], '253:0')
        self.assertEqual(lvs[0]['size'], 10 * GIB)
        self.assertIsNone(lvs[1]['majmin'])
        self.assertEqual(lvs[1]['attr'], '-wi-------')  # dopełnione do 10 znaków
        self.assertEqual(lvm.find_lv('vg0', 'off', lvs)['name'], 'off')


class DevPathsTests(unittest.TestCase):
    def test_dm_name_and_paths(self):
        self.assertEqual(lvm.dm_name('ubuntu-vg', 'vm-storage'), 'ubuntu--vg-vm--storage')
        self.assertEqual(lvm.dm_name('vg0', 't1'), 'vg0-t1')
        self.assertEqual(lvm.dm_name('a--b', 'c-'), 'a----b-c--')
        self.assertEqual(lvm.dm_name('vg_x.y', 'lv+1'), 'vg_x.y-lv+1')
        self.assertEqual(lvm.dev_paths('ubuntu-vg', 'vm-storage'),
                         ('/dev/ubuntu-vg/vm-storage', '/dev/mapper/ubuntu--vg-vm--storage'))


def fake_stat(mode):
    return lambda p: types.SimpleNamespace(st_mode=mode, st_rdev=0)


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hvmtest-').replace('\\', '/')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(runner, 'ROOT', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(self.tmp + '/dev/vg0')

    def test_path_states(self):
        self.assertEqual(lvm.path_state('/dev/vg0/none'), 'brak')
        os.makedirs(self.tmp + '/dev/vg0/empty')
        self.assertEqual(lvm.path_state('/dev/vg0/empty'), 'katalog (pusty)')
        os.makedirs(self.tmp + '/dev/vg0/full/x')
        self.assertEqual(lvm.path_state('/dev/vg0/full'), 'katalog (niepusty)')
        open(self.tmp + '/dev/vg0/file', 'w').close()
        self.assertEqual(lvm.path_state('/dev/vg0/file'), 'zwykły plik')
        with mock.patch.object(lvm.os, 'lstat', fake_stat(stat.S_IFLNK | 0o777)), \
                mock.patch.object(lvm.os, 'readlink', return_value='../dm-7'):
            self.assertEqual(lvm.path_state('/dev/vg0/x'), 'dowiązanie → ../dm-7')
        with mock.patch.object(lvm.os, 'lstat', fake_stat(stat.S_IFBLK | 0o660)):
            self.assertTrue(lvm.path_state('/dev/vg0/x').startswith('urządzenie blokowe'))
        self.assertIsNone(lvm.block_majmin('/dev/vg0/file'))

    def test_conflict_rules(self):
        with mock.patch.object(lvm, 'processes_using', return_value=[]):
            os.makedirs(self.tmp + '/dev/vg0/t1')
            ok, _ = lvm.conflict_fixable('/dev/vg0/t1')
            self.assertTrue(ok)
            found = lvm.conflict_findings('vg0', 't1')
            self.assertEqual([(f['path'], f['fixable']) for f in found], [('/dev/vg0/t1', True)])
            os.makedirs(self.tmp + '/dev/mapper')
            open(self.tmp + '/dev/mapper/vg0-t1', 'w').close()
            found = lvm.conflict_findings('vg0', 't1')
            self.assertEqual([f['fixable'] for f in found], [True, False])
            self.assertIn('zwykły plik', found[1]['reason'])
            with mock.patch.object(lvm.os.path, 'ismount', return_value=True):
                ok, reason = lvm.conflict_fixable('/dev/vg0/t1')
            self.assertFalse(ok)
            self.assertIn('punktem montowania', reason)
        with mock.patch.object(lvm, 'processes_using', return_value=[12, 34]):
            ok, reason = lvm.conflict_fixable('/dev/vg0/t1')
            self.assertFalse(ok)
            self.assertIn('PID 12, 34', reason)
        with mock.patch.object(lvm, 'processes_using', return_value=None):
            self.assertFalse(lvm.conflict_fixable('/dev/vg0/t1')[0])

    def test_remove_conflict_dir(self):
        os.makedirs(self.tmp + '/dev/vg0/t1')
        with mock.patch.object(lvm, 'processes_using', return_value=[]), \
                mock.patch('sys.stdout'):
            with mock.patch.object(runner, 'DRY_RUN', True):
                lvm.remove_conflict_dir('/dev/vg0/t1')
            self.assertTrue(os.path.isdir(self.tmp + '/dev/vg0/t1'))
            lvm.remove_conflict_dir('/dev/vg0/t1')
        self.assertFalse(os.path.exists(self.tmp + '/dev/vg0/t1'))
        os.makedirs(self.tmp + '/dev/vg0/t2/x')
        with mock.patch.object(lvm, 'processes_using', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'niepusty'):
                lvm.remove_conflict_dir('/dev/vg0/t2')

    def test_holders(self):
        self.assertIsNone(lvm.holders('253:3'))  # brak /sys → nie da się sprawdzić
        os.makedirs(self.tmp + '/sys/class/block/dm-3/holders')
        self.assertEqual(lvm.holders('253:3'), [])
        os.makedirs(self.tmp + '/sys/class/block/dm-3/holders/dm-9')
        self.assertEqual(lvm.holders('253:3'), ['dm-9'])
        self.assertIsNone(lvm.holders(None))

    def test_processes_using_scans_proc(self):
        links = {'/proc/100/cwd': '/dev/vg0/t1', '/proc/100/root': '/', '/proc/200/cwd': '/root',
                 '/proc/200/root': '/', '/proc/200/fd/3': '/dev/vg0/t1/x', '/proc/300/cwd': '/tmp',
                 '/proc/300/root': '/'}
        dirs = {'/proc': ['100', '200', '300', 'self', 'meminfo'], '/proc/100/fd': [], '/proc/200/fd': ['3'],
                '/proc/300/fd': []}

        def listdir(p):
            return dirs[p]

        def readlink(p):
            if p not in links:
                raise FileNotFoundError(p)
            return links[p]
        with mock.patch.object(runner, 'ROOT', ''), mock.patch.object(lvm.os, 'listdir', listdir), \
                mock.patch.object(lvm.os, 'readlink', readlink):
            self.assertEqual(lvm.processes_using('/dev/vg0/t1'), [100, 200])
            self.assertEqual(lvm.processes_using('/dev/vg0/t9'), [])
        with mock.patch.object(runner, 'ROOT', ''), mock.patch.object(lvm.os, 'listdir', side_effect=OSError):
            self.assertIsNone(lvm.processes_using('/dev/vg0/t1'))


class DeviceTests(unittest.TestCase):
    """wait_device (nowy LV) i device_check (przypadek B) na atrapie węzłów."""

    def setUp(self):
        self.nodes = {}
        self.calls = []
        for p in (mock.patch.object(lvm, 'block_majmin', lambda p: self.nodes.get(p)),
                  mock.patch.object(lvm, 'node_info', lambda p: ('link', '../dm-3') if p in self.nodes
                                    else ('missing', '')),
                  mock.patch.object(runner, 'run', side_effect=self.fake_run),
                  mock.patch.object(lvm, 'find_lv', lambda vg, name, lvs=None: {'majmin': self.lvs_majmin})):
            p.start()
            self.addCleanup(p.stop)
        self.mapper = '/dev/mapper/vg0-t1'

    def fake_run(self, args, check=True):
        self.calls.append(args)
        if args[0] == 'vgscan' and self.after_vgscan:
            self.nodes[self.mapper] = '253:3'
        return cp(args, 0)

    after_vgscan = False
    lvs_majmin = '253:3'

    def test_wait_device_present(self):
        self.nodes[self.mapper] = '253:3'
        self.assertEqual(lvm.wait_device('vg0', 't1'), self.mapper)
        self.assertEqual(self.calls, [['udevadm', 'settle', '--timeout=30']])

    def test_wait_device_vgscan_fallback(self):
        self.after_vgscan = True
        with mock.patch('sys.stdout'):
            self.assertEqual(lvm.wait_device('vg0', 't1'), self.mapper)
        self.assertIn(['vgscan', '--mknodes'], self.calls)

    def test_wait_device_refuses_device_not_matching_lvm(self):
        self.nodes[self.mapper] = '253:3'
        self.lvs_majmin = '253:9'
        with self.assertRaisesRegex(RuntimeError, 'nie odpowiada'):
            lvm.wait_device('vg0', 't1')

    def test_wait_device_missing(self):
        with mock.patch('sys.stdout'), self.assertRaisesRegex(RuntimeError, 'nie pojawiło się'):
            lvm.wait_device('vg0', 't1')

    def test_device_check(self):
        lv = {'majmin': '253:3'}
        self.nodes[self.mapper] = '253:3'
        self.nodes['/dev/vg0/t1'] = '253:3'
        res = lvm.device_check('vg0', 't1', lv)
        self.assertEqual((res['device'], res['case']), (self.mapper, None))
        del self.nodes['/dev/vg0/t1']
        res = lvm.device_check('vg0', 't1', lv)
        self.assertEqual((res['device'], res['case']), (self.mapper, 'B'))
        self.nodes[self.mapper] = '253:9'
        res = lvm.device_check('vg0', 't1', lv)
        self.assertIsNone(res['device'])
        self.assertIn('niespójność', res['detail'])
        self.assertIsNone(lvm.device_check('vg0', 't1', {'majmin': None})['device'])


class SignatureTests(unittest.TestCase):
    def detect(self, wipefs=(0, ''), blkid=(2, '')):
        def fake(args, check=True):
            rc, out = wipefs if args[0] == 'wipefs' else blkid
            return cp(args, rc, out)
        with mock.patch.object(runner, 'run', side_effect=fake) as run:
            result = fs.detect_signature('/dev/x')
        for c in run.call_args_list:  # wyłącznie odczyt
            self.assertNotIn('-a', c[0][0])
            if c[0][0][0] == 'wipefs':
                self.assertIn('-n', c[0][0])
        return result[0]

    def test_cases(self):
        self.assertEqual(self.detect(), 'empty')
        self.assertEqual(self.detect((0, 'ext4\n'), (0, f'TYPE=ext4\nUUID={UUID}\n')), 'ext4')
        self.assertEqual(self.detect((0, 'xfs\n'), (0, 'TYPE=xfs\n')), 'other')
        self.assertEqual(self.detect((0, 'gpt\nPMBR\n'), (0, 'PTTYPE=gpt\n')), 'other')
        self.assertEqual(self.detect((0, 'ext4\nLVM2_member\n'), (0, 'TYPE=ext4\n')), 'other')
        self.assertEqual(self.detect((0, ''), (8, '')), 'other')
        self.assertEqual(self.detect((0, 'swap\n'), (2, '')), 'other')

    def test_wipefs_failure_raises(self):
        with mock.patch.object(runner, 'run', return_value=cp([], 1, '', 'Permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                fs.detect_signature('/dev/x')

    def test_read_uuid(self):
        with mock.patch.object(runner, 'run', return_value=cp([], 0, f'TYPE=ext4\nUUID={UUID}\n')):
            self.assertEqual(fs.read_uuid('/dev/x'), UUID)
        with mock.patch.object(runner, 'run', return_value=cp([], 0, 'TYPE=ext4\nUUID=garbage\n')):
            with self.assertRaises(RuntimeError):
                fs.read_uuid('/dev/x')

    def test_mkfs_args(self):
        self.assertEqual(fs.mkfs_args('/dev/x', 'a-very-long-label-name'),
                         ['mkfs.ext4', '-F', '-m', '1', '-L', 'a-very-long-labe', '/dev/x'])


if __name__ == '__main__':
    unittest.main()
