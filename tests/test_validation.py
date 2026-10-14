"""Czyste funkcje walidujące: nazwy LVM, punkty montowania, fstab, rozmiary i menu rozmiaru, nazwy magazynów."""
import unittest

import fakehost  # noqa: F401 — ustawia sys.path
from hvm import libvirt, lvm, mount as mnt, wizard

GIB = lvm.GIB
UUID = '0f3c2a1b-1234-4abc-8def-0123456789ab'


class VerifyNameTests(unittest.TestCase):
    def test_valid(self):
        for name in ('vm-storage', 'a', 'A', 'vm_data', 'x+y.z-1', 'Snapshot', 'a' * 64):
            with self.subTest(name=name):
                self.assertTrue(lvm.verify_name(name))

    def test_reserved(self):
        for name in ('snapshot', 'pvmove', '.', '..', 'my_rimage', 'x_tmeta1', 'a_cdata', 'data_mlog', 'v_vorigin',
                     'z_pmspare', 'snapshot1', 'pvmove_x', 'a_cpool', 'a_cvol', 'a_imeta', 'a_iorig', 'a_wcorig'):
            with self.subTest(name=name):
                self.assertFalse(lvm.verify_name(name))

    def test_length_and_chars(self):
        self.assertFalse(lvm.verify_name('a' * 65))
        for name in ('', '1abc', '-a', '_a', '.a', '+a', 'a b', 'a/b', 'a\\b', 'zażółć', 'ab\n', 'a:b', 'a$b', 'a;rm',
                     None, 123, b'abc', ['a']):
            with self.subTest(name=name):
                self.assertFalse(lvm.verify_name(name))

    def test_vg_name_rules(self):
        for good in ('ubuntu-vg', '1vg', '_vg', 'vg.data'):
            self.assertTrue(lvm.verify_vg_name(good), good)
        for bad in ('-vg', '.', '..', 'vg/x', 'vg x', ''):
            self.assertFalse(lvm.verify_vg_name(bad), bad)


class MountPathTests(unittest.TestCase):
    def test_normalizes(self):
        for raw, expected in {'/vm-storage': '/vm-storage', '/vm-storage/': '/vm-storage', '//x': '/x',
                              '///srv//pool/': '/srv/pool', '/srv/./pool': '/srv/pool', '  /vm  ': '/vm'}.items():
            with self.subTest(raw=raw):
                self.assertEqual(mnt.normalize_mount(raw), expected)

    def test_rejects(self):
        for raw in ('vm-storage', 'srv/x', '', '   ', None, 5, '/', '//', '///', '/./', '/srv/../etc', '/..', '/a/..',
                    '/vm data', '/vm\tdata', '/vm\x01', '/vm\x1b[0m', '/vm\\data', '\\vm', '/vm x'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    mnt.normalize_mount(raw)

    def test_allowlist(self):
        for p in ('/vm', '/vm-storage', '/vm_data', '/vm2', '/srv/vm-storage', '/data/pool_1'):
            self.assertTrue(mnt.mount_allowed(p), p)
        for p in ('/etc/x', '/boot/x', '/srv/a/b', '/srv', '/data', '/data/x/y', '/home/vm', '/vmA', '/VM', '/srv/Pool',
                  '/var/lib/libvirt/images', '/usr', '/', '/vm/sub', '/srv/x\n', '/var/lib/libvirt/images/a/b',
                  '/var/lib/libvirt/images/pula1'):
            self.assertFalse(mnt.mount_allowed(p), p)
        with self.assertRaisesRegex(ValueError, 'dozwolonych'):
            mnt.check_allowed_mount('/etc/x')

    def test_protected_targets_refused_statically(self):
        """Katalogi pul local / Morpheus — odmowa niezależnie od libvirt (w nich, nad nimi i one same)."""
        for raw in ('/var/lib/libvirt/images/pula1', '/var/lib/libvirt/images', '/var/lib/libvirt', '/var',
                    '/var/morpheus', '/var/morpheus/kvm/images', '//var/lib/libvirt/images/./x'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, 'chronionym katalogiem'):
                    mnt.check_allowed_mount(raw)
        self.assertEqual(mnt.check_allowed_mount('/vm-storage'), '/vm-storage')


class FstabParsingTests(unittest.TestCase):
    TEXT = ('# /etc/fstab: static file system information.\n'
            '#UUID=11111111-2222-3333-4444-555555555555 /vm-old ext4 defaults 0 2\n\n   \n'
            '   # wcięty komentarz /indented\n'
            '/dev/mapper/vg0-root / ext4 errors=remount-ro 0 1\n'
            'UUID=AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE\t/srv/tabbed\text4\tdefaults\t0\t2\n'
            '  /dev/sdb1   /vm\\040data   ext4 defaults 0 2\n'
            '/dev/sdc1 /data/slash/ ext4 defaults 0 2\n'
            '/dev/disk/by-uuid/99999999-8888-7777-6666-555555555555 /data/byuuid ext4 defaults 0 2\n'
            'UUID="12345678-90ab-cdef-1234-567890abcdef" /data/quoted ext4 defaults 0 2\n'
            'PARTUUID=deadbeef-0000-1111-2222-333333333333 /data/part ext4 defaults 0 2\n'
            'onlyonefield\nnone swap swap sw 0 0\n')

    def test_has_mount(self):
        for mount in ('/', '/srv/tabbed', '/vm data', '/data/slash', '/data/slash/', '/data/byuuid'):
            self.assertTrue(mnt.fstab_has_mount(self.TEXT, mount), mount)
        for mount in ('/vm-old', '/indented', '/vm\\040data', '/vm', '/srv', '/data', 'onlyonefield', '/srv/tabbed2'):
            self.assertFalse(mnt.fstab_has_mount(self.TEXT, mount), mount)

    def test_has_uuid(self):
        for uuid in ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE',
                     '99999999-8888-7777-6666-555555555555', '12345678-90AB-CDEF-1234-567890ABCDEF'):
            self.assertTrue(mnt.fstab_has_uuid(self.TEXT, uuid), uuid)
        for uuid in ('11111111-2222-3333-4444-555555555555', 'deadbeef-0000-1111-2222-333333333333', UUID):
            self.assertFalse(mnt.fstab_has_uuid(self.TEXT, uuid), uuid)
        self.assertFalse(mnt.fstab_has_mount('', '/vm'))
        self.assertFalse(mnt.fstab_has_uuid('', UUID))

    def test_fstab_line_format(self):
        line = mnt.fstab_line(UUID, '/vm-storage')
        self.assertEqual(line, f'UUID={UUID} /vm-storage ext4 defaults,nofail 0 2\n')
        self.assertTrue(mnt.fstab_has_uuid(line, UUID) and mnt.fstab_has_mount(line, '/vm-storage'))


