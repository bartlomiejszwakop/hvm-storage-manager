"""Polecenia z 0.4 (create / resume) na symulatorze FakeHost — przeniesione testy transakcji i dokończenia.

Zmiana polityki 1.0: nieudane `create` nie usuwa już LV automatycznie (brak lvremove)."""
import os
import unittest

from fakehost import GIB, HostTestCase

from hvm import legacy, lvm

MOUNT = '/vm-t1'
BROKEN = b'\nUUID=deadbeef-0000-4000-8000-000000000000 /data/gone ext4 defaults 0 2\n'  # źródło, którego nie ma
MAPPER = '/dev/mapper/ubuntu--vg-t1'
ORIGINAL = (b'# /etc/fstab \xff niespodzianka spoza UTF-8\n'
            b'/dev/mapper/vg0-root / ext4 errors=remount-ro 0 1\n'
            b'UUID=5d2b3c4e-1111-4222-8333-944455556666 /boot ext4 defaults 0 2')  # bez \n na końcu


class LegacyCase(HostTestCase):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.set_fstab(ORIGINAL)

    def set_fstab(self, data):
        with open(self.h.path('/etc/fstab'), 'wb') as f:
            f.write(data)

    def fstab_bytes(self):
        with open(self.h.path('/etc/fstab'), 'rb') as f:
            return f.read()

    def backups(self):
        return [n for n in os.listdir(self.h.path('/etc')) if n.startswith('fstab.backup-hvm-')]

    def create(self, assume_yes=True, answers=None, fix_dev=False, size=2, mount=MOUNT):
        return self.run_on(self.h, lambda: legacy.create_volume('ubuntu-vg', 't1', size, mount, assume_yes, fix_dev),
                           answers)


