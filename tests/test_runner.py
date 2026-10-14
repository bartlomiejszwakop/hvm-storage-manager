"""Warstwa poleceń (run, allowlista, dry-run, środowisko), zasady safety, zapis plików i silnik kroków."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fakehost  # noqa: F401 — ustawia sys.path
from hvm import mount as mnt, runner, safety, ui


def cp(args, rc=0, out='', errt=''):
    return subprocess.CompletedProcess(list(args), rc, out, errt)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


V = ['virsh', '-c', safety.VIRSH_URI]


class FakePopen:
    """Atrapa subprocess.Popen: communicate() może najpierw rzucić KeyboardInterrupt (Ctrl+C)."""
    instances = []

    def __init__(self, args, rc=0, out='', err='', interrupts=0, **kwargs):
        self.args, self.kwargs, self.returncode = args, kwargs, None
        self._rc, self._out, self._err, self._interrupts = rc, out, err, interrupts
        self.communicate_calls = 0
        FakePopen.instances.append(self)

    def communicate(self):
        self.communicate_calls += 1
        if self._interrupts:
            self._interrupts -= 1
            raise KeyboardInterrupt
        self.returncode = self._rc
        return self._out, self._err


def popen(**behaviour):
    return mock.patch.object(runner.subprocess, 'Popen', side_effect=lambda args, **kw: FakePopen(args, **behaviour, **kw))


class RunTests(unittest.TestCase):
    def setUp(self):
        safety.ARMED.clear()
        self.addCleanup(safety.ARMED.clear)
        FakePopen.instances = []
        # żaden test tej klasy nie może uruchomić prawdziwego procesu
        for p in (mock.patch.object(runner.subprocess, 'run', side_effect=AssertionError('subprocess.run!')),
                  mock.patch.object(runner.subprocess, 'Popen', side_effect=AssertionError('Popen!')),
                  mock.patch.object(runner, 'DRY_RUN', False)):
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_mutating_not_executed(self):
        with mock.patch.object(runner, 'DRY_RUN', True), quiet() as out:
            p = runner.run(['lvcreate', '-L', '1G', '-n', 'x', 'vg0'])
        self.assertEqual(p.returncode, 0)
        self.assertIn('[DRY-RUN] lvcreate -L 1G -n x vg0', out.getvalue())

    def test_mutating_is_derived_from_command(self):
        for args in (['mkfs.ext4', '/dev/x'], ['mount', '/vm'], ['umount', '/vm'], ['systemctl', 'daemon-reload'],
                     ['vgscan', '--mknodes'], V + ['pool-start', '--pool', 'p'], V + ['pool-refresh', '--pool', 'p'],
                     V + ['pool-define-as', '--name', 'p', '--type', 'dir', '--target', '/vm'],
                     ['virsh', 'pool-start', 'p']):  # niepoprawna forma też liczy się jako zmieniająca
            self.assertTrue(safety.is_mutating(args), args)
        for args in (['lvs'], ['findmnt', '-M', '/vm'], V + ['pool-list'], ['udevadm', 'settle'],
                     ['blkid', '-p', '/dev/x'], ['wipefs', '-n', '/dev/x'], ['dmsetup', 'ls']):
            self.assertFalse(safety.is_mutating(args), args)

    def test_dry_run_read_only_executed(self):
        with mock.patch.object(runner, 'DRY_RUN', True), \
                mock.patch.object(runner.subprocess, 'run', return_value=cp(['vgs'], 0, 'ok')) as sp:
            self.assertEqual(runner.run(['vgs']).stdout, 'ok')
        sp.assert_called_once()

    def test_safe_environment_and_stdin(self):
        with mock.patch.object(runner.subprocess, 'run', return_value=cp([], 0)) as sp:
            runner.run(['lsblk', 5])
        args, kwargs = sp.call_args
        self.assertEqual(args[0], ['lsblk', '5'])
        self.assertIs(kwargs['env'], runner.ENV)
        self.assertEqual(runner.ENV['LC_ALL'], 'C')
        self.assertEqual(runner.ENV['PATH'], '/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', kwargs)

    def test_mutating_runs_in_new_session_without_shell(self):
        with popen(rc=0):
            runner.run(['mount', '/vm'])
        p = FakePopen.instances[0]
        self.assertEqual(p.args, ['mount', '/vm'])
        self.assertTrue(p.kwargs['start_new_session'])
        self.assertIs(p.kwargs['env'], runner.ENV)
        self.assertEqual(p.kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', p.kwargs)

    def test_ctrl_c_waits_for_mutating_child_then_reraises(self):
        with popen(rc=0, out='done', interrupts=2), quiet() as out:
            with self.assertRaises(KeyboardInterrupt):
                runner.run(['mount', '/vm'])
        p = FakePopen.instances[0]
        self.assertEqual(p.communicate_calls, 3)  # dwa Ctrl+C, proces doczekany do końca
        self.assertEqual(p.returncode, 0)
        self.assertIn('czekam na zakończenie', out.getvalue())

    def test_missing_binary_returns_127(self):
        with mock.patch.object(runner.subprocess, 'run', side_effect=FileNotFoundError('no such file')):
            p = runner.run(['lsblk'], check=False)
            self.assertEqual(p.returncode, 127)
            self.assertIn('no such file', p.stderr)
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                runner.run(['lsblk'])
        self.assertEqual(ctx.exception.returncode, 127)
        with mock.patch.object(runner.subprocess, 'Popen', side_effect=FileNotFoundError('x')):
            self.assertEqual(runner.run(['mount', '/vm'], check=False).returncode, 127)

    def test_check_raises(self):
        with mock.patch.object(runner.subprocess, 'run', return_value=cp(['lvs'], 3, 'o', 'boom')):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                runner.run(['lvs'])
            self.assertEqual(runner.run(['lvs'], check=False).returncode, 3)
        e = ctx.exception
        self.assertEqual((e.returncode, e.cmd, e.stderr), (3, ['lvs'], 'boom'))
        self.assertIn('boom', ui.describe(e))

    def test_show_commands(self):
        with mock.patch.object(runner, 'SHOW_COMMANDS', True), quiet() as out, \
                mock.patch.object(runner.subprocess, 'run', return_value=cp([], 0)):
            runner.run(['lvs', '-o', 'lv_name'])
        self.assertIn('$ lvs -o lv_name', out.getvalue())

    def refused(self, args):
        with self.assertRaises(safety.CommandRefused):
            runner.run(args)  # setUp: każde uruchomienie procesu = AssertionError

    def test_allowlist_refuses_unknown_binaries(self):
        for args in (['rm', '-rf', '/'], ['bash', '-c', 'echo'], ['sh'], ['dd', 'if=/dev/zero'], ['apt-get', 'upgrade'],
                     ['reboot'], ['parted'], ['pvcreate', '/dev/sdb'], ['vgextend', 'vg', '/dev/sdb'], ['lvextend'],
                     ['/usr/sbin/lvcreate', '-L', '1G'], ['wipefs', '-a', '/dev/x'], ['wipefs', '/dev/x'], []):
            with self.subTest(args=args):
                self.refused(args)

    def test_subcommand_allowlist(self):
        for args in (V + ['pool-delete', '--pool', 'x'], V + ['undefine', '--domain', 'vm'], V + ['destroy', 'vm'],
                     V + ['vol-delete', 'x'], ['systemctl', 'reboot'], ['systemctl', 'stop', 'libvirtd'],
                     ['dmsetup', 'remove', 'x'], ['vgscan'], ['udevadm', 'control'], ['udevadm', 'trigger'], V,
                     ['virsh']):
            with self.subTest(args=args):
                self.refused(args)

    def test_virsh_requires_system_uri_and_strict_options(self):
        for args in (['virsh', 'pool-list', '--all'], ['virsh', '-c', 'qemu:///session', 'pool-list'],
                     ['virsh', '--connect', 'qemu:///system', 'pool-list'], ['virsh', '-c=qemu:///system', 'uri'],
                     V + ['pool-info', 'x'],                                  # nazwa pozycyjna
                     V + ['pool-info', '--pool=x'],                           # forma --opcja=wartość
                     V + ['pool-refresh', '--pool=local'], V + ['pool-define-as', '--name=local', '--type', 'dir',
                                                                 '--target', '/vm'],
                     V + ['pool-autostart', '--disable', '--pool', 'x'], V + ['pool-autostart', '--disable', 'local'],
                     V + ['pool-start', '--pool', '--name'], V + ['pool-start', '--pool', '-x'],
                     V + ['pool-start'],                                      # brak nazwy
                     V + ['pool-start', '--pool', 'a', '--pool', 'b'],
                     V + ['domblklist', 'vm'], V + ['list', '--inactive'], V + ['uri', 'extra']):
            with self.subTest(args=args):
                self.refused(args)
        with mock.patch.object(runner.subprocess, 'run', return_value=cp([], 0)) as sp:
            runner.run(V + ['pool-info', '--pool', 'local'])
            runner.run(V + ['domblklist', '--details', '--domain', 'vm1'])
        self.assertEqual(sp.call_count, 2)

    def test_protected_pools_read_only(self):
        for name in safety.PROTECTED_POOLS:
            for sub in ('pool-start', 'pool-autostart', 'pool-refresh', 'pool-destroy', 'pool-undefine'):
                safety.arm(V + [sub, '--pool', name])
                self.refused(V + [sub, '--pool', name])
            self.refused(V + ['pool-define-as', '--name', name, '--type', 'dir', '--target', '/vm'])

    def test_positional_arguments_cannot_be_options(self):
        for args in (['mount', '-a'], ['mount', '--bind', '/a', '/b'], ['umount', '-l', '/vm'], ['umount', '-a'],
                     ['mkfs.ext4', '-F', '-m', '1', '-L', 'x', '-q'], ['lvcreate', '-L', '1G', '-n', 'x', '--yes'],
                     ['lvcreate', '-L', '1G', '-n', '-x', 'vg'], ['lvremove', '-y']):
            with self.subTest(args=args):
                safety.arm(args)
                self.refused(args)

    def test_destructive_commands_require_arming_once(self):
        for args in (['lvremove', '-y', 'vg/lv'], ['mkfs.ext4', '-F', '-m', '1', '-L', 'a', '/dev/x'],
                     V + ['pool-destroy', '--pool', 'p'], V + ['pool-undefine', '--pool', 'p']):
            with self.subTest(args=args):
                self.refused(args)
                safety.arm(args)
                with popen(rc=0):
                    runner.run(args)
                self.refused(args)  # uzbrojenie jednorazowe
        safety.arm(['lvremove', '-y', 'vg/a'])
        self.refused(['lvremove', '-y', 'vg/b'])  # tylko dokładnie to polecenie

    def test_refusal_is_logged_and_output_reports(self):
        with quiet() as out:
            self.assertEqual(runner.output(['rm', '-rf', '/']), 1)
        self.assertIn('nie jest na liście', out.getvalue())


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hvmtest-').replace('\\', '/')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(runner, 'ROOT', self.tmp), mock.patch.object(runner, 'DRY_RUN', False)):
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(self.tmp + '/etc')
        self.real = self.tmp + '/etc/fstab'

    def write(self, data):
        with open(self.real, 'wb') as f:
            f.write(data)

    def read(self):
        with open(self.real, 'rb') as f:
            return f.read()

    def test_replaces_content_without_temp_leftovers(self):
        self.write(b'old\n')
        mnt.atomic_write('/etc/fstab', 'line1\nline2\n')
        self.assertEqual(self.read(), b'line1\nline2\n')
        self.assertEqual(os.listdir(self.tmp + '/etc'), ['fstab'])

    def test_creates_missing_file(self):
        mnt.atomic_write('/etc/fstab', 'x\n')
        self.assertEqual(self.read(), b'x\n')

    def test_non_utf8_bytes_preserved(self):
        raw = b'# komentarz \xff\xfe latin1 \xe9\n/dev/sda1 / ext4 defaults 0 1\n'
        self.write(raw)
        mnt.atomic_write('/etc/fstab', mnt.read_fstab() + 'nowa\n')
        self.assertEqual(self.read(), raw + b'nowa\n')

    def test_dry_run_does_not_write(self):
        self.write(b'old\n')
        with mock.patch.object(runner, 'DRY_RUN', True), quiet():
            mnt.atomic_write('/etc/fstab', 'new\n')
        self.assertEqual(self.read(), b'old\n')

    def test_failure_leaves_original_and_no_temp(self):
        self.write(b'old\n')
        with mock.patch.object(mnt.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                mnt.atomic_write('/etc/fstab', 'new\n')
        self.assertEqual(self.read(), b'old\n')
        self.assertEqual(os.listdir(self.tmp + '/etc'), ['fstab'])

    @unittest.skipUnless(os.name == 'posix', 'bity uprawnień POSIX')
    def test_mode_preserved(self):
        self.write(b'old\n')
        os.chmod(self.real, 0o640)
        mnt.atomic_write('/etc/fstab', 'new\n')
        self.assertEqual(os.stat(self.real).st_mode & 0o777, 0o640)

    def test_restore_keeps_crlf_bytes(self):
        raw = b'a / ext4 defaults 0 1\r\n# x\r\n'
        self.write(raw)
        with open(self.tmp + '/etc/backup', 'wb') as f:
            f.write(raw)
        with mock.patch.object(runner, '_exec', return_value=cp([], 0)):
            mnt.atomic_write('/etc/fstab', mnt.read_fstab() + 'UUID=x /vm ext4 defaults 0 2\n')
            self.assertTrue(self.read().startswith(raw))
            mnt.restore_fstab('/etc/backup', '')
        self.assertEqual(self.read(), raw)

    def test_check_mount_target(self):
        os.makedirs(self.tmp + '/vm-empty')
        os.makedirs(self.tmp + '/vm-full')
        open(self.tmp + '/vm-full/disk.qcow2', 'w').close()
        open(self.tmp + '/vm-file', 'w').close()
        with mock.patch.object(mnt, 'is_mounted', return_value=False):
            mnt.check_mount_target('/vm-new/deeper')
            mnt.check_mount_target('/vm-empty')
            self.assertEqual(mnt.missing_dirs('/vm-new/deeper'), ['/vm-new', '/vm-new/deeper'])
            with self.assertRaisesRegex(ValueError, 'nie jest pusty'):
                mnt.check_mount_target('/vm-full')
            with self.assertRaisesRegex(ValueError, 'nie jest katalogiem'):
                mnt.check_mount_target('/vm-file')
        with mock.patch.object(mnt, 'is_mounted', return_value=True):
            with self.assertRaisesRegex(ValueError, 'punktem montowania'):
                mnt.check_mount_target('/vm-empty')

    def test_symlink_in_path_rejected(self):
        os.makedirs(self.tmp + '/real')
        try:
            os.symlink(self.tmp + '/real', self.tmp + '/link', target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'os.symlink niedostępne: {exc}')
        for path in ('/link', '/link/new'):
            with self.assertRaisesRegex(ValueError, 'dowiązaniem'):
                mnt.check_mount_target(path)


class OperationTests(unittest.TestCase):
    def test_rollback_reverse_order_only_failed_phase(self):
        done, op = [], safety.Operation()

        def step(i, phase):
            def action():
                op.undo(f'u{i}', f'manual{i}', lambda: done.append(i))
            op.add(f's{i}', action, phase=phase)
        step(1, 1)
        step(2, 1)
        step(3, 2)
        op.add('fail', lambda: (_ for _ in ()).throw(RuntimeError('boom')), phase=2)
        op.add('after', lambda: done.append('never'), phase=2)
        with quiet() as out:
            self.assertFalse(op.run())
            op.report()
        self.assertEqual(done, [3])  # faza 1 nietknięta
        self.assertEqual([s.status for s in op.steps], ['done', 'done', 'undone', 'failed', 'skipped'])
        text = out.getvalue()
        for line in ('✓ s1 — wykonano', '↺ s3 — wycofano', '✗ fail — błąd: boom', '○ after — pominięto'):
            self.assertIn(line, text)

    def test_failed_undo_reported_for_manual_action(self):
        op = safety.Operation()

        def act():
            op.undo('pierwszy', 'cmd1', lambda: None)
            op.undo('drugi', 'umount /vm', lambda: (_ for _ in ()).throw(OSError('busy')))
        op.add('krok', act)
        op.add('błąd', lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        with quiet() as out:
            self.assertFalse(op.run())
            op.report()
        self.assertTrue(op.interrupted)
        self.assertEqual(op.manual, [('drugi', 'umount /vm')])
        self.assertEqual(op.steps[0].status, 'done')
        self.assertIn('umount /vm', out.getvalue())
        self.assertIn('przerwano (Ctrl+C)', out.getvalue())

    def test_skip_is_not_fatal(self):
        op = safety.Operation()
        op.add('a', lambda: (_ for _ in ()).throw(safety.Skip('niepotrzebne')))
        op.add('b', lambda: None)
        with quiet():
            self.assertTrue(op.run())
        self.assertEqual([s.status for s in op.steps], ['skipped', 'done'])
        self.assertTrue(op.changed)

    def test_details_and_dry_prefix(self):
        op = safety.Operation()
        op.add('x', lambda: None, ['lvcreate -L 1G -n a vg'])
        with quiet() as out:
            op.print_details(dry=True)
        self.assertIn('[DRY-RUN] lvcreate -L 1G -n a vg', out.getvalue())


class UiTests(unittest.TestCase):
    def test_ascii_fallback(self):
        class Latin1(io.StringIO):
            encoding = 'latin-1'
        with contextlib.redirect_stdout(Latin1()) as out:
            ui.box(['HVM STORAGE MANAGER 1.0'])
            print(ui.state_line('ok', 'a'), ui.state_line('error', 'b'), ui.state_line('unknown', 'c'))
        self.assertNotIn('✓', out.getvalue())
        self.assertIn('+ a x b ? c', out.getvalue())
        self.assertIn('+---', out.getvalue())

    def test_unicode_symbols(self):
        with quiet() as out:
            print(ui.state_line('ok', 'a'), ui.sym('skip'), ui.sym('undo'))
        self.assertEqual(out.getvalue().strip(), '✓ a ○ ↺')

    def test_ask_helpers(self):
        with mock.patch('builtins.input', side_effect=EOFError), quiet():
            self.assertFalse(ui.ask_yes_no('?'))
            self.assertFalse(ui.ask_phrase('?', 'X'))
            self.assertEqual(ui.choose('?', ('0', '1')), '0')
        with mock.patch('builtins.input', side_effect=['zle', '1']), quiet():
            self.assertEqual(ui.choose('?', ('0', '1')), '1')
        with mock.patch('builtins.input', side_effect=['q']), self.assertRaises(ui.Cancelled):
            ui.ask('x')
        with mock.patch('builtins.input', side_effect=['', 'abc', '7']), quiet() as out:
            self.assertEqual(ui.ask('x', None, int), 7)
        self.assertIn('BŁĄD', out.getvalue())


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(runner.setup_logging, None)

    def test_log_file_records_commands(self):
        path = os.path.join(self.tmp, 'x.log')
        runner.setup_logging(path)
        self.assertEqual(runner.log_path, path)
        with mock.patch.object(runner.subprocess, 'run', return_value=cp(['lvs'], 0, '', 'warn')):
            runner.run(['lvs'])
        with self.assertRaises(safety.CommandRefused):
            runner.run(['rm', '-rf', '/'])
        runner.setup_logging(None)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('lvs -> rc=0 stderr: warn', text)
        self.assertIn('ODMOWA: rm -rf /', text)

    def test_none_writes_nothing(self):
        runner.setup_logging(None)
        self.assertIsNone(runner.log_path)
        with mock.patch.object(runner.subprocess, 'run', return_value=cp(['lvs'], 0)):
            runner.run(['lvs'])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_unwritable_log_does_not_crash(self):
        runner.setup_logging(os.path.join(self.tmp, 'nope-dir-hvm', 'x', 'y.log'))
        self.assertIsNone(runner.log_path)

    @unittest.skipUnless(os.name == 'posix', 'O_NOFOLLOW / bity uprawnień POSIX')
    def test_log_symlink_refused_and_mode_0640(self):
        target = os.path.join(self.tmp, 'target')
        link = os.path.join(self.tmp, 'link.log')
        os.symlink(target, link)
        runner.setup_logging(link)
        self.assertIsNone(runner.log_path)
        self.assertFalse(os.path.exists(target))
        path = os.path.join(self.tmp, 'ok.log')
        old = os.umask(0)
        try:
            runner.setup_logging(path)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, runner, '_LOCK_FD', None)
        runner._LOCK_FD = None

    def test_dry_run_creates_no_lock_file(self):
        path = os.path.join(self.tmp, 'lock')
        with mock.patch.object(runner, 'DRY_RUN', True):
            self.assertTrue(runner.acquire_lock(path))
        self.assertFalse(os.path.exists(path))

    @unittest.skipIf(sys.platform == 'win32', 'fcntl niedostępny na Windows')
    def test_second_lock_fails(self):
        path = os.path.join(self.tmp, 'lock')
        with mock.patch.object(runner, 'DRY_RUN', False):
            self.assertTrue(runner.acquire_lock(path))
            fd = runner._LOCK_FD
            runner._LOCK_FD = None
            self.assertFalse(runner.acquire_lock(path))
        os.close(fd)


class InterruptShieldTests(unittest.TestCase):
    def test_rollback_and_report_ignore_sigint_then_restore(self):
        import signal
        before = signal.getsignal(signal.SIGINT)
        seen, op = [], safety.Operation()

        def act():
            op.undo('u', 'm', lambda: seen.append(signal.getsignal(signal.SIGINT)))
        op.add('krok', act)
        op.add('błąd', lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        with quiet():
            self.assertFalse(op.run())
            with mock.patch.object(op, '_report', side_effect=lambda: seen.append(signal.getsignal(signal.SIGINT))):
                op.report()
        self.assertEqual(seen, [signal.SIG_IGN, signal.SIG_IGN])
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_shield_is_noop_outside_main_thread(self):
        import threading
        errors = []

        def worker():
            try:
                with safety.no_interrupt():
                    pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
