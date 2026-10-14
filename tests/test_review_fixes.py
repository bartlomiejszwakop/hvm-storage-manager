"""Testy regresji poprawek po przeglądzie 1.0 (H1–H4, M1–M8, L1–L7) na symulatorze FakeHost."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fakehost import BASE_FSTAB, GIB, FakeHost, HostTestCase

from hvm import cli, diagnostics as diag, lvm, mount as mnt, runner, safety, wizard

MOUNT = '/vm-storage'
MAPPER = '/dev/mapper/ubuntu--vg-vm--storage'
URI = ['virsh', '-c', 'qemu:///system']


def prepare(size='1200', **kw):
    kw.setdefault('assume_yes', True)
    return lambda: wizard.prepare_cli(size, **kw)


class CliCase(HostTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='hvmcli-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(runner.setup_logging, None)
        self.log = os.path.join(self.tmp, 'hvm.log')

    def cli(self, h, argv, answers=None, lock=True):
        with mock.patch.object(runner, 'acquire_lock', return_value=lock):
            return self.run_on(h, lambda: cli.main(['--log-file', self.log] + argv), answers)


class ProtectedTargetsTests(CliCase):
    """H1: punkt montowania w/nad katalogiem chronionych pul — odmowa także przy repair i bez libvirt."""

    def test_repair_and_prepare_refuse_protected_dirs_even_without_libvirt(self):
        h = self.host(libvirt=False)
        for mount in ('/var/lib/libvirt/images/pula1', '/var/morpheus/x', '/var/lib/libvirt'):
            with self.subTest(mount=mount):
                self.assertEqual(self.cli(h, ['repair', '--mount', mount, '--yes']), 1)
                self.assertIn('chronionym katalogiem', self.output)
                self.assertEqual(self.cli(h, ['prepare', '--mount', mount, '--yes', '--pool-name', 'X1']), 1)
                self.assertIn('chronionym katalogiem', self.output)
        self.assertEqual(h.mutating_calls(), [])


class PoolNameInjectionTests(CliCase):
    """H2: nazwa puli nigdy nie staje się opcją virsh."""

    def test_cli_pool_name_injection_refused(self):
        h = self.host()
        for argv in (['prepare', '--yes', '--pool-name=--name=local'], ['prepare', '--yes', '--pool-name=-x'],
                     ['prepare', '--yes', '--pool-name', 'a=b'], ['prepare', '--yes', '--pool-name', 'local'],
                     ['repair', '--yes', '--pool-name=--pool=local']):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(h, argv), 1)
                self.assertIn('BŁĄD', self.output)
                self.assertNotIn('Traceback', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_existing_lv_path_validates_pool_name(self):
        h = self.host()
        h.add_storage(pool=None, mounted=False)
        self.assertEqual(self.cli(h, ['prepare', '--yes', '--pool-name=--name=local']), 1)
        self.assertEqual(h.mutating_calls(), [])

    def test_all_virsh_calls_use_system_uri_and_option_names(self):
        h = self.host()
        self.assertTrue(self.run_on(h, prepare()))
        virsh = [c for c in h.calls if c[0] == 'virsh']
        self.assertTrue(virsh)
        for c in virsh:
            self.assertEqual(c[:3], URI, c)
            safety.parse_virsh(c)  # ścisła składnia

    def test_forbidden_call_detector_is_strict(self):
        h = FakeHost()
        self.addCleanup(h.cleanup)
        h.defined.add('mine')
        bad = [URI + ['pool-refresh', '--pool=local'], URI + ['pool-define-as', '--name=local', '--type', 'dir',
                                                             '--target', '/x'],
               URI + ['pool-autostart', '--disable', 'local'], URI + ['pool-start', 'local'],
               ['virsh', 'pool-start', '--pool', 'mine'], URI + ['pool-undefine', '--pool', 'other'],
               URI + ['pool-delete', '--pool', 'mine'], ['wipefs', '-a', '/dev/x'], ['lvremove', '-y', 'vg/x']]
        good = [URI + ['pool-undefine', '--pool', 'mine'], URI + ['pool-info', '--pool', 'local'],
                URI + ['pool-refresh', '--pool', 'mine']]
        h.calls = [list(c) for c in bad + good]
        self.assertEqual(h.forbidden_calls(), bad)


class DryRunNoWritesTests(CliCase):
    """H4: dry-run i --diagnose bez root niczego nie zapisują (log, blokada, raport, cwd)."""

    def test_dry_run_and_non_root_diagnose_create_no_files(self):
        h = self.host()
        lock = os.path.join(self.tmp, 'hvm.lock')
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        etc_before = sorted(os.listdir(h.path('/etc')))
        with mock.patch.object(runner, 'LOCK_FILE', lock):
            for argv in (['--dry-run', 'prepare', '--yes'], ['--dry-run', 'repair', '--yes'],
                         ['create', '--vg', 'ubuntu-vg', '--name', 'x1', '--size', '5', '--mount', '/vm-x1', '--yes',
                          '--dry-run']):
                with self.subTest(argv=argv):
                    with h.active(), contextlib.redirect_stdout(io.StringIO()), \
                            mock.patch('builtins.input', side_effect=AssertionError):
                        cli.main(['--log-file', self.log] + argv)
            with h.active(), mock.patch.object(runner, 'is_root', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--log-file', self.log, '--diagnose', '--json']), 0)
                self.assertEqual(cli.main(['--log-file', self.log, '--diagnose']), 0)
        self.assertEqual(os.listdir(self.tmp), [])  # brak logu, blokady i plików w cwd
        self.assertEqual(sorted(os.listdir(h.path('/etc'))), etc_before)
        self.assertEqual(h.fstab(), BASE_FSTAB)
        self.assertEqual(h.mutating_calls(), [])

    def test_export_report_in_dry_run_prints_instead_of_writing(self):
        h = self.host()
        with h.active(), mock.patch.object(runner, 'DRY_RUN', True), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(diag.export_report('/etc/report.json'))
        self.assertFalse(os.path.exists(h.path('/etc/report.json')))
        self.assertIn('"storages"', out.getvalue())

    def test_menu_toggle_dry_run_off_starts_logging_and_lock(self):
        with mock.patch.object(runner, 'DRY_RUN', True), mock.patch.object(runner, 'is_root', return_value=True), \
                mock.patch.object(runner, 'acquire_lock', return_value=True) as lock, \
                mock.patch.object(cli, 'LOG_PATH', [self.log]):
            cli.toggle_dry_run()
            self.assertFalse(runner.DRY_RUN)
            lock.assert_called_once()
            self.assertEqual(runner.log_path, self.log)
            cli.toggle_dry_run()
            self.assertTrue(runner.DRY_RUN)
            self.assertIsNone(runner.log_path)

    def test_menu_toggle_refused_when_lock_busy(self):
        with mock.patch.object(runner, 'DRY_RUN', True), mock.patch.object(runner, 'is_root', return_value=True), \
                mock.patch.object(runner, 'acquire_lock', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.toggle_dry_run()
            self.assertTrue(runner.DRY_RUN)
        self.assertIn('inna instancja', out.getvalue())


class FormatGuardTests(HostTestCase):
    """M1: przed mkfs istniejącego LV — maszyny (także zatrzymane), typ LV, holders, maj:min."""

    def empty_lv(self, **kw):
        h = self.host(**kw)
        h.add_storage(fs=None, mounted=False, fstab=False)
        return h

    def repair(self, h):
        return self.run_on(h, lambda: wizard.repair(assume_yes=True, format_empty='ubuntu-vg/vm-storage'))

    def test_stopped_vm_using_lv_blocks_format(self):
        h = self.empty_lv()
        h.domains['stopped-vm'] = {'running': False, 'disks': ['/dev/ubuntu-vg/vm-storage']}
        self.assertFalse(self.repair(h))
        self.assertIn('stopped-vm', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_libvirt_unreachable_blocks_format_of_existing_lv(self):
        h = self.empty_lv(libvirt=False)
        self.assertFalse(self.repair(h))
        self.assertIn('Nie można sprawdzić', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_snapshot_or_pool_lv_not_formatted(self):
        for attr0 in ('s', 'o', 't', 'r'):
            with self.subTest(attr0=attr0):
                h = self.empty_lv()
                h.lv()['attr0'] = attr0
                self.assertFalse(self.repair(h))
                self.assertIn('nie jest zwykłym wolumenem', self.output)
                self.assertNotIn('mkfs.ext4', h.names())

    def test_thin_volume_is_allowed(self):
        h = self.empty_lv()
        h.lv()['attr0'] = 'V'
        self.assertTrue(self.repair(h))
        self.assertIn('mkfs.ext4', h.names())

    def test_holders_block_format(self):
        h = self.empty_lv()
        os.makedirs(h.path(f'/sys/class/block/dm-{h.lv()["minor"]}/holders/dm-99'))
        self.assertFalse(self.repair(h))
        self.assertIn('dm-99', self.output)
        self.assertNotIn('mkfs.ext4', h.names())

    def test_lv_opened_between_plan_and_mkfs_is_not_formatted(self):
        h = self.empty_lv()
        orig = h.c_wipefs

        def wipefs_then_open(a):  # ktoś otwiera LV tuż po diagnozie
            h.domains['late'] = {'running': True, 'disks': [MAPPER]}
            return orig(a)
        h.c_wipefs = wipefs_then_open
        self.assertFalse(self.repair(h))
        self.assertNotIn('mkfs.ext4', h.names())

    def test_mkfs_never_runs_without_fresh_empty_check(self):
        h = self.empty_lv()
        with h.active(), self.assertRaises(safety.CommandRefused):
            runner.run(['mkfs.ext4', '-F', '-m', '1', '-L', 'vm-storage', MAPPER])
        self.assertNotIn('mkfs.ext4', h.names())


class FormatEmptyPolicyTests(CliCase):
    """M2: --format-empty musi wskazywać dokładnie cel."""

    def test_format_empty_must_match_target(self):
        h = self.host()
        h.add_storage(fs=None, mounted=False, fstab=False)
        for value in ('ubuntu-vg/other', 'vm-storage', 'other-vg/vm-storage', ''):
            with self.subTest(value=value):
                self.assertEqual(self.cli(h, ['repair', '--yes', '--format-empty', value]), 1)
                self.assertIn('--format-empty ubuntu-vg/vm-storage', self.output)
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(self.cli(h, ['repair', '--yes', '--format-empty', 'ubuntu-vg/vm-storage']), 0)
        self.assertEqual(h.lv()['fs'], 'ext4')


class PoolDecisionTests(HostTestCase):
    """M6: nieczytelna definicja puli i pule zagnieżdżone — odmowa."""

    def test_unreadable_pool_blocks_creation(self):
        h = self.host()
        h.pools['mystery'] = {'type': 'dir', 'target': '/srv/x', 'state': 'running', 'autostart': 'yes', 'broken': True}
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIn('mystery', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_pool_on_parent_dir_blocks_creation(self):
        h = self.host()
        h.pools['wide'] = {'type': 'dir', 'target': '/', 'state': 'running', 'autostart': 'yes'}
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIn('wide', self.output)
        self.assertEqual(h.mutating_calls(), [])


class CleanErrorsTests(CliCase):
    """M7: błędy użytkownika i poleceń bez tracebacków."""

    def test_bad_mounts_and_failing_vgs(self):
        h = self.host()
        for argv in (['repair', '--mount', 'vm-storage'], ['repair', '--mount', '/vm storage'],
                     ['repair', '--mount', '/etc/x']):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(h, argv), 1)
                self.assertIn('BŁĄD', self.output)
        h.fail.add('vgs')
        self.assertEqual(self.cli(h, ['prepare', '--yes']), 1)
        self.assertIn('BŁĄD', self.output)
        self.assertEqual(h.mutating_calls(), [])


class DeviceIdentityTests(HostTestCase):
    """M8: fstab /dev/... i montowanie sprawdzane po maj:min; duplikaty UUID."""

    def test_fstab_entry_for_same_device_on_other_target_refused(self):
        h = self.host()
        h.add_storage(mounted=False, fstab=False)
        h.write_fstab(BASE_FSTAB + f'{MAPPER} /srv/old ext4 defaults 0 2\n')
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('/srv/old', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_fstab_symlink_source_resolved(self):
        h = self.host()
        h.add_storage(mounted=False, fstab=False)
        h.write_fstab(BASE_FSTAB + '/dev/ubuntu-vg/vm-storage /vm-storage ext4 defaults 0 2\n')
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('ręczny przegląd', self.output)

    def test_same_uuid_on_other_device_is_not_ours(self):
        h = self.host()
        uuid = h.add_storage(mounted=False)
        h.mounts.append({'target': MOUNT, 'source': '/dev/mapper/ubuntu--vg-clone', 'fstype': 'ext4',
                         'size': GIB, 'uuid': uuid})
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('INNE urządzenie', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_duplicate_uuid_refused(self):
        h = self.host()
        uuid = h.add_storage(mounted=False)
        h.extra_uuid_devices[uuid] = ['/dev/mapper/ubuntu--vg-vm--storage--snap']
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('migawka lub klon', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_fstab_check_unit(self):
        text = BASE_FSTAB + '/dev/mapper/x /other ext4 defaults 0 2\n'
        with mock.patch.object(lvm, 'block_majmin', lambda p: '253:7' if p == '/dev/mapper/x' else None):
            with self.assertRaisesRegex(ValueError, '/other'):
                mnt.fstab_check(text, '/vm-storage', None, '253:7')
            self.assertEqual(mnt.fstab_check(text, '/vm-storage', None, '253:8'), 'missing')


class InterruptDuringFstabTests(HostTestCase):
    """L1/L7: Ctrl+C w trakcie zapisu fstab (przed i po os.replace) — fstab wraca do oryginału."""

    def run_interrupted(self, after_replace):
        h = self.host()
        real_replace = os.replace

        def replace(src, dst):
            if dst.endswith('/etc/fstab') and not after_replace:
                raise KeyboardInterrupt
            real_replace(src, dst)
            if dst.endswith('/etc/fstab') and h.lv() and h.lv()['fs'] and 'UUID=' in h.fstab().split('\n')[-2]:
                raise KeyboardInterrupt
        with mock.patch.object(mnt.os, 'replace', side_effect=replace):
            self.assertFalse(self.run_on(h, prepare()))
        return h

    def test_interrupt_before_and_after_replace(self):
        for after in (False, True):
            with self.subTest(after_replace=after):
                h = self.run_interrupted(after)
                self.assertEqual(h.fstab(), BASE_FSTAB)
                self.assertIn('przerwano (Ctrl+C)', self.output)
                self.assertIsNone(h.mount_of(MOUNT))
                self.assertNotIn('lvremove', h.names())


class NoFalseCleanClaimTests(unittest.TestCase):
    """L2: po mkfs nigdy „Wszystkie zmiany wycofane”."""

    def test_formatted_never_claims_full_rollback(self):
        op = safety.Operation()
        op.ctx['formatted'] = True
        same = {'lv': True, 'dev': 'x', 'fs': 'ext4', 'mounted': False, 'fstab': 'h', 'dirs': ()}
        t = {'vg': 'vg', 'lv': 'lv', 'mount': '/vm', 'pool': None}
        with mock.patch.object(diag, 'snapshot', return_value=dict(same)), \
                mock.patch.object(diag.mnt, 'read_fstab', return_value=''), \
                mock.patch.object(runner, 'run'), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(diag.diagnose_after(t, dict(same), (), op))
        self.assertNotIn('Wszystkie zmiany wycofane', out.getvalue())
        self.assertIn('mkfs.ext4', out.getvalue())


class NamingTests(HostTestCase):
    """M5: nazwa hosta bez znaczenia → wymagana jawna nazwa puli."""

    def test_generic_hostname_requires_pool_name(self):
        h = self.host(hostname='localhost')
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIn('--pool-name', self.output)
        self.assertEqual(h.mutating_calls(), [])
        self.assertTrue(self.run_on(h, prepare(pool='LAB-Local-1200')))
        self.assertIn('LAB-Local-1200', h.pools)

    def test_generic_hostname_repair_notes_missing_name(self):
        h = self.host(hostname='ubuntu')
        h.add_storage(pool=None)
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('--pool-name', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_interactive_wizard_asks_for_name_without_default(self):
        h = self.host(hostname='localhost')
        self.assertTrue(self.run_on(h, wizard.prepare_interactive, ['2', '', 'Lab-1', '1', 'FORMATUJ vm-storage']))
        self.assertIn('Lab-1', h.pools)


if __name__ == '__main__':
    unittest.main()
