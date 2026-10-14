"""Zasady bezpieczeństwa: allowlista poleceń, chronione pule, plan operacji i raport kroków.

Polityka wersji 1.0:
- run() uruchamia wyłącznie programy (i podpolecenia) z ALLOWED_COMMANDS;
- virsh wyłącznie jako `virsh -c qemu:///system <podpolecenie> [opcje]`; dla każdego podpolecenia
  dozwolony jest tylko znany zestaw opcji, nazwy pul/maszyn wyłącznie jako wartości --pool/--name/
  --domain (bez formy --opcja=wartość i bez argumentów pozycyjnych);
- wipefs tylko w trybie odczytu (-n); `virsh pool-delete` nigdy; pule z PROTECTED_POOLS
  wyłącznie do odczytu;
- lvremove, mkfs.ext4, `virsh pool-destroy` i `virsh pool-undefine` wykonają się tylko po
  jednorazowym „uzbrojeniu” (arm) dokładnie tego polecenia. Uzbraja je wyłącznie: krok
  formatowania tuż po świeżym potwierdzeniu braku sygnatur, wycofanie puli zdefiniowanej w tym
  uruchomieniu oraz jawna zgoda operatora (fraza USUN <lv>) na usunięcie LV utworzonego w tym
  uruchomieniu — nigdy w trybie --yes;
- automatyczne wycofanie obejmuje tylko niedestrukcyjne kroki z bieżącego uruchomienia.
"""
import logging
import shlex
import signal
import threading

from hvm import ui

PROTECTED_POOLS = ('local', 'morpheus-images', 'morpheus-cloud-init')
VIRSH_URI = 'qemu:///system'

ALLOWED_COMMANDS = {
    'lsblk': None, 'pvs': None, 'vgs': None, 'lvs': None, 'blkid': None, 'findmnt': None, 'df': None,
    'ip': None, 'hostname': ('-I',), 'wipefs': None, 'lvcreate': None, 'lvremove': None, 'mkfs.ext4': None,
    'mount': None, 'umount': None, 'vgscan': ('--mknodes',), 'dmsetup': ('ls', 'info'),
    'udevadm': ('settle', 'info'), 'systemctl': ('daemon-reload',), 'virsh': None,
}
# virsh: podpolecenie → (flagi bez wartości, opcje z wartością)
VIRSH_SPEC = {
    'uri': ((), ()), 'version': ((), ()),
    'list': (('--all', '--name', '--state-running'), ()),
    'domblklist': (('--details',), ('--domain',)),
    'pool-list': (('--all', '--name'), ()),
    'pool-info': ((), ('--pool',)), 'pool-dumpxml': ((), ('--pool',)),
    'pool-define-as': ((), ('--name', '--type', '--target')),
    'pool-start': ((), ('--pool',)), 'pool-autostart': ((), ('--pool',)), 'pool-refresh': ((), ('--pool',)),
    'pool-destroy': ((), ('--pool',)), 'pool-undefine': ((), ('--pool',)),
}
VIRSH_MUTATING = ('pool-define-as', 'pool-start', 'pool-autostart', 'pool-refresh', 'pool-destroy', 'pool-undefine')
_MUTATING_BIN = ('lvcreate', 'lvremove', 'mkfs.ext4', 'mount', 'umount', 'vgscan', 'systemctl')
_ARMED_ONLY = ('lvremove', 'mkfs.ext4', 'virsh pool-destroy', 'virsh pool-undefine')
ARMED = set()

log = logging.getLogger('hvm-storage-manager')


class CommandRefused(RuntimeError):
    """Polecenie odrzucone przez zasady bezpieczeństwa (nigdy nie zostało uruchomione)."""


def virsh_sub(args):
    """Podpolecenie virsh (po obowiązkowym `-c qemu:///system`) albo ''."""
    return args[3] if len(args) > 3 and args[1:3] == ['-c', VIRSH_URI] else ''


def _key(args):
    if args[0] == 'virsh':
        return f'virsh {virsh_sub(args)}'
    if args[0] in ('udevadm', 'systemctl', 'dmsetup') and len(args) > 1:
        return f'{args[0]} {args[1]}'
    return args[0]


