"""Scenariusze end-to-end na symulatorze FakeHost: kreator, naprawa (przypadki A–D), pule libvirt,
błędy i przerwania, dwa hosty, dry-run, raport --diagnose. Po każdym teście HostTestCase sprawdza,
że żaden host nie otrzymał lvremove / wipefs bez -n / pool-delete / zmian chronionych pul."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fakehost import GIB, HostTestCase, BASE_FSTAB

from hvm import cli
from hvm import diagnostics as diag, disks, libvirt, runner, wizard

D_TEXT = '✓ Magazyn jest już poprawnie skonfigurowany.\nNie trzeba wykonywać dodatkowych operacji.'
MOUNT = '/vm-storage'
MAPPER = '/dev/mapper/ubuntu--vg-vm--storage'
LVPATH = '/dev/ubuntu-vg/vm-storage'


def prepare(size='1200', **kw):
    kw.setdefault('assume_yes', True)
    return lambda: wizard.prepare_cli(size, **kw)


class PrepareTests(HostTestCase):
    def test_cli_happy_path_creates_volume_mount_and_pool(self):
        h = self.host()
        self.assertTrue(self.run_on(h, prepare()))
        lv = h.lv()
        self.assertEqual((lv['size'], lv['fs']), (1200 * GIB, 'ext4'))
        self.assertIn(f'UUID={lv["uuid"]} {MOUNT} ext4 defaults,nofail 0 2\n', h.fstab())
        self.assertTrue(h.fstab().startswith(BASE_FSTAB))
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
        self.assertEqual(h.pools['HVM2-Local-1200'], {'type': 'dir', 'target': MOUNT, 'state': 'running',
                                                      'autostart': 'yes'})
        keys = h.keys()
        order = [h.index(k) for k in ('lvcreate', 'mkfs.ext4', 'systemctl daemon-reload', 'mount',
                                      'virsh pool-define-as', 'virsh pool-start', 'virsh pool-autostart',
                                      'virsh pool-refresh')]
        self.assertEqual(order, sorted(order))
        self.assertLess(keys.index('wipefs'), keys.index('mkfs.ext4'))
        self.assertIn(['lvcreate', '--yes', '-Wy', '-Zy', '-L', '1200G', '-n', 'vm-storage', 'ubuntu-vg'], h.calls)
        self.assertIn(['mkfs.ext4', '-F', '-m', '1', '-L', 'vm-storage', MAPPER], h.calls)
        self.assertIn(['virsh', '-c', 'qemu:///system', 'pool-define-as', '--name', 'HVM2-Local-1200', '--type', 'dir', '--target', MOUNT],
                      h.calls)
        self.assertEqual(len([n for n in os.listdir(h.path('/etc')) if n.startswith('fstab.backup-hvm-')]), 1)
        self.assertIn('RAPORT KROKÓW', self.output)
        self.assertNotIn('✗', self.output)
        self.assertIn('nofail', self.output)

    def test_interactive_wizard_texts_and_execution(self):
        h = self.host()
        answers = ['2', '', '1', 'FORMATUJ vm-storage']
        self.assertTrue(self.run_on(h, wizard.prepare_interactive, answers))
        for text in ('NOWY MAGAZYN DANYCH', 'Wykryto:', 'Serwer: HVM2', 'Dostępne miejsce: 1387 GiB',
                     'Ile miejsca przeznaczyć na maszyny?', '[1] 1000 GiB\n', '[2] 1200 GiB — zalecane\n',
                     '[3] Maksymalna dostępna przestrzeń (1373 GiB)', '[4] Wpisz własną wartość', '[0] Wróć',
                     'PODSUMOWANIE OPERACJI\nSerwer: HVM2\nNowy magazyn: HVM2-Local-1200\nPojemność: 1200 GiB\n'
                     'Lokalizacja: /vm-storage\nOperacja utworzy nowy magazyn i zarejestruje\njego pulę w lokalnym '
                     'libvirt.\nDysk systemowy i istniejące maszyny\nnie zostaną celowo zmodyfikowane.',
                     '[1] Wykonaj', '[2] Pokaż szczegóły techniczne', '[0] Anuluj', 'sda: dysk systemowy'):
            self.assertIn(text, self.output)
        self.assertIn('HVM2-Local-1200', h.pools)

    def test_custom_size_and_custom_name(self):
        h = self.host()
        self.assertTrue(self.run_on(h, wizard.prepare_interactive,
                                    ['4', '5000', '700', 'Lab-Store', '1', 'FORMATUJ vm-storage']))
        self.assertIn('maksymalnie 1373 GiB', self.output)
        self.assertEqual(h.lv()['size'], 700 * GIB)
        self.assertIn('Lab-Store', h.pools)

    def test_details_then_cancel_changes_nothing(self):
        h = self.host()
        self.assertFalse(self.run_on(h, wizard.prepare_interactive, ['2', '', '2', '0']))
        self.assertIn('lvcreate --yes -Wy -Zy -L 1200G -n vm-storage ubuntu-vg', self.output)
        self.assertIn('virsh -c qemu:///system pool-define-as --name HVM2-Local-1200 --type dir --target /vm-storage',
                      self.output)
        self.assertIn('Anulowano', self.output)
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(h.fstab(), BASE_FSTAB)

    def test_wrong_format_phrase_cancels(self):
        h = self.host()
        self.assertFalse(self.run_on(h, wizard.prepare_interactive, ['2', '', '1', 'FORMATUJ']))
        self.assertEqual(h.mutating_calls(), [])

    def test_size_menu_hides_options_that_do_not_fit(self):
        h = self.host(vg_free=900 * GIB)
        self.assertFalse(self.run_on(h, wizard.prepare_interactive, ['0']))
        self.assertIn('[1] 800 GiB — zalecane', self.output)
        self.assertIn('[2] Maksymalna dostępna przestrzeń (891 GiB)', self.output)
        self.assertNotIn('1200 GiB', self.output)
        self.assertNotIn('1000 GiB', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_cli_size_too_big_refused(self):
        h = self.host(vg_free=900 * GIB)
        self.assertFalse(self.run_on(h, prepare('1200')))
        self.assertIn('maksymalnie 891 GiB', self.output)
        self.assertNotIn('lvcreate', h.names())

    def test_cli_recommended_and_max(self):
        h = self.host(vg_free=900 * GIB)
        self.assertTrue(self.run_on(h, prepare('recommended')))
        self.assertEqual(h.lv()['size'], 800 * GIB)
        self.assertIn('HVM2-Local-800', h.pools)
        h2 = self.host(hostname='srv-a')
        self.assertTrue(self.run_on(h2, prepare('max')))
        self.assertEqual(h2.lv()['size'], 1373 * GIB)
        self.assertIn('SRV-A-Local-1373', h2.pools)

    def test_no_free_space_refused(self):
        h = self.host(vg_free=0)
        self.assertFalse(self.run_on(h, wizard.prepare_interactive, []))
        self.assertIn('Brak wolnego miejsca', self.output)
        self.assertIn('sdb: pusty dysk — tylko informacja', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_libvirt_down_refuses_before_changes(self):
        h = self.host(libvirt=False)
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIn('libvirt nie odpowiada', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_existing_lv_goes_to_repair_never_formats(self):
        h = self.host()
        h.add_storage(mounted=False, fstab=False, pool=None)
        self.assertTrue(self.run_on(h, prepare()))
        self.assertIn('już istnieje', self.output)
        for forbidden in ('lvcreate', 'mkfs.ext4'):
            self.assertNotIn(forbidden, h.names())
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
        self.assertIn('HVM2-Local-1200', h.pools)


class CaseDTests(HostTestCase):
    def test_rerun_after_prepare_does_nothing(self):
        h = self.host()
        self.assertTrue(self.run_on(h, prepare()))
        before = len(h.calls)
        self.assertTrue(self.run_on(h, wizard.prepare_interactive, []))
        self.assertIn(D_TEXT, self.output)
        self.assertEqual([c for c in h.calls[before:] if c in h.mutating_calls()], [])
        for line in ('✓ Wolumen Linux gotowy', '✓ Pula libvirt aktywna',
                     '? Rejestracja w panelu Morpheus niezweryfikowana'):
            self.assertIn(line, self.output)

    def test_repair_on_ready_storage(self):
        h = self.host()
        h.add_storage()
        self.assertTrue(self.run_on(h, wizard.repair, []))
        self.assertIn(D_TEXT, self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_ready_storage_without_pool_offers_pool_creation(self):
        h = self.host()
        h.add_storage(pool=None)
        self.assertTrue(self.run_on(h, wizard.repair, ['1']))
        self.assertIn('brak puli libvirt', self.output)
        self.assertEqual(h.pools['HVM2-Local-1200']['state'], 'running')
        for forbidden in ('mkfs.ext4', 'mount', 'lvcreate'):
            self.assertNotIn(forbidden, h.names())


class CaseATests(HostTestCase):
    """/dev/<vg>/<lv> jest katalogiem (błąd lvcreate „not found: device not cleared”)."""

    def setUp(self):
        super().setUp()
        self.h = self.host()
        os.makedirs(self.h.path(LVPATH))

    def test_yes_without_flag_refuses(self):
        self.assertFalse(self.run_on(self.h, prepare()))
        self.assertIn('--fix-dev-conflict', self.output)
        self.assertNotIn('lvcreate', self.h.names())
        self.assertTrue(os.path.isdir(self.h.path(LVPATH)))

    def test_fix_flag_removes_empty_dir_then_creates(self):
        self.assertTrue(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('device not cleared', self.output)
        self.assertEqual(self.h.nodes[LVPATH][0], 'link')
        self.assertLess(self.output.index('Usunięcie pustego katalogu'), self.output.index('Utworzenie wolumenu'))

    def test_interactive_confirmation(self):
        self.assertTrue(self.run_on(self.h, wizard.prepare_interactive, ['2', '', 't', '1', 'FORMATUJ vm-storage']))
        self.assertFalse(os.path.isdir(self.h.path(LVPATH)))
        self.assertIsNotNone(self.h.lv())

    def test_interactive_decline_default_no(self):
        self.assertFalse(self.run_on(self.h, wizard.prepare_interactive, ['2', '', '']))
        self.assertTrue(os.path.isdir(self.h.path(LVPATH)))
        self.assertEqual(self.h.mutating_calls(), [])

    def test_non_empty_dir_refused(self):
        with open(self.h.path(LVPATH + '/x'), 'w') as f:
            f.write('x')
        self.assertFalse(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('niepusty', self.output)
        self.assertTrue(os.path.exists(self.h.path(LVPATH + '/x')))
        self.assertEqual(self.h.mutating_calls(), [])

    def test_dir_in_use_by_process_refused(self):
        self.h.busy[LVPATH] = [4242]
        self.assertFalse(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('PID 4242', self.output)
        self.assertTrue(os.path.isdir(self.h.path(LVPATH)))

    def test_cannot_check_processes_refused(self):
        with mock.patch.object(self.h, 'processes_using', return_value=None):
            self.assertFalse(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('nie można sprawdzić', self.output)
        self.assertTrue(os.path.isdir(self.h.path(LVPATH)))

    def test_mountpoint_dir_refused(self):
        real = self.h.path(LVPATH)
        with mock.patch('os.path.ismount', side_effect=lambda p: os.path.normpath(p) == os.path.normpath(real)):
            self.assertFalse(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('punktem montowania', self.output)

    def test_stale_symlink_refused(self):
        os.rmdir(self.h.path(LVPATH))
        self.h.nodes[LVPATH] = ('link', '../dm-77')
        self.assertFalse(self.run_on(self.h, prepare(fix_dev=True)))
        self.assertIn('nieaktualny', self.output)
        self.assertIn('../dm-77', self.output)

    def test_repair_only_frees_path(self):
        self.assertTrue(self.run_on(self.h, lambda: wizard.repair(assume_yes=True, fix_dev=True)))
        self.assertFalse(os.path.exists(self.h.path(LVPATH)))
        self.assertIn('teraz można utworzyć magazyn (opcja 1)', self.output)
        self.assertNotIn('lvcreate', self.h.names())

    def test_lv_exists_with_dir_instead_of_link(self):
        """A+B: LV jest w LVM, mapper zgodny, a /dev/<vg>/<lv> to pusty katalog."""
        os.rmdir(self.h.path(LVPATH))
        self.h.add_storage(mounted=False)
        del self.h.nodes[LVPATH]
        os.makedirs(self.h.path(LVPATH))
        self.assertTrue(self.run_on(self.h, lambda: wizard.repair(assume_yes=True, fix_dev=True)))
        self.assertIn(['vgscan', '--mknodes'], self.h.calls)
        self.assertEqual(self.h.nodes[LVPATH][0], 'link')
        self.assertEqual(self.h.mount_of(MOUNT)['source'], MAPPER)


class CaseBTests(HostTestCase):
    def test_missing_lv_symlink_uses_mapper_and_mknodes(self):
        h = self.host()
        h.add_storage(mounted=False)
        del h.nodes[LVPATH]
        self.assertTrue(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('brak dowiązania /dev/ubuntu-vg/vm-storage', self.output)
        self.assertIn(['vgscan', '--mknodes'], h.calls)
        self.assertEqual(h.nodes[LVPATH][0], 'link')
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
        self.assertNotIn('mkfs.ext4', h.names())

    def test_mknodes_failure_is_not_fatal(self):
        h = self.host()
        h.add_storage(mounted=False)
        del h.nodes[LVPATH]
        with mock.patch.object(h, 'c_vgscan', return_value=(5, '', 'boom')):
            self.assertTrue(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('○ Odtworzenie dowiązania', self.output)
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)

    def test_mapper_inconsistent_with_lvm_refused(self):
        h = self.host()
        h.add_storage(mounted=False)
        del h.nodes[LVPATH]
        h.nodes[f'/dev/dm-{h.lv()["minor"]}'] = ('block', '253:99')
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('niespójność device-mapper', self.output)
        self.assertEqual(h.mutating_calls(), [])


class CaseCTests(HostTestCase):
    def test_not_mounted_fstab_missing(self):
        h = self.host()
        h.add_storage(mounted=False, fstab=False)
        self.assertTrue(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        uuid = h.lv()['uuid']
        self.assertEqual(h.fstab(), BASE_FSTAB + f'UUID={uuid} {MOUNT} ext4 defaults,nofail 0 2\n')
        self.assertEqual(h.mount_of(MOUNT)['uuid'], uuid)
        self.assertNotIn('mkfs.ext4', h.names())
        self.assertIn('Wykryto ext4 — formatowanie pominięte', self.output)

    def test_not_mounted_fstab_ok_only_mounts(self):
        h = self.host()
        h.add_storage(mounted=False)
        before = h.fstab()
        self.assertTrue(self.run_on(h, wizard.repair, ['1']))
        self.assertEqual(h.fstab(), before)
        self.assertIn(['mount', MOUNT], h.calls)
        self.assertNotIn('systemctl', h.names())

    def test_empty_lv_requires_format_phrase(self):
        h = self.host()
        h.add_storage(fs=None, mounted=False, fstab=False)
        self.assertFalse(self.run_on(h, wizard.repair, ['1', 'FORMATUJ']))
        self.assertEqual(h.mutating_calls(), [])
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('--format-empty', self.output)
        self.assertTrue(self.run_on(h, wizard.repair, ['1', 'FORMATUJ vm-storage']))
        self.assertEqual(h.lv()['fs'], 'ext4')

    def test_foreign_signature_refused(self):
        h = self.host()
        h.add_storage(fs='gpt', mounted=False, fstab=False)
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True, format_empty='ubuntu-vg/vm-storage')))
        self.assertIn('odmowa', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_other_device_mounted_refused(self):
        h = self.host()
        h.add_storage(mounted=False)
        h.mounts.append({'target': MOUNT, 'source': '/dev/sdz1', 'fstype': 'ext4', 'size': GIB, 'uuid': ''})
        self.assertFalse(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('INNE urządzenie', self.output)
        self.assertEqual(h.mutating_calls(), [])


class PoolTests(HostTestCase):
    def test_same_name_same_target_inactive_is_started_not_redefined(self):
        h = self.host()
        h.add_storage(pool_state='inactive', autostart='no')
        self.assertTrue(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertNotIn('virsh pool-define-as', h.keys())
        for key in ('virsh pool-start', 'virsh pool-autostart', 'virsh pool-refresh'):
            self.assertIn(key, h.keys())
        self.assertEqual(h.pools['HVM2-Local-1200']['state'], 'running')

    def test_same_name_other_target_refused(self):
        h = self.host()
        os.makedirs(h.path('/srv/other'))
        h.pools['HVM2-Local-1200'] = {'type': 'dir', 'target': '/srv/other', 'state': 'running', 'autostart': 'yes'}
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIn('już istnieje i wskazuje na /srv/other', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_other_pool_on_same_dir_is_reused(self):
        h = self.host()
        os.makedirs(h.path(MOUNT))
        h.pools['vmstore'] = {'type': 'dir', 'target': MOUNT + '/', 'state': 'running', 'autostart': 'yes'}
        self.assertTrue(self.run_on(h, prepare()))
        self.assertIn('zostanie ona użyta', self.output)
        self.assertNotIn('virsh pool-define-as', h.keys())
        self.assertIn(['virsh', '-c', 'qemu:///system', 'pool-refresh', '--pool', 'vmstore'], h.calls)
        self.assertNotIn('HVM2-Local-1200', h.pools)

    def test_protected_pool_names_and_targets(self):
        h = self.host()
        self.assertFalse(self.run_on(h, lambda: wizard.prepare_cli('100', pool='local', assume_yes=True)))
        self.assertIn('chronionej', self.output)
        self.assertFalse(self.run_on(h, lambda: wizard.prepare_cli('100', mount='/var/lib/libvirt/images/pula1',
                                                                    assume_yes=True)))
        self.assertIn('chronionym katalogiem', self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_protected_pool_never_modified_even_if_inactive(self):
        h = self.host()
        h.pools['local']['state'] = 'inactive'
        h.add_storage()
        self.assertTrue(self.run_on(h, wizard.check_storages, ['t']))
        self.assertFalse(any('local' in c for c in h.calls if h.key(c) == 'virsh pool-refresh'))
        self.assertIn(['virsh', '-c', 'qemu:///system', 'pool-refresh', '--pool', 'HVM2-Local-1200'], h.calls)


class FailureTests(HostTestCase):
    def assert_phase1_clean(self, h):
        self.assertEqual(h.fstab(), BASE_FSTAB)
        self.assertFalse(os.path.exists(h.path(MOUNT)))
        self.assertIsNone(h.mount_of(MOUNT))
        self.assertNotIn('lvremove', h.names())

    def test_lvcreate_error(self):
        h = self.host()
        h.fail.add('lvcreate')
        self.assertFalse(self.run_on(h, prepare()))
        self.assert_phase1_clean(h)
        self.assertIsNone(h.lv())
        self.assertIn('✗ Utworzenie wolumenu LVM', self.output)
        self.assertIn('○ Formatowanie ext4', self.output)
        self.assertIn('Nie wprowadzono żadnych zmian — stan zweryfikowany.', self.output)
        self.assertIn('opcję 2', self.output)

    def test_lvcreate_partial_keeps_lv(self):
        h = self.host()
        h.fail.add('lvcreate_partial')
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIsNotNone(h.lv())
        self.assertIn('częściowo', self.output)
        self.assertIn('lvremove /dev/ubuntu-vg/vm-storage', self.output)  # tylko jako wskazówka
        self.assertIn('RÓŻNI SIĘ', self.output)
        self.assert_phase1_clean(h)

    def test_partial_lv_interactive_offer_keep_or_remove(self):
        h = self.host()
        h.fail.add('lvcreate_partial')
        self.assertFalse(self.run_on(h, wizard.prepare_interactive, ['2', '', '1', 'FORMATUJ vm-storage', '']))
        self.assertIsNotNone(h.lv())
        self.assertIn('NIE został usunięty automatycznie', self.output)
        self.assertIn('Wolumen pozostawiony', self.output)
        h2 = self.host()
        h2.fail.add('lvcreate_partial')
        h2.allow_lvremove = True
        self.assertFalse(self.run_on(h2, wizard.prepare_interactive,
                                     ['2', '', '1', 'FORMATUJ vm-storage', 'USUN vm-storage']))
        self.assertIsNone(h2.lv())
        self.assertIn(['lvremove', '-y', 'ubuntu-vg/vm-storage'], h2.calls)

    def test_mkfs_error_keeps_lv_no_lvremove(self):
        h = self.host()
        h.fail.add('mkfs.ext4')
        self.assertFalse(self.run_on(h, prepare()))
        self.assertIsNotNone(h.lv())
        self.assertIn('✗ Formatowanie ext4', self.output)
        self.assertIn('Pełne wycofanie nie jest możliwe', self.output)
        self.assert_phase1_clean(h)

    def test_failures_after_fstab_roll_back(self):
        for fail, mounted in (('verify_after', False), ('mount', False), ('mount_wrong', True)):
            with self.subTest(fail=fail):
                h = self.host()
                h.fail.add(fail)
                self.assertFalse(self.run_on(h, prepare()))
                self.assert_phase1_clean(h)
                self.assertEqual('umount' in h.names(), mounted)
                self.assertIn('↺', self.output)
                self.assertNotIn('virsh pool-define-as', h.keys())

    def test_rerun_after_mkfs_failure_completes_with_repair(self):
        h = self.host()
        h.fail.add('mount')
        self.assertFalse(self.run_on(h, prepare()))
        h.fail.clear()
        self.assertTrue(self.run_on(h, lambda: wizard.repair(assume_yes=True)))
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
        self.assertEqual(h.names().count('mkfs.ext4'), 1)
        self.assertIn('HVM2-Local-1200', h.pools)

    def test_pool_failure_keeps_volume_and_undefines_only_own_pool(self):
        h = self.host()
        h.fail.add('virsh pool-start')
        self.assertFalse(self.run_on(h, prepare()))
        self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
        self.assertNotIn('HVM2-Local-1200', h.pools)
        self.assertIn(['virsh', '-c', 'qemu:///system', 'pool-undefine', '--pool', 'HVM2-Local-1200'], h.calls)
        self.assertIn('rejestracja puli libvirt nie powiodła się', self.output)
        self.assertIn('✓ Montowanie', self.output)
        for p in ('local', 'morpheus-images', 'morpheus-cloud-init'):
            self.assertIn(p, h.pools)

    def test_interrupt_at_every_step(self):
        steps = ['lvcreate', 'udevadm settle', 'wipefs', 'mkfs.ext4', 'systemctl daemon-reload', 'mount',
                 'virsh pool-define-as', 'virsh pool-start', 'virsh pool-autostart', 'virsh pool-refresh']
        for key in steps:
            with self.subTest(step=key):
                h = self.host()
                h.interrupt = key
                self.assertFalse(self.run_on(h, prepare()))
                self.assertIsNone(h.interrupt, 'przerwanie nie nastąpiło')
                self.assertIn('RAPORT KROKÓW', self.output)
                self.assertIn('przerwano (Ctrl+C)', self.output)
                self.assertNotIn('lvremove', h.names())
                if key.startswith('virsh'):
                    self.assertEqual(h.mount_of(MOUNT)['source'], MAPPER)
                    self.assertNotIn('HVM2-Local-1200', h.pools)
                else:
                    self.assert_phase1_clean(h)
                    self.assertEqual(h.lv() is not None, key != 'lvcreate')


class TwoHostsTests(HostTestCase):
    def test_identical_paths_different_disks_get_own_names_and_pools(self):
        h1 = self.host(hostname='hvm1', vg_free=1500 * GIB, data_disk='nvme0n1', ip='10.10.0.11')
        h2 = self.host(hostname='node2.lab.local', vg_free=1387 * GIB, system_disk='vda', data_disk=None)
        self.assertTrue(self.run_on(h1, prepare()))
        self.assertTrue(self.run_on(h2, prepare()))
        self.assertIn('HVM1-Local-1200', h1.pools)
        self.assertNotIn('HVM2-Local-1200', h1.pools)
        self.assertIn('HVM2-Local-1200', h2.pools)
        self.assertNotIn('HVM1-Local-1200', h2.pools)
        self.assertFalse(any('HVM2' in ' '.join(c) for c in h1.calls))
        self.assertFalse(any('HVM1' in ' '.join(c) for c in h2.calls))
        self.assertNotEqual(h1.lv()['uuid'], h2.lv()['uuid'])
        self.assertTrue(self.run_on(h1, lambda: wizard.repair(assume_yes=True)))
        self.assertIn('HVM1-Local-1200', self.output)
        self.assertNotIn('HVM2', self.output)


class DisksTests(HostTestCase):
    def test_system_disk_blocked_and_raw_disk_info_only(self):
        h = self.host()
        h.disks.append({'name': 'sdc', 'size': 50 * GIB, 'parts': []})
        h.domains['morpheus-manager']['disks'].append('/dev/sdc')
        with h.active():
            data = disks.scan(libvirt.vm_sources())
        status = {d['name']: d['status'] for d in data['disks']}
        self.assertEqual(status, {'sda': 'system', 'sdb': 'free', 'sdc': 'vm'})
        sda = next(d for d in data['disks'] if d['name'] == 'sda')
        self.assertIn('/', sda['mounts'])
        self.assertEqual([v['name'] for v in data['candidates']], ['ubuntu-vg'])
        self.assertTrue(data['vgs'][0]['system'])
        self.assertEqual(data['vgs'][0]['max_gib'], 1373)
        self.run_on(h, prepare())
        for c in h.mutating_calls():
            self.assertFalse(any(arg.startswith(('/dev/sd', '/dev/sdc')) for arg in c), c)


class CliTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.log = tempfile.NamedTemporaryFile(prefix='hvmlog-', delete=False).name
        self.addCleanup(lambda: os.path.exists(self.log) and os.unlink(self.log))
        self.addCleanup(runner.setup_logging, os.devnull)

    def cli(self, h, argv, answers=None):
        with mock.patch.object(runner, 'acquire_lock', return_value=True):
            return self.run_on(h, lambda: cli.main(['--log-file', self.log] + argv), answers)

    def test_dry_run_makes_no_mutating_calls(self):
        h = self.host()
        os.makedirs(h.path(LVPATH))
        for argv in (['--dry-run', 'prepare', '--yes', '--fix-dev-conflict'], ['prepare', '--size', 'max', '--dry-run', '--yes', '--fix-dev-conflict'],
                     ['--dry-run', 'repair', '--yes', '--fix-dev-conflict'], ['--dry-run', 'create', '--vg', 'ubuntu-vg', '--name', 'x1', '--size',
                                               '5', '--mount', '/vm-x1', '--yes']):
            with self.subTest(argv=argv):
                self.cli(h, argv)
                self.assertEqual(h.mutating_calls(), [])
                self.assertEqual(h.fstab(), BASE_FSTAB)
                self.assertFalse(os.path.exists(h.path(MOUNT)))
                self.assertTrue(os.path.isdir(h.path(LVPATH)))

    def test_dry_run_prints_plan(self):
        h = self.host()
        self.assertEqual(self.cli(h, ['--dry-run', 'prepare', '--yes']), 0)
        for text in ('[DRY-RUN] lvcreate --yes -Wy -Zy -L 1200G -n vm-storage ubuntu-vg', '[DRY-RUN] mkfs.ext4',
                     '[DRY-RUN] virsh -c qemu:///system pool-define-as --name HVM2-Local-1200 --type dir --target /vm-storage',
                     'DRY-RUN zakończony — nic nie zostało zmienione.'):
            self.assertIn(text, self.output)
        self.assertEqual(h.mutating_calls(), [])

    def test_prepare_and_repair_cli(self):
        h = self.host()
        self.assertEqual(self.cli(h, ['prepare', '--size', '1200', '--yes']), 0)
        self.assertEqual(self.cli(h, ['repair', '--yes']), 0)
        self.assertIn(D_TEXT, self.output)

    def test_diagnose_json(self):
        h = self.host()
        h.add_storage()
        self.assertEqual(self.cli(h, ['--diagnose', '--json']), 0)
        rep = json.loads(self.output)
        self.assertEqual(rep['version'], '1.0.0')
        self.assertEqual(rep['host']['hostname'], 'hvm2')
        self.assertEqual(rep['host']['ip'], '10.10.0.12')
        self.assertEqual(rep['host']['os'], 'Ubuntu 24.04.1 LTS')
        st = rep['storages'][0]
        self.assertEqual(st['name'], 'HVM2-Local-1200')
        self.assertEqual((st['linux']['state'], st['libvirt']['state'], st['morpheus']['state']),
                         ('ok', 'ok', 'unknown'))
        for s in rep['storages']:
            for key in ('linux', 'libvirt', 'morpheus'):
                self.assertIn(s[key]['state'], ('ok', 'error', 'unknown'))
        self.assertTrue(any(p['name'] == 'local' and p['protected'] for p in rep['pools']))
        self.assertEqual(h.mutating_calls(), [])

    def test_diagnose_json_missing_storage_and_non_root(self):
        h = self.host(libvirt=False)
        with mock.patch.object(runner, 'is_root', return_value=False):
            with h.active(), mock.patch.object(runner, 'is_root', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['--log-file', self.log, '--diagnose', '--json']), 0)
        rep = json.loads(out.getvalue())
        st = rep['storages'][0]
        self.assertEqual((st['linux']['state'], st['libvirt']['state'], st['morpheus']['state']),
                         ('error', 'unknown', 'unknown'))
        self.assertTrue(rep['warnings'])

    def test_diagnose_text(self):
        h = self.host()
        h.add_storage()
        self.assertEqual(self.cli(h, ['--diagnose']), 0)
        self.assertIn('RAPORT STANU SERWERA', self.output)
        self.assertIn('? Rejestracja w panelu Morpheus niezweryfikowana', self.output)

    def test_menu_header_and_options(self):
        h = self.host()
        h.add_storage()
        self.assertEqual(self.cli(h, [], ['0']), 0)
        for text in ('HVM STORAGE MANAGER 1.0', 'Zarządzanie dyskami HPE HVM', 'Serwer: hvm2', 'Adres IP: 10.10.0.12',
                     'System: Ubuntu 24.04.1 LTS', 'STATUS MAGAZYNÓW', '✓ Dysk systemowy: sda',
                     '✓ Magazyn VM: 1200 GiB', '✓ Zamontowany: /vm-storage', '✓ Dostępny w libvirt: tak',
                     '1. Przygotuj nowy magazyn dla VM', '2. Napraw lub dokończ konfigurację',
                     '3. Pokaż dostępne dyski i miejsce', '4. Sprawdź magazyny Morpheus', '5. Sprawdź stan serwera',
                     '6. Zaawansowane', '0. Wyjście', '╔'):
            self.assertIn(text, self.output)

    def test_menu_header_missing_storage(self):
        h = self.host()
        self.assertEqual(self.cli(h, [], ['0']), 0)
        self.assertIn('✗ Magazyn VM: nie utworzono', self.output)
        self.assertIn('✗ Niezamontowany: /vm-storage', self.output)

    def test_menu_navigation_read_only_screens(self):
        h = self.host()
        h.add_storage()
        answers = ['3', '', '5', '', '4', 'n', '', '6', '1', '3', '4', '5', '6', '8', '0', '', '0']
        self.assertEqual(self.cli(h, [], answers), 0)
        self.assertIn('GRUPY LVM', self.output)
        self.assertIn('RAPORT STANU SERWERA', self.output)
        self.assertIn('MAGAZYNY NA TYM HOŚCIE', self.output)
        self.assertIn('$ lsblk', self.output)  # pokazywanie poleceń włączone opcją 1
        self.assertEqual(h.mutating_calls(), [])

    def test_export_json_report(self):
        h = self.host()
        h.add_storage()
        with h.active(), contextlib.redirect_stdout(io.StringIO()):
            path = diag.export_report('/etc/report.json')
        self.assertEqual(path, '/etc/report.json')
        with open(h.path('/etc/report.json'), encoding='utf-8') as f:
            rep = json.load(f)
        self.assertEqual(rep['storages'][0]['morpheus']['state'], 'unknown')

    def test_legacy_create_and_resume_cli(self):
        h = self.host()
        self.assertEqual(self.cli(h, ['create', '--vg', 'ubuntu-vg', '--name', 't1', '--size', '2', '--mount',
                                      '/vm-t1', '--yes']), 0)
        self.assertEqual(h.mount_of('/vm-t1')['source'], '/dev/mapper/ubuntu--vg-t1')
        self.assertNotIn('virsh pool-define-as', h.keys())  # create z 0.4 nie tworzy pul
        self.assertEqual(self.cli(h, ['resume', '--vg', 'ubuntu-vg', '--name', 't1', '--mount', '/vm-t1', '--yes']), 0)
        self.assertIn('Nic do zrobienia', self.output)


if __name__ == '__main__':
    unittest.main()