class CreateTests(LegacyCase):
    def test_happy_path(self):
        self.assertTrue(self.create())
        uuid = self.h.lv('t1')['uuid']
        self.assertEqual(self.fstab_bytes(), ORIGINAL + b'\n' + f'UUID={uuid} {MOUNT} ext4 defaults,nofail 0 2\n'.encode())
        self.assertTrue(os.path.isdir(self.h.path(MOUNT)))
        self.assertIn(['lvcreate', '--yes', '-Wy', '-Zy', '-L', '2G', '-n', 't1', 'ubuntu-vg'], self.h.calls)
        self.assertIn(['mkfs.ext4', '-F', '-m', '1', '-L', 't1', MAPPER], self.h.calls)
        keys = self.h.keys()
        verify = [i for i, c in enumerate(self.h.calls) if c[0] == 'findmnt' and '--verify' in c]
        self.assertEqual(len(verify), 2)  # przed zmianą i po zapisie fstab
        self.assertLess(verify[0], keys.index('lvcreate'))
        self.assertTrue(keys.index('systemctl daemon-reload') < verify[1] < keys.index('mount'))
        self.assertTrue(keys.index('lvcreate') < keys.index('udevadm settle') < keys.index('wipefs'))
        for forbidden in ('lvremove', 'umount', 'vgscan'):
            self.assertNotIn(forbidden, self.h.names())
        self.assertNotIn('virsh pool-define-as', keys)
        self.assertEqual(len(self.backups()), 1)
        with open(self.h.path('/etc/' + self.backups()[0]), 'rb') as f:
            self.assertEqual(f.read(), ORIGINAL)
        self.assertIn(f'findmnt -M {MOUNT}', self.output)

    def test_confirmation_phrase(self):
        self.assertTrue(self.create(assume_yes=False, answers=[legacy.CONFIRM_PHRASE]))
        self.assertIsNotNone(self.h.lv('t1'))

    def test_wrong_phrase_cancels(self):
        self.assertFalse(self.create(assume_yes=False, answers=['tak']))
        self.assertEqual(self.h.mutating_calls(), [])
        self.assertEqual(self.fstab_bytes(), ORIGINAL)

    def assert_rolled_back(self, lv_left, mounted):
        self.assertEqual(self.fstab_bytes(), ORIGINAL)
        self.assertFalse(os.path.exists(self.h.path(MOUNT)), 'utworzone katalogi powinny zostać usunięte')
        self.assertEqual('umount' in self.h.names(), mounted)
        self.assertEqual(self.h.lv('t1') is not None, lv_left)
        self.assertNotIn('lvremove', self.h.names())

    def test_failure_points_roll_back_without_lvremove(self):
        for step, lv_left, mounted in (('lvcreate', False, False), ('lvcreate_partial', True, False),
                                       ('mkfs.ext4', True, False), ('uuid', True, False), ('verify_after', True, False),
                                       ('mount', True, False), ('mount_wrong', True, True)):
            with self.subTest(step=step):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                self.h.fail.add(step)
                self.assertFalse(self.create())
                self.assert_rolled_back(lv_left, mounted)
                if lv_left:
                    self.assertIn(f'lvremove /dev/ubuntu-vg/t1', self.output)  # wskazówka, nie akcja
                    self.assertIn('RÓŻNI SIĘ', self.output)
                else:
                    self.assertIn('Nie wprowadzono żadnych zmian — stan zweryfikowany.', self.output)
                if step in ('verify_after', 'mount', 'mount_wrong'):
                    self.assertEqual(self.h.keys().count('systemctl daemon-reload'), 2)  # zapis + przywrócenie

    def test_signature_on_new_lv_stops_before_mkfs(self):
        orig = self.h.c_lvcreate

        def lvcreate_with_garbage(a):
            res = orig(a)
            self.h.lv('t1')['fs'] = 'xfs'
            return res
        self.h.c_lvcreate = lvcreate_with_garbage
        self.assertFalse(self.create())
        self.assertNotIn('mkfs.ext4', self.h.names())
        self.assertIn('wykryto sygnaturę', self.output)

    def test_interactive_failure_offers_lvremove(self):
        self.h.fail.add('mount')
        self.assertFalse(self.create(assume_yes=False, answers=[legacy.CONFIRM_PHRASE, '']))
        self.assertIsNotNone(self.h.lv('t1'))
        self.assertIn('NIE został usunięty automatycznie', self.output)

    def test_rollback_continues_after_failed_undo_step(self):
        self.h.fail.update({'mount_wrong', 'umount'})
        self.assertFalse(self.create())
        self.assertIn('Nie wszystko udało się wycofać', self.output)
        self.assertIn(f'umount {MOUNT}', self.output)
        self.assertEqual(self.fstab_bytes(), ORIGINAL)  # dalsze kroki wycofane mimo błędu
        self.assertNotIn('stan zweryfikowany', self.output)

    def test_fstab_changed_during_run_aborts(self):
        orig = self.h.c_mkfs_ext4

        def sneaky(a):
            with open(self.h.path('/etc/fstab'), 'ab') as f:
                f.write(f'\n/dev/sdz1 {MOUNT} ext4 defaults 0 2\n'.encode())
            return orig(a)
        self.h.c_mkfs_ext4 = sneaky
        self.assertFalse(self.create())
        self.assertNotIn('mount', self.h.names())
        self.assertFalse(os.path.exists(self.h.path(MOUNT)))
        self.assertEqual(self.backups(), [])
        self.assertIn('ZMIENIONY', self.output)

    def test_validation_rejections(self):
        cases = [('istniejący LV', lambda h: h._add_lv('ubuntu-vg', 't1', 1)),
                 ('brak miejsca', lambda h: h.vgs['ubuntu-vg'].update(free=2 * GIB)),
                 ('brak VG', lambda h: h.vgs.update({'other': h.vgs.pop('ubuntu-vg')})),
                 ('zepsuty fstab', lambda h: self.set_fstab(ORIGINAL + BROKEN)),
                 ('punkt w fstab', lambda h: self.set_fstab(ORIGINAL + f'\n/dev/sdb1 {MOUNT}/ xfs defaults 0 0\n'.encode())),
                 ('niepusty katalog', lambda h: (os.makedirs(h.path(MOUNT)), open(h.path(MOUNT + '/vm.img'), 'w').close())),
                 ('poza allowlistą', None)]
        for label, setup in cases:
            with self.subTest(label=label):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                if setup:
                    setup(self.h)
                self.assertFalse(self.create(mount=MOUNT if setup else '/etc/x'))
                self.assertEqual([c for c in self.h.mutating_calls()], [])
        self.assertIn('nie jest na liście dozwolonych', self.output)

    def test_empty_dev_dir(self):
        os.makedirs(self.h.path('/dev/ubuntu-vg/t1'))
        self.assertFalse(self.create())
        self.assertIn('--fix-dev-conflict', self.output)
        self.assertNotIn('lvcreate', self.h.names())
        self.assertFalse(self.create(assume_yes=False, answers=['']))  # domyślnie NIE
        self.assertTrue(os.path.isdir(self.h.path('/dev/ubuntu-vg/t1')))
        self.assertTrue(self.create(assume_yes=False, answers=['t', legacy.CONFIRM_PHRASE]))
        self.assertEqual(self.h.nodes['/dev/ubuntu-vg/t1'][0], 'link')

    def test_dev_dir_appearing_before_lvcreate_is_caught(self):
        orig_findmnt = self.h.c_findmnt

        def sneaky(a):
            if '-J' in a and 'lvcreate' not in self.h.names() and self.h.verify_calls:
                os.makedirs(self.h.path('/dev/ubuntu-vg/t1'), exist_ok=True)
            return orig_findmnt(a)
        self.h.c_findmnt = sneaky
        self.assertFalse(self.create())
        self.assertNotIn('lvcreate', self.h.names())
        self.assertIn('blokuje', self.output)
        self.assertIn('RÓŻNI SIĘ', self.output)  # pojawił się w trakcie — zgłoszone, nie przemilczane