def is_mutating(args):
    # virsh ostrożnie: zmieniające, jeśli którekolwiek słowo to podpolecenie zmieniające
    return args[0] in _MUTATING_BIN or (args[0] == 'virsh' and any(a in VIRSH_MUTATING for a in args[1:]))


def parse_virsh(args):
    """(podpolecenie, {opcja: wartość}) albo CommandRefused — ścisła składnia virsh."""
    line = shlex.join(args)
    if len(args) < 4 or args[1:3] != ['-c', VIRSH_URI]:
        raise CommandRefused(f'{line}: virsh wolno uruchomić tylko jako `virsh -c {VIRSH_URI} ...` — odmowa')
    sub = args[3]
    if sub not in VIRSH_SPEC:
        raise CommandRefused(f'{line}: niedozwolone podpolecenie virsh — odmowa')
    flags, opts = VIRSH_SPEC[sub]
    values, rest, i = {}, args[4:], 0
    while i < len(rest):
        tok = rest[i]
        if tok in flags:
            i += 1
        elif (tok in opts and tok not in values and i + 1 < len(rest) and rest[i + 1]
              and not rest[i + 1].startswith('-')):
            values[tok] = rest[i + 1]
            i += 2
        else:
            raise CommandRefused(f'{line}: niedozwolony argument virsh {tok!r} — odmowa')
    if sub in VIRSH_MUTATING:
        name = values.get('--pool') or values.get('--name')
        if not name:
            raise CommandRefused(f'{line}: brak nazwy puli (--pool/--name) — odmowa')
        if name in PROTECTED_POOLS:
            raise CommandRefused(f'pula {name} jest chroniona — program jej nie zmienia')
    return sub, values


def _check_positionals(args):
    line, binary = shlex.join(args), args[0]
    if binary in ('mount', 'umount') and (len(args) != 2 or args[1].startswith('-')):
        raise CommandRefused(f'{line}: dozwolone tylko `{binary} <ścieżka>` — odmowa')
    if binary in ('mkfs.ext4', 'lvcreate', 'lvremove') and (len(args) < 2 or args[-1].startswith('-')):
        raise CommandRefused(f'{line}: urządzenie/grupa nie może zaczynać się od „-” — odmowa')
    if binary == 'lvcreate' and ('-n' not in args[:-1] or args[args.index('-n') + 1].startswith('-')):
        raise CommandRefused(f'{line}: niepoprawna nazwa nowego LV — odmowa')


def check_command(args):
    """Rzuca CommandRefused, jeśli polecenie łamie zasady; zużywa jednorazowe „uzbrojenie”."""
    if not args:
        raise CommandRefused('puste polecenie')
    binary = args[0]
    if binary not in ALLOWED_COMMANDS:
        raise CommandRefused(f'program {binary!r} nie jest na liście dozwolonych poleceń — odmowa')
    subs = ALLOWED_COMMANDS[binary]
    if subs is not None and (len(args) < 2 or args[1] not in subs):
        raise CommandRefused(f'{shlex.join(args)}: niedozwolone podpolecenie — odmowa')
    if binary == 'wipefs' and not {'-n', '--no-act'} & set(args):
        raise CommandRefused('wipefs wolno uruchomić wyłącznie w trybie odczytu (-n) — odmowa')
    if binary == 'virsh':
        parse_virsh(args)
    _check_positionals(args)
    if _key(args) in _ARMED_ONLY:
        armed = tuple(args)
        if armed not in ARMED:
            raise CommandRefused(f'{shlex.join(args)}: polecenie destrukcyjne wymaga osobnej, jawnej zgody — odmowa')
        ARMED.discard(armed)


def arm(args):
    """Jednorazowa zgoda na konkretne polecenie z _ARMED_ONLY."""
    ARMED.add(tuple(str(a) for a in args))


class no_interrupt:  # noqa: N801 — używane jako `with no_interrupt():`
    """Ignoruje SIGINT (kolejne Ctrl+C) na czas wycofania i raportu; poza wątkiem głównym nic nie robi."""

    def __enter__(self):
        self._old = None
        try:
            if threading.current_thread() is threading.main_thread():
                self._old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, AttributeError, OSError):
            self._old = None
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                signal.signal(signal.SIGINT, self._old)
            except (ValueError, OSError):
                pass
        return False


