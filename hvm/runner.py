"""Warstwa poleceń: allowlista (safety), stałe środowisko PATH/LC_ALL=C, log, dry-run, blokada."""
import logging
import os
import posixpath
import shlex
import subprocess

from hvm import safety, ui

ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
LOG_FILE = '/var/log/hvm-storage-manager.log'
LOCK_FILE = '/run/hvm-storage-manager.lock'
DRY_RUN = False        # polecenia zmieniające system są tylko wypisywane; nic nie jest zapisywane
SHOW_COMMANDS = False  # wypisuj każde polecenie przed wykonaniem (menu Zaawansowane)
ROOT = ''              # prefiks lokalnego systemu plików — używany wyłącznie w testach (FakeHost)
log_path = None

log = logging.getLogger('hvm-storage-manager')
log.addHandler(logging.NullHandler())  # bez setup_logging() (np. w testach) nic nie zapisujemy
_LOCK_FD = None


def host_path(path):
    """Ścieżka lokalnego pliku (z prefiksem ROOT w testach)."""
    path = str(path)
    return ROOT + path if ROOT and path.startswith('/') else path


def canon(path):
    """Kanoniczna ścieżka katalogu do porównań (realpath, bez końcowego /)."""
    return os.path.realpath(host_path(posixpath.normpath(str(path))))


def _close_handlers():
    for h in list(log.handlers):
        log.removeHandler(h)
        stream = getattr(h, 'hvm_stream', None)
        h.close()
        if stream is not None:
            stream.close()


def setup_logging(path=LOG_FILE):
    """Log do pliku (O_APPEND|O_NOFOLLOW, 0640). path=None → nic nie jest zapisywane (dry-run, --diagnose bez root)."""
    global log_path
    log.setLevel(logging.INFO)
    log.propagate = False
    _close_handlers()
    log_path = None
    handler = logging.NullHandler()
    if path:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o640)
            stream = os.fdopen(fd, 'a', encoding='utf-8')
            handler = logging.StreamHandler(stream)
            handler.hvm_stream = stream
            log_path = path
        except OSError:  # brak uprawnień / brak katalogu / dowiązanie — logujemy „w nicość”
            handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(process)d] %(message)s'))
    log.addHandler(handler)


def _exec(args, mutating=False):
    """Jedyne miejsce, które naprawdę uruchamia proces (w testach podmieniane przez FakeHost).

    Polecenia zmieniające system działają w osobnej sesji (Ctrl+C z terminala do nich nie trafia)
    i są zawsze doczekiwane do końca — przerwanie jest zapamiętywane i zgłaszane PO zakończeniu
    procesu (atrybut `interrupted`), żeby np. mkfs/lvcreate nie zostały zabite w połowie."""
    if not mutating:
        try:
            return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  env=ENV, stdin=subprocess.DEVNULL)
        except OSError as exc:  # np. brak programu w PATH
            return subprocess.CompletedProcess(args, 127, '', str(exc))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding='utf-8', errors='replace', env=ENV, start_new_session=True)
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, '', str(exc))
    interrupted = False
    while True:
        try:
            out, err = proc.communicate()
            break
        except KeyboardInterrupt:
            interrupted = True
            ui.warn(f'przerwanie odebrane — czekam na zakończenie `{shlex.join(args)}` (nie przerywam go w połowie).')
    result = subprocess.CompletedProcess(args, proc.returncode, out or '', err or '')
    result.interrupted = interrupted
    return result


def run(args, check=True):
    """Uruchamia polecenie z allowlisty (bez powłoki). W trybie dry-run polecenia zmieniające
    system są tylko wypisywane. check=True: kod != 0 → CalledProcessError."""
    args = [str(a) for a in args]
    line = shlex.join(args)
    try:
        safety.check_command(args)
    except safety.CommandRefused as exc:
        log.error('ODMOWA: %s (%s)', line, exc)
        raise
    mutating = safety.is_mutating(args)
    if DRY_RUN and mutating:
        print(f'[DRY-RUN] {line}')
        log.info('[DRY-RUN] %s', line)
        return subprocess.CompletedProcess(args, 0, '', '')
    if SHOW_COMMANDS:
        print(ui.paint('2', f'  $ {line}'))
    p = _exec(args, mutating)
    stderr = (p.stderr or '').strip()
    log.info('%s -> rc=%s%s', line, p.returncode, f' stderr: {stderr}' if stderr else '')
    if getattr(p, 'interrupted', False):
        log.info('Przerwanie (Ctrl+C) zgłoszone po zakończeniu: %s', line)
        raise KeyboardInterrupt
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, args, p.stdout, p.stderr)
    return p


def output(args):
    """Wypisuje wynik polecenia tylko do odczytu; zwraca kod wyjścia."""
    try:
        p = run(args, check=False)
    except safety.CommandRefused as exc:
        ui.err(str(exc))
        return 1
    print((p.stdout or '').rstrip() or '(brak danych)')
    if p.returncode:
        ui.err((p.stderr or '').strip() or f'kod wyjścia {p.returncode}')
    return p.returncode


def is_root():
    return getattr(os, 'geteuid', lambda: -1)() == 0


def acquire_lock(path=None):
    """True = blokada założona (lub pominięta w dry-run), False = działa inna instancja.
    W trybie dry-run plik blokady nie jest tworzony."""
    global _LOCK_FD
    if DRY_RUN or _LOCK_FD is not None:
        return True
    try:
        import fcntl
    except ImportError:  # system bez fcntl (Windows — tylko testy)
        return True
    fd = os.open(path or LOCK_FILE, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _LOCK_FD = fd
    return True