class FstabCheckTests(unittest.TestCase):
    BASE = '/dev/mapper/vg0-root / ext4 defaults 0 1\n'

    def test_missing(self):
        self.assertEqual(mnt.fstab_check(self.BASE, '/vm-storage', UUID), 'missing')
        self.assertEqual(mnt.fstab_check(self.BASE, '/vm-storage', None), 'missing')
        self.assertEqual(mnt.fstab_check(self.BASE + f'#UUID={UUID} /vm-storage ext4 d 0 2\n', '/vm-storage', UUID),
                         'missing')

    def test_ok(self):
        for src in (f'UUID={UUID}', f'UUID={UUID.upper()}', f'UUID="{UUID}"', f'/dev/disk/by-uuid/{UUID}'):
            with self.subTest(src=src):
                text = self.BASE + f'{src} /vm-storage/ ext4 defaults,nofail 0 2\n'
                self.assertEqual(mnt.fstab_check(text, '/vm-storage', UUID), 'ok')

    def test_conflicts(self):
        other = '11111111-2222-3333-4444-555555555555'
        for label, extra in {
            'inne źródło': f'UUID={other} /vm-storage ext4 defaults 0 2\n',
            'urządzenie zamiast UUID': '/dev/mapper/vg0-vm--storage /vm-storage ext4 defaults 0 2\n',
            'duplikat': f'UUID={UUID} /vm-storage ext4 defaults 0 2\nUUID={UUID} /vm-storage ext4 defaults 0 2\n',
            'UUID gdzie indziej': f'UUID={UUID} /srv/other ext4 defaults 0 2\n',
            'UUID w dwóch miejscach': f'UUID={UUID} /vm-storage ext4 defaults 0 2\nUUID={UUID} /srv/x ext4 d 0 2\n',
        }.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, 'ręczny przegląd'):
                    mnt.fstab_check(self.BASE + extra, '/vm-storage', UUID)
        with self.assertRaises(ValueError):  # pusty LV, a punkt już w fstab
            mnt.fstab_check(self.BASE + f'UUID={UUID} /vm-storage ext4 d 0 2\n', '/vm-storage', None)