# --- plan operacji i raport kroków ---------------------------------------------------

DONE, FAILED, SKIPPED, UNDONE = 'done', 'failed', 'skipped', 'undone'
_STATUS = {DONE: ('ok', 'wykonano'), FAILED: ('fail', 'błąd'), SKIPPED: ('skip', 'pominięto'),
           UNDONE: ('undo', 'wycofano'), None: ('skip', 'pominięto')}


class Skip(Exception):
    """Krok niekrytyczny nie został wykonany (operacja trwa dalej)."""


class Step:
    def __init__(self, title, action, commands, phase):
        self.title, self.action, self.commands, self.phase = title, action, list(commands), phase
        self.status, self.note = None, ''


class Operation:
    """Lista kroków wykonywanych po kolei. Faza 1 = wolumen Linux, faza 2 = pula libvirt.
    Błąd w fazie wycofuje (automatycznie) wyłącznie zarejestrowane kroki tej fazy."""

    def __init__(self):
        self.steps, self.ctx, self.manual = [], {}, []
        self._undo, self._current = [], None
        self.failed_phase, self.interrupted = None, False

    def add(self, title, action, commands=(), phase=1):
        self.steps.append(Step(title, action, commands, phase))

    def undo(self, desc, manual, fn):
        """Rejestruje wycofanie kroku, który właśnie się wykonuje."""
        self._undo.append((self._current, desc, manual, fn))

    @property
    def changed(self):
        return any(s.status in (DONE, UNDONE) for s in self.steps)

    def print_details(self, dry=False):
        print('\nSZCZEGÓŁY TECHNICZNE — polecenia w kolejności wykonania:')
        for i, step in enumerate(self.steps, 1):
            print(f'{i:>2}. {step.title}')
            for cmd in step.commands:
                print(f'      {"[DRY-RUN] " if dry else ""}{cmd}')

    def run(self):
        for step in self.steps:
            if self.failed_phase is not None:
                step.status = SKIPPED
                continue
            self._current = step
            print(f'→ {step.title}')
            log.info('KROK: %s', step.title)
            try:
                step.action()
                step.status = DONE
            except Skip as exc:
                step.status, step.note = SKIPPED, str(exc)
                ui.warn(f'{step.title}: {exc}')
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — każdy błąd kończy fazę
                step.status, step.note = FAILED, ui.describe(exc)
                self.failed_phase, self.interrupted = step.phase, isinstance(exc, KeyboardInterrupt)
                ui.err(f'{step.title}: {step.note}')
                log.error('KROK NIEUDANY: %s: %s', step.title, step.note)
        self._current = None
        if self.failed_phase is not None:
            with no_interrupt():
                self._rollback(self.failed_phase)
        return self.failed_phase is None

    def _rollback(self, phase):
        entries = [u for u in self._undo if u[0] is not None and u[0].phase == phase]
        if not entries:
            return
        print('\nWycofywanie niedestrukcyjnych kroków wykonanych w tym uruchomieniu:')
        broken = set()
        for step, desc, manual, fn in reversed(entries):
            try:
                fn()
                ui.ok(f'  {ui.sym("undo")} {desc}')
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — wycofujemy dalej
                ui.err(f'  {desc}: {ui.describe(exc)}')
                log.error('Wycofanie nieudane: %s: %s', desc, ui.describe(exc))
                self.manual.append((desc, manual))
                broken.add(step)
        for step in {e[0] for e in entries} - broken:
            if step.status == DONE:
                step.status = UNDONE

    def report(self):
        with no_interrupt():
            self._report()

    def _report(self):
        print('\nRAPORT KROKÓW')
        for step in self.steps:
            key, word = _STATUS[step.status]
            print(f'{ui.sym(key)} {step.title} — {word}' + (f': {step.note}' if step.note else ''))
        if self.manual:
            print('Nie wszystko udało się wycofać. Wykonaj ręcznie (w tej kolejności), po sprawdzeniu stanu systemu:')
            for desc, manual in self.manual:
                print(f'  {manual:<60} # {desc}')
            print('Nie restartuj hosta, dopóki `findmnt --verify` nie przejdzie bez błędów.')
