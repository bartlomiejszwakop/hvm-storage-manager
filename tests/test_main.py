"""CLI: argumenty, kody wyjścia, root/blokada, przekazywanie flag do poleceń, shim zgodności."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fakehost  # noqa: F401 — ustawia sys.path
from hvm import cli
from hvm import __version__, runner

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainTests(unittest.TestCase):
    CREATE = ['create', '--vg', 'vg0', '--name', 't1', '--size', '2', '--mount', '/vm-t1', '--yes']

    def setUp(self):
        fd, self.logfile = tempfile.mkstemp(prefix='hvmlog-')
        os.close(fd)
        self.addCleanup(os.unlink, self.logfile)
        self.addCleanup(runner.setup_logging, os.devnull)
        for p in (mock.patch.object(runner, 'DRY_RUN', False), mock.patch.object(runner, 'SHOW_COMMANDS', False)):
            p.start()
            self.addCleanup(p.stop)

    def main(self, argv, root=True, lock=True, **patches):
        """main() z podmienionymi funkcjami (nazwa → wartość zwracana) z modułów legacy/wizard."""
        mocks = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runner, 'is_root', return_value=root))
            stack.enter_context(mock.patch.object(runner, 'acquire_lock', return_value=lock))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for name, rv in patches.items():
                module, fn = name.split('.')
                target = getattr(cli, module)
                mocks[fn] = stack.enter_context(mock.patch.object(target, fn, return_value=rv))
            rc = cli.main(['--log-file', self.logfile] + argv)
        return rc, mocks

    def test_version(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as ctx:
            cli.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), 'hvm-storage-manager 1.0.0')
        self.assertEqual(__version__, '1.0.0')

    def test_argparse_errors_exit_2(self):
        for argv in (['create'], ['create', '--vg', 'v', '--name', 'n', '--size', 'abc', '--mount', '/vm'],
                     ['--bogus'], ['delete'], ['resume'], ['resume', '--vg', 'v', '--name', 'n'], ['pools', '--bogus'],
                     ['create', '--vg', 'v', '--name', 'n', '--size', '1', '--mount', '/vm', '--format-empty'],
                     ['--json'], ['--diagnose', 'audit'], ['prepare', '--format-empty'], ['repair', '--size', '1'],
                     ['repair', '--yes', '--format-empty'], ['resume', '--vg', 'v', '--name', 'n', '--mount', '/vm',
                                                             '--format-empty']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_non_root_refused_without_dry_run(self):
        rc, mk = self.main(self.CREATE, root=False, **{'legacy.create_volume': True})
        self.assertEqual(rc, 1)
        mk['create_volume'].assert_not_called()

    def test_lock_busy(self):
        rc, mk = self.main(self.CREATE, lock=False, **{'legacy.create_volume': True})
        self.assertEqual(rc, 1)
        mk['create_volume'].assert_not_called()

    def test_dry_run_flag_positions_and_non_root(self):
        for argv in (['--dry-run'] + self.CREATE, self.CREATE + ['--dry-run']):
            seen = {}

            def fake_create(*args, **kwargs):
                seen.update(dry=runner.DRY_RUN, args=args, kwargs=kwargs)
                return True
            with self.subTest(argv=argv), mock.patch.object(cli.legacy, 'create_volume', side_effect=fake_create):
                rc, _ = self.main(argv, root=False)
            self.assertEqual(rc, 0)
            self.assertTrue(seen['dry'])
            self.assertEqual(seen['args'], ('vg0', 't1', 2, '/vm-t1', True))
            self.assertEqual(seen['kwargs'], {'fix_dev': False})

    def test_legacy_dispatch(self):
        rc, mk = self.main(self.CREATE + ['--fix-dev-conflict'], **{'legacy.create_volume': True})
        self.assertEqual(rc, 0)
        mk['create_volume'].assert_called_once_with('vg0', 't1', 2, '/vm-t1', True, fix_dev=True)
        rc, mk = self.main(['resume', '--vg', 'vg0', '--name', 'r1', '--mount', '/vm-r1', '--yes', '--format-empty',
                            'vg0/r1'], **{'legacy.resume_volume': True})
        mk['resume_volume'].assert_called_once_with('vg0', 'r1', '/vm-r1', True, format_empty='vg0/r1')
        rc, mk = self.main(['resume', '--vg', 'vg0', '--name', 'r1', '--mount', '/vm-r1'],
                           **{'legacy.resume_volume': False})
        self.assertEqual(rc, 1)
        mk['resume_volume'].assert_called_once_with('vg0', 'r1', '/vm-r1', False, format_empty=None)
        rc, mk = self.main(['pools'], **{'legacy.pools_command': True})
        mk['pools_command'].assert_called_once_with('/vm-storage', False, False)
        rc, mk = self.main(['pools', '--mount', '/srv/x', '--refresh', '--yes'], **{'legacy.pools_command': False})
        self.assertEqual(rc, 1)
        mk['pools_command'].assert_called_once_with('/srv/x', True, True)
        rc, mk = self.main(['audit', '--mount', '/vm-storage'], **{'legacy.audit_command': True})
        mk['audit_command'].assert_called_once_with('/vm-storage')

    def test_prepare_and_repair_dispatch(self):
        rc, mk = self.main(['prepare', '--size', 'max', '--vg', 'vg0', '--name', 'lv', '--mount', '/srv/x',
                            '--pool-name', 'P1', '--yes', '--fix-dev-conflict'], **{'wizard.prepare_cli': True})
        self.assertEqual(rc, 0)
        mk['prepare_cli'].assert_called_once_with('max', 'vg0', 'lv', '/srv/x', 'P1', True, True)
        rc, mk = self.main(['prepare'], **{'wizard.prepare_cli': False})
        self.assertEqual(rc, 1)
        mk['prepare_cli'].assert_called_once_with('recommended', None, None, None, None, False, False)
        target = {'vg': 'vg0', 'lv': 'lv', 'mount': '/vm-storage', 'pool': 'P'}
        rc, mk = self.main(['repair', '--yes', '--format-empty', 'vg0/lv'],
                           **{'wizard.repair': True, 'diag.locate_target': target})
        self.assertEqual(rc, 0)
        mk['locate_target'].assert_called_once_with(None, None, None, None)
        mk['repair'].assert_called_once_with(target, True, 'vg0/lv', False)

    def test_errors_in_dispatch_are_clean(self):
        for exc in (ValueError('zła ścieżka'), RuntimeError('x'), OSError('y'),
                    subprocess.CalledProcessError(5, ['vgs'], '', 'vgs failed')):
            with self.subTest(exc=exc), contextlib.redirect_stderr(io.StringIO()):
                with mock.patch.object(cli.wizard, 'prepare_cli', side_effect=exc):
                    rc, _ = self.main(['prepare'])
                self.assertEqual(rc, 1)

    def test_menu_survives_errors(self):
        with mock.patch.object(cli, 'header'), mock.patch.object(cli.wizard, 'repair', side_effect=RuntimeError('boom')), \
                mock.patch('builtins.input', side_effect=['2', '', '0']), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.menu(), 0)
        self.assertIn('BŁĄD: boom', out.getvalue())

    def test_diagnose_runs_without_root_and_lock(self):
        rc, mk = self.main(['--diagnose'], root=False, lock=False, **{'diag.collect_report': {}, 'diag.print_report': None})
        self.assertEqual(rc, 0)
        mk['print_report'].assert_called_once()

    def test_keyboard_interrupt_exit_130(self):
        with mock.patch.object(cli, 'menu', side_effect=KeyboardInterrupt):
            rc, _ = self.main([])
        self.assertEqual(rc, 130)

    def test_menu_text_and_eof(self):
        with mock.patch.object(cli, 'header'), mock.patch('builtins.input', side_effect=EOFError), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.menu(), 0)
        self.assertIn(cli.MENU, out.getvalue())

    def test_shim_runs_main(self):
        p = subprocess.run([sys.executable, os.path.join(REPO, 'hvm_storage_manager.py'), '--version'],
                           capture_output=True, text=True, cwd=tempfile.gettempdir())
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), 'hvm-storage-manager 1.0.0')


if __name__ == '__main__':
    unittest.main()