class SizeTests(unittest.TestCase):
    def test_size_ok_boundaries(self):
        self.assertTrue(lvm.size_ok(1, 2 * GIB))
        self.assertFalse(lvm.size_ok(1, 2 * GIB - 1))
        self.assertFalse(lvm.size_ok(0, 100 * GIB))
        self.assertFalse(lvm.size_ok(-1, 100 * GIB))
        self.assertTrue(lvm.size_ok(9, 10 * GIB))
        self.assertFalse(lvm.size_ok(10, 10 * GIB))
        self.assertTrue(lvm.size_ok(10, 10 * GIB, reserve_gib=0))
        self.assertFalse(lvm.size_ok(1, 0))

    def test_reserve_is_one_percent_but_at_least_one_gib(self):
        self.assertEqual(lvm.reserve_for(10 * GIB), 1)
        self.assertEqual(lvm.reserve_for(100 * GIB), 1)
        self.assertEqual(lvm.reserve_for(101 * GIB), 2)
        self.assertEqual(lvm.reserve_for(1387 * GIB), 14)
        self.assertEqual(lvm.max_size_gib(1387 * GIB), 1373)
        self.assertEqual(lvm.max_size_gib(GIB), 0)
        for free in (2 * GIB, 57 * GIB + 5, 1387 * GIB, 4000 * GIB):
            self.assertTrue(lvm.size_ok(lvm.max_size_gib(free), free))
            self.assertFalse(lvm.size_ok(lvm.max_size_gib(free) + 1, free))

    def test_parse_size(self):
        self.assertEqual(lvm.parse_size('1'), 1)
        self.assertEqual(lvm.parse_size(' 42 '), 42)
        self.assertEqual(lvm.parse_size(1000000), 1000000)
        for bad in ('0', '1000001', '-1', '1.5', 'abc', '', '1e3', '0x10', '５'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lvm.parse_size(bad)

    def test_size_options(self):
        cases = {
            1373: ([1000, 1200, 1373], 1200),
            1200: ([1000, 1200], 1200),
            1100: ([1000, 1100], 1000),
            900: ([800, 900], 800),
            891: ([800, 891], 800),
            150: ([140, 150], 140),
            37: ([30, 37], 30),
            5: ([5], 5),
        }
        for max_gib, (sizes, rec) in cases.items():
            with self.subTest(max_gib=max_gib):
                opts, got = wizard.size_options(max_gib)
                self.assertEqual([s for s, _ in opts], sizes)
                self.assertEqual(got, rec)
                self.assertTrue(all(s <= max_gib for s in sizes))
                self.assertEqual(sum('zalecane' in label for _, label in opts), 1)
        self.assertEqual(wizard.size_options(0), ([], None))
        self.assertNotIn(1200, [s for s, _ in wizard.size_options(1199)[0]])

    def test_resolve_size(self):
        self.assertEqual(wizard.resolve_size('max', 1373), 1373)
        self.assertEqual(wizard.resolve_size('recommended', 1373), 1200)
        self.assertEqual(wizard.resolve_size('recommended', 900), 800)
        self.assertEqual(wizard.resolve_size(' 500 ', 1373), 500)
        for bad, max_gib in (('1374', 1373), ('abc', 1373), ('0', 10), ('max', 0)):
            with self.assertRaises(ValueError):
                wizard.resolve_size(bad, max_gib)


class NamingTests(unittest.TestCase):
    def test_suggest_name(self):
        self.assertEqual(libvirt.suggest_name('hvm1', 1200), 'HVM1-Local-1200')
        self.assertEqual(libvirt.suggest_name('NODE2.lab.local', 1200), 'HVM2-Local-1200')
        self.assertEqual(libvirt.suggest_name('node1'), 'HVM1-Local')
        self.assertEqual(libvirt.suggest_name('srv-a', 500), 'SRV-A-Local-500')
        self.assertEqual(libvirt.suggest_name('Srv_b.lab', 5), 'SRV_B-Local-5')
        self.assertEqual(libvirt.suggest_name('srv a$x', 5), 'SRV-A-X-Local-5')  # oczyszczona nazwa hosta
        self.assertEqual(libvirt.suggest_datastore_name('hvm2', 1200 * GIB), 'HVM2-Local-1200')

    def test_generic_hostnames_require_explicit_name(self):
        for host in ('', 'localhost', 'localhost.localdomain', 'host', 'ubuntu', 'Ubuntu.lan', None):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, '--pool-name'):
                    libvirt.suggest_name(host, 1200)
                self.assertIsNone(libvirt.try_suggest(host, 1200))

    def test_pool_name_check(self):
        for good in ('HVM2-Local-1200', 'a', 'Lab_Store.1'):
            self.assertEqual(libvirt.check_pool_name(good), good)
        for bad in ('', '-x', 'a b', 'a/b', 'x' * 65, 'local', 'morpheus-images', 'morpheus-cloud-init', 'zażółć',
                    '--name=local', '--pool=local', 'a=b', '=x', '.x', '_x', None, 5, 'x\n'):
            with self.assertRaises(ValueError, msg=bad):
                libvirt.check_pool_name(bad)


if __name__ == '__main__':
    unittest.main()
