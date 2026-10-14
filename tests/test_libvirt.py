"""libvirt: parsowanie, decyzja o puli, użycie przez VM, polecenia pools/audit z 0.4."""
import os
import unittest

from fakehost import HostTestCase

from hvm import legacy, libvirt, runner
from unittest import mock

POOL_XML = "<pool type='{t}'><name>{n}</name><target><path>{p}</path></target></pool>"
POOL_INFO = ('Name:           x\nUUID:           abc\nState:          running\nPersistent:     yes\n'
             'Autostart:      yes\nCapacity:       1.17 TiB\nAllocation:     10.00 GiB\nAvailable:      1.16 TiB\n')


def pool(name, target, ptype='dir', state='running', autostart='yes'):
    return {'name': name, 'type': ptype, 'target': target, 'state': state, 'autostart': autostart,
            'protected': name in libvirt.PROTECTED_POOLS}


class ParsingTests(unittest.TestCase):
    def test_parse_pool_xml(self):
        self.assertEqual(libvirt.parse_pool_xml(POOL_XML.format(t='dir', n='a', p='/vm-storage')), ('dir', '/vm-storage'))
        self.assertEqual(libvirt.parse_pool_xml(POOL_XML.format(t='logical', n='a', p='/dev/vg')), ('logical', '/dev/vg'))
        self.assertEqual(libvirt.parse_pool_xml("<pool type='dir'><name>a</name></pool>"), ('dir', ''))
        self.assertIsNone(libvirt.parse_pool_xml('<pool'))
        self.assertIsNone(libvirt.parse_pool_xml(''))

    def test_parse_kv(self):
        info = libvirt.parse_kv(POOL_INFO)
        self.assertEqual((info['State'], info['Capacity'], info['Autostart']), ('running', '1.17 TiB', 'yes'))

    def test_domain_sources(self):
        out = (' Type   Device   Target   Source\n------------------------------------\n'
               ' file   disk     vda      /vm-storage/vm1.qcow2\n block  disk     vdb      /dev/sdc\n'
               ' file   cdrom    sda      -\n')
        with mock.patch.object(runner, 'run', return_value=mock.Mock(returncode=0, stdout=out)):
            self.assertEqual(libvirt.domain_sources('vm1'), ['/vm-storage/vm1.qcow2', '/dev/sdc'])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(runner, 'ROOT', '')
        p.start()
        self.addCleanup(p.stop)
        self.base = [pool('local', '/var/lib/libvirt/images'), pool('morpheus-images', '/var/morpheus/kvm/images')]

    def test_create_when_nothing_matches(self):
        self.assertEqual(libvirt.pool_decision('HVM2-Local-1200', '/vm-storage', self.base), ('create', None))

    def test_existing_same_name_same_target(self):
        p = pool('HVM2-Local-1200', '/vm-storage/')
        self.assertEqual(libvirt.pool_decision('HVM2-Local-1200', '/vm-storage', self.base + [p]), ('existing', p))

    def test_same_name_other_target_or_type_refused(self):
        kind, why = libvirt.pool_decision('HVM2-Local-1200', '/vm-storage', self.base + [pool('HVM2-Local-1200', '/srv/x')])
        self.assertEqual(kind, 'refuse')
        self.assertIn('już istnieje', why)
        kind, why = libvirt.pool_decision('HVM2-Local-1200', '/vm-storage',
                                          self.base + [pool('HVM2-Local-1200', '/vm-storage', ptype='logical')])
        self.assertEqual(kind, 'refuse')
        self.assertIn('pokrywa się', why)

    def test_any_pool_on_parent_child_or_same_dir_of_other_type_refused(self):
        for p in (pool('parent', '/srv'), pool('child', '/srv/vm/sub'), pool('fs', '/srv/vm', ptype='fs'),
                  pool('root', '/')):
            with self.subTest(pool=p['name']):
                kind, why = libvirt.pool_decision('NEW', '/srv/vm', self.base + [p])
                self.assertEqual(kind, 'refuse')
                self.assertIn(p['name'], why)
        ok = pool('neighbour', '/srv/vm2')  # sąsiedni katalog nie przeszkadza
        self.assertEqual(libvirt.pool_decision('NEW', '/srv/vm', self.base + [ok])[0], 'create')

    def test_unreadable_pool_definition_refused(self):
        broken = dict(pool('mystery', ''), broken=True, type='?')
        kind, why = libvirt.pool_decision('NEW', '/vm-storage', self.base + [broken])
        self.assertEqual(kind, 'refuse')
        self.assertIn('mystery', why)

    def test_other_pool_same_target_reused(self):
        p = pool('vmstore', '/vm-storage')
        self.assertEqual(libvirt.pool_decision('HVM2-Local-1200', '/vm-storage', self.base + [p]), ('reuse', p))

    def test_protected(self):
        self.assertEqual(libvirt.pool_decision('local', '/vm-storage', self.base)[0], 'refuse')
        for mount in ('/var/lib/libvirt/images/pula1', '/var/lib/libvirt/images'):
            kind, why = libvirt.pool_decision('X', mount, self.base)
            self.assertEqual(kind, 'refuse')
            self.assertIn('chronionej puli local', why)

    def test_vm_usage(self):
        sources = [('vm1', '/vm-storage/disk.qcow2'), ('vm2', '/var/lib/libvirt/images/a.qcow2'),
                   ('vm3', '/dev/mapper/ubuntu--vg-vm--storage'), ('vm4', '/vm-storage2/x.qcow2')]
        hits = libvirt.vm_usage('/vm-storage', ['/dev/mapper/ubuntu--vg-vm--storage'], sources)
        self.assertEqual([h[0] for h in hits], ['vm1', 'vm3'])
        with mock.patch.object(libvirt, 'domains', return_value=None):
            self.assertIsNone(libvirt.vm_usage('/vm-storage'))  # libvirt nie odpowiada → nieznane


class LegacyPoolsTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.h.add_storage(pool='vm-pool')
        self.h.pools['trailing'] = {'type': 'dir', 'target': '/vm-storage/', 'state': 'running', 'autostart': 'yes'}
        self.h.pools['lvm-pool'] = {'type': 'logical', 'target': '/vm-storage', 'state': 'running', 'autostart': 'yes'}

    def test_discovery_filters_by_type_and_path_and_is_read_only(self):
        with self.h.active():
            found = libvirt.discover_pools('/vm-storage')
        self.assertEqual(sorted(p['name'] for p in found), ['trailing', 'vm-pool'])
        self.assertEqual(found[0]['info']['State'], 'running')
        self.assertEqual(self.h.mutating_calls(), [])

    def test_not_mountpoint_stops(self):
        self.h.mounts = [m for m in self.h.mounts if m['target'] != '/vm-storage']
        self.assertFalse(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage', True, True)))
        self.assertIn('STOP', self.output)
        self.assertNotIn('virsh pool-refresh', self.h.keys())

    def test_missing_dir_stops(self):
        self.assertFalse(self.run_on(self.h, lambda: legacy.pools_command('/vm-nope', True, True)))
        self.assertEqual(self.h.calls, [])

    def test_list_only_and_refresh_after_confirm(self):
        self.assertTrue(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage')))
        self.assertNotIn('virsh pool-refresh', self.h.keys())
        self.assertIn('LOKALNY dysk', self.output)
        self.assertTrue(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage', refresh=True), ['n']))
        self.assertNotIn('virsh pool-refresh', self.h.keys())
        self.assertTrue(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage', refresh=True), ['t']))
        refreshed = [c[-1] for c in self.h.calls if self.h.key(c) == 'virsh pool-refresh']
        self.assertEqual(sorted(refreshed), ['trailing', 'vm-pool'])
        for forbidden in ('pool-define-as', 'pool-create', 'pool-build', 'pool-delete', 'pool-undefine'):
            self.assertNotIn(f'virsh {forbidden}', self.h.keys())

    def test_refresh_failure_returns_false(self):
        self.h.fail.add('virsh pool-refresh')
        self.assertFalse(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage', True, True)))

    def test_no_matching_pools(self):
        for name in ('vm-pool', 'trailing', 'lvm-pool'):
            del self.h.pools[name]
        self.assertTrue(self.run_on(self.h, lambda: legacy.pools_command('/vm-storage', True, True)))
        self.assertIn('NIE tworzy pul', self.output)


class AuditTests(HostTestCase):
    def test_audit_is_read_only_and_suggests_name(self):
        h = self.host(hostname='hvm1')
        h.add_storage(pool='vm-pool')
        h.pools['hvm1-local'] = {'type': 'dir', 'target': '/vm-storage', 'state': 'running', 'autostart': 'yes'}
        self.assertTrue(self.run_on(h, lambda: legacy.audit_command('/vm-storage')))
        self.assertIn('HVM1-Local-1176', self.output)  # rozmiar systemu plików (98% LV)
        self.assertIn('Nie zmieniono żadnych zasobów', self.output)
        lines = [line for line in self.output.splitlines() if line.strip().startswith('- ')]
        self.assertTrue(any('vm-pool' in line and 'może być identyczna' in line for line in lines))
        self.assertTrue(any('hvm1-local' in line and 'może być identyczna' not in line for line in lines))
        self.assertEqual(h.mutating_calls(), [])

    def test_audit_stops_when_not_mountpoint(self):
        h = self.host(hostname='hvm1')
        os.makedirs(h.path('/vm-storage'))
        self.assertFalse(self.run_on(h, lambda: legacy.audit_command('/vm-storage')))
        self.assertIn('STOP', self.output)
        self.assertIn('HVM1-Local', self.output)
        self.assertNotIn('virsh pool-dumpxml', h.keys())


if __name__ == '__main__':
    unittest.main()
