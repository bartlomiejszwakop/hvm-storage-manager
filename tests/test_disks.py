"""Klasyfikacja dysków z lsblk JSON: dysk systemowy (także przez łańcuch PV → VG → LV), VM, w użyciu, pusty."""
import unittest

import fakehost  # noqa: F401 — ustawia sys.path
from hvm import disks

GIB = 1024 ** 3


def node(name, size, typ, fstype=None, mounts=(None,), children=(), path=None):
    n = {'name': name, 'path': path or f'/dev/{name}', 'size': size, 'type': typ, 'fstype': fstype,
         'mountpoints': list(mounts)}
    if children:
        n['children'] = list(children)
    return n


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        root_lv = node('ubuntu--vg-ubuntu--lv', 100 * GIB, 'lvm', 'ext4', ['/'], path='/dev/mapper/ubuntu--vg-ubuntu--lv')
        self.tree = [
            node('sda', 1800 * GIB, 'disk', children=[
                node('sda1', GIB, 'part', 'vfat', ['/boot/efi']),
                node('sda2', 2 * GIB, 'part', 'ext4', ['/boot']),
                node('sda3', 1797 * GIB, 'part', 'LVM2_member', children=[root_lv])]),
            node('sdb', 2000 * GIB, 'disk'),
            node('sdc', 500 * GIB, 'disk', children=[node('sdc1', 500 * GIB, 'part', 'xfs', ['/backup'])]),
            node('nvme0n1', 900 * GIB, 'disk', 'LVM2_member', children=[
                node('data--vg-swap', 8 * GIB, 'lvm', 'swap', ['[SWAP]'], path='/dev/mapper/data--vg-swap')]),
            node('sdd', 100 * GIB, 'disk'),
            node('loop0', GIB, 'loop'),
            node('sr0', GIB, 'rom'),
        ]
        self.pvs = [{'pv_name': '/dev/sda3', 'vg_name': 'ubuntu-vg', 'pv_size': 1797 * GIB},
                    {'pv_name': '/dev/nvme0n1', 'vg_name': 'data-vg', 'pv_size': 900 * GIB},
                    {'pv_name': '/dev/loop0', 'vg_name': 'test-vg', 'pv_size': GIB}]
        self.vgs = [{'vg_name': 'ubuntu-vg', 'vg_size': 1797 * GIB, 'vg_free': 1387 * GIB},
                    {'vg_name': 'data-vg', 'vg_size': 900 * GIB, 'vg_free': 892 * GIB},
                    {'vg_name': 'test-vg', 'vg_size': GIB, 'vg_free': GIB}]

    def test_classification(self):
        data = disks.classify(self.tree, self.pvs, self.vgs, [('manager', '/dev/sdd')])
        status = {d['name']: d['status'] for d in data['disks']}
        self.assertEqual(status, {'sda': 'system', 'sdb': 'free', 'sdc': 'in-use', 'nvme0n1': 'system', 'sdd': 'vm'})
        by = {d['name']: d for d in data['disks']}
        self.assertIn('/', by['sda']['reason'])
        self.assertIn('[SWAP]', by['nvme0n1']['reason'])  # swap na LVM też blokuje dysk
        self.assertIn('manager', by['sdd']['reason'])
        self.assertIn('tylko informacja', by['sdb']['reason'])
        self.assertEqual(by['sda']['vgs'], ['ubuntu-vg'])
        vgs = {v['name']: v for v in data['vgs']}
        self.assertTrue(vgs['ubuntu-vg']['system'] and vgs['data-vg']['system'])
        self.assertFalse(vgs['test-vg']['system'])
        self.assertEqual(vgs['ubuntu-vg']['max_gib'], 1373)
        self.assertEqual([v['name'] for v in data['candidates']], ['ubuntu-vg', 'data-vg'])  # test-vg: brak miejsca

    def test_old_lsblk_mountpoint_field_and_string_sizes(self):
        tree = [{'name': 'vda', 'path': '/dev/vda', 'size': str(50 * GIB), 'type': 'disk', 'children': [
            {'name': 'vda1', 'path': '/dev/vda1', 'size': '1000', 'type': 'part', 'fstype': 'ext4', 'mountpoint': '/'}]}]
        data = disks.classify(tree, [], [])
        self.assertEqual(data['disks'][0]['status'], 'system')
        self.assertEqual(data['disks'][0]['size'], 50 * GIB)
        self.assertEqual(data['candidates'], [])


if __name__ == '__main__':
    unittest.main()