class ResumeTests(LegacyCase):
    def setUp(self):
        super().setUp()
        self.uuid = None

    def make(self, fs=None, mounted=None, attr_open=False, active=True):
        lv = self.h._add_lv('ubuntu-vg', 'r1', 1, fs=fs, uuid='0f3c2a1b-1234-4abc-8def-0123456789ab' if fs else None)
        lv['active'] = active
        self.uuid = lv['uuid']
        if mounted:
            os.makedirs(self.h.path('/vm-r1'), exist_ok=True)
            ours = mounted[1] == lvm.dev_paths('ubuntu-vg', 'r1')[1]
            self.h.mounts.append({'target': mounted[0], 'source': mounted[1], 'fstype': 'ext4', 'size': GIB,
                                  'uuid': (self.uuid or '') if ours else ''})
        if attr_open:
            self.h.domains['vm1'] = {'running': True, 'disks': [lvm.dev_paths('ubuntu-vg', 'r1')[1]]}
        return lv

    @property
    def line(self):
        return f'UUID={self.h.lv("r1")["uuid"]} /vm-r1 ext4 defaults,nofail 0 2\n'.encode()

    def resume(self, assume_yes=True, format_empty=False, answers=None):
        return self.run_on(self.h, lambda: legacy.resume_volume('ubuntu-vg', 'r1', '/vm-r1', assume_yes, format_empty),
                           answers)

    def assert_untouched(self):
        for forbidden in ('mkfs.ext4', 'mount', 'umount', 'lvremove', 'lvcreate'):
            self.assertNotIn(forbidden, self.h.names())
        self.assertEqual(self.fstab_bytes(), ORIGINAL)
        self.assertEqual(self.backups(), [])

    def test_empty_lv_formats_after_phrase(self):
        self.make()
        self.assertTrue(self.resume(assume_yes=False, answers=['t', 'FORMATUJ r1']))
        self.assertEqual(self.fstab_bytes(), ORIGINAL + b'\n' + self.line)
        self.assertEqual(self.h.mount_of('/vm-r1')['source'], '/dev/mapper/ubuntu--vg-r1')
        self.assertLess(self.h.names().index('wipefs'), self.h.names().index('mkfs.ext4'))
        self.assertIn('nofail', self.output)

    def test_empty_lv_wrong_phrase_or_decline(self):
        for answers in (['t', 'FORMATUJ'], ['t', 'formatuj r1'], ['']):
            with self.subTest(answers=answers):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                self.make()
                self.assertFalse(self.resume(assume_yes=False, answers=answers))
                self.assert_untouched()

    def test_empty_lv_yes_requires_format_empty(self):
        self.make()
        self.assertFalse(self.resume())
        self.assertIn('--format-empty', self.output)
        self.assert_untouched()
        self.assertTrue(self.resume(format_empty='ubuntu-vg/r1'))
        self.assertEqual(self.fstab_bytes(), ORIGINAL + b'\n' + self.line)

    def test_ext4_skips_mkfs(self):
        self.make(fs='ext4')
        self.assertTrue(self.resume())
        self.assertNotIn('mkfs.ext4', self.h.names())
        self.assertIn('Wykryto ext4 — formatowanie pominięte', self.output)
        self.assertEqual(self.fstab_bytes(), ORIGINAL + b'\n' + self.line)
        self.assertEqual(len(self.backups()), 1)

    def test_other_signatures_refused(self):
        for fs in ('xfs', 'gpt', 'LVM2_member', 'crypto_LUKS'):
            with self.subTest(fs=fs):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                self.make(fs=fs)
                self.assertFalse(self.resume(format_empty='ubuntu-vg/r1'))
                self.assert_untouched()
                self.assertIn('odmowa', self.output)

    def test_already_complete_nothing_to_do(self):
        self.make(fs='ext4', mounted=('/vm-r1', '/dev/mapper/ubuntu--vg-r1'))
        self.set_fstab(ORIGINAL + b'\n' + self.line)
        self.assertTrue(self.resume(assume_yes=False, answers=[]))
        self.assertIn('Nic do zrobienia', self.output)
        self.assertEqual(self.h.mutating_calls(), [])
        self.assertEqual(self.backups(), [])

    def test_mounted_correctly_adds_only_fstab(self):
        self.make(fs='ext4', mounted=('/vm-r1', '/dev/mapper/ubuntu--vg-r1'))
        self.assertTrue(self.resume())
        self.assertNotIn('mount', self.h.names())
        self.assertEqual(self.fstab_bytes(), ORIGINAL + b'\n' + self.line)

    def test_refusals(self):
        cases = {
            'inne urządzenie': (dict(fs='ext4', mounted=('/vm-r1', '/dev/sdz1')), 'INNE urządzenie'),
            'zamontowany gdzie indziej': (dict(fs='ext4', mounted=('/mnt/other', '/dev/mapper/ubuntu--vg-r1')),
                                          '/mnt/other'),
            'otwarty, niezamontowany': (dict(fs='ext4', attr_open=True), 'otwarty'),
            'nieaktywny': (dict(active=False), 'lvchange -ay'),
        }
        for label, (kw, text) in cases.items():
            with self.subTest(label=label):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                self.make(**kw)
                self.assertFalse(self.resume(format_empty='ubuntu-vg/r1'))
                self.assertIn(text, self.output)
                self.assertEqual(self.h.mutating_calls(), [])

    def test_lv_missing_refused(self):
        self.assertFalse(self.resume())
        self.assertIn('create', self.output)
        self.assert_untouched()

    def test_fstab_correct_not_mounted(self):
        self.make(fs='ext4')
        self.set_fstab(ORIGINAL + b'\n' + self.line)
        os.makedirs(self.h.path('/vm-r1'))
        self.assertTrue(self.resume())
        self.assertIn(['mount', '/vm-r1'], self.h.calls)
        self.assertEqual(self.backups(), [])
        self.assertIn('już poprawny', self.output)

    def test_conflicting_fstab_refused(self):
        other = '11111111-2222-3333-4444-5555555555ff'
        self.make(fs='ext4')
        for label, extra in (('inne UUID', f'UUID={other} /vm-r1 ext4 defaults 0 2\n'.encode()),
                             ('duplikat', self.line * 2),
                             ('UUID gdzie indziej', f'UUID={self.uuid} /srv/elsewhere ext4 defaults 0 2\n'.encode())):
            with self.subTest(label=label):
                self.set_fstab(ORIGINAL + b'\n' + extra)
                self.assertFalse(self.resume())
                self.assertIn('ręczny przegląd', self.output)
                self.assertNotIn('mount', self.h.names())

    def test_broken_fstab_refused_before_changes(self):
        self.make(fs='ext4')
        self.set_fstab(ORIGINAL + BROKEN)  # źródło, którego nie ma
        self.assertFalse(self.resume())
        self.assertIn('findmnt --verify', self.output)
        self.assertNotIn('mount', self.h.names())

    def test_empty_lv_with_existing_fstab_entry_refused(self):
        self.make()
        text = ORIGINAL + b'\nUUID=0f3c2a1b-1234-4abc-8def-0123456789ab /vm-r1 ext4 defaults 0 2\n'
        self.set_fstab(text)
        self.assertFalse(self.resume(format_empty='ubuntu-vg/r1'))
        self.assertNotIn('mkfs.ext4', self.h.names())
        self.assertEqual(self.fstab_bytes(), text)

    def test_rollback_never_lvremoves_and_reports_mkfs(self):
        for step, mounted in (('verify_after', False), ('mount', False), ('mount_wrong', True)):
            with self.subTest(step=step):
                self.h = self.host()
                self.set_fstab(ORIGINAL)
                self.make()
                self.h.fail.add(step)
                self.assertFalse(self.resume(format_empty='ubuntu-vg/r1'))
                self.assertIsNotNone(self.h.lv('r1'))
                self.assertEqual('umount' in self.h.names(), mounted)
                self.assertEqual(self.fstab_bytes(), ORIGINAL)
                self.assertFalse(os.path.exists(self.h.path('/vm-r1')))
                self.assertIn('mkfs.ext4 na', self.output)
                self.assertIn('RÓŻNI SIĘ', self.output)  # ext4 został — nie udajemy pełnego wycofania
                self.assertNotIn('Wszystkie zmiany wycofane', self.output)

    def test_rollback_ext4_verified_clean(self):
        self.make(fs='ext4')
        self.h.fail.add('mount')
        self.assertFalse(self.resume())
        self.assertEqual(self.fstab_bytes(), ORIGINAL)
        self.assertIn('Wszystkie zmiany wycofane — stan zweryfikowany.', self.output)

    def test_idempotent_second_run(self):
        self.make()
        self.assertTrue(self.resume(format_empty='ubuntu-vg/r1'))
        after_first, calls = self.fstab_bytes(), len(self.h.calls)
        self.assertTrue(self.resume(format_empty='ubuntu-vg/r1'))
        self.assertIn('Nic do zrobienia', self.output)
        self.assertEqual([c for c in self.h.calls[calls:] if c in self.h.mutating_calls()], [])
        self.assertEqual(self.fstab_bytes(), after_first)
        self.assertEqual(len(self.backups()), 1)


if __name__ == '__main__':
    unittest.main()
