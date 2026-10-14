#!/usr/bin/env python3
"""Bezpieczny kreator NOWYCH wolumenów LVM/ext4 dla laboratorium HPE HVM.

Tylko biblioteka standardowa Pythona 3.8+, jeden plik. Każde polecenie
zewnętrzne przechodzi przez run(), które je loguje i respektuje --dry-run.
"""
import argparse
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

__version__ = '0.2.0'

FSTAB = Path('/etc/fstab')
LOG_FILE = '/var/log/hvm-storage-manager.log'
LOCK_FILE = '/run/hvm-storage-manager.lock'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
GIB = 1024 ** 3
RESERVE_GIB = 1
CONFIRM_PHRASE = 'TWORZ NOWY WOLUMEN'
MORPHEUS_HINT = 'Dodaj w Morpheus: Infrastructure → Clusters → Storage → Datastores → ADD → Directory Pool.'

# Allowlista punktów montowania: wyrażenia regularne dopasowywane do CAŁEJ
# znormalizowanej ścieżki. Inne lokalizacje są odrzucane. Aby dopuścić nową
# lokalizację, dopisz tu wzorzec (tylko małe litery, cyfry, '_' i '-').
ALLOWED_MOUNT_PATTERNS = [
    r'^/vm[a-z0-9_-]*$',                       # /vm-storage, /vm_data, /vm2
    r'^/srv/[a-z0-9_-]+$',                     # /srv/vm-storage
    r'^/data/[a-z0-9_-]+$',                    # /data/vm-storage
    r'^/var/lib/libvirt/images/[a-z0-9_-]+$',  # /var/lib/libvirt/images/pula1
]
LVM_RESERVED_NAMES = ('.', '..')
LVM_RESERVED_PREFIXES = ('snapshot', 'pvmove')
LVM_RESERVED_PARTS = ('_cdata', '_cmeta', '_corig', '_cpool', '_cvol', '_imeta', '_iorig', '_mlog',
                      '_mimage', '_pmspare', '_rimage', '_rmeta', '_tdata', '_tmeta', '_vorigin',
                      '_vdata', '_wcorig')
NAME_HELP = ('litera na początku, dalej litery, cyfry oraz _ + . -; maks. 64 znaki; '
             'bez nazw zastrzeżonych przez LVM (snapshot, pvmove, _rimage, _tmeta...)')
UUID_RE = re.compile(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')

DRY_RUN = False
log = logging.getLogger('hvm-storage-manager')
log.addHandler(logging.NullHandler())  # bez setup_logging() (np. w testach) nic nie wypisujemy
_LOCK_FD = None


class Cancelled(Exception):
    """Użytkownik wpisał q (lub zamknął wejście) — powrót do menu."""


# --- wyjście na konsolę ------------------------------------------------------

def _paint(code, text):
    tty = getattr(sys.stdout, 'isatty', lambda: False)()
    return text if os.environ.get('NO_COLOR') or not tty else f'\033[{code}m{text}\033[0m'


def err(msg): print(_paint('31', f'BŁĄD: {msg}'))
def ok(msg): print(_paint('32', msg))
def warn(msg): print(_paint('33', f'UWAGA: {msg}'))
def fmt_gib(b): return f'{b / GIB:.1f} GiB'


def _describe(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or '').strip()
        return f'polecenie `{shlex.join(exc.cmd)}` zakończone kodem {exc.returncode}' + (f': {detail}' if detail else '')
    if isinstance(exc, KeyboardInterrupt):
        return 'przerwano (Ctrl+C)'
    return str(exc) or type(exc).__name__


# --- warstwa poleceń, logowanie, blokada --------------------------------------

def setup_logging(path=LOG_FILE):
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h); h.close()
    try:
        handler = logging.FileHandler(path, encoding='utf-8')
    except OSError:  # brak uprawnień / brak katalogu — logujemy tylko „w nicość”
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(process)d] %(message)s'))
    log.addHandler(handler)


def run(args, check=True, mutating=False):
    """Uruchamia polecenie z bezpiecznym środowiskiem. Zmieniające system
    polecenia (mutating=True) w trybie dry-run są tylko wypisywane."""
    args = [str(a) for a in args]
    line = shlex.join(args)
    if mutating and DRY_RUN:
        print(f'[DRY-RUN] {line}')
        log.info('[DRY-RUN] %s', line)
        return subprocess.CompletedProcess(args, 0, '', '')
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=ENV, stdin=subprocess.DEVNULL)
    except OSError as exc:  # np. brak programu w PATH
        p = subprocess.CompletedProcess(args, 127, '', str(exc))
    log.info('%s -> rc=%s%s', line, p.returncode, f' stderr: {p.stderr.strip()}' if p.stderr.strip() else '')
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, args, p.stdout, p.stderr)
    return p


def output(args):
    p = run(args, check=False)
    print(p.stdout.rstrip() or '(brak danych)')
    if p.returncode:
        err(p.stderr.strip() or f'kod wyjścia {p.returncode}')
    return p.returncode


def is_root():
    return getattr(os, 'geteuid', lambda: -1)() == 0


def acquire_lock(path=LOCK_FILE):
    """True = blokada założona (lub pominięta), False = działa inna instancja."""
    global _LOCK_FD
    try:
        import fcntl
    except ImportError:  # system bez fcntl (np. Windows — tylko testy)
        return True
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError:
        if DRY_RUN:
            return True
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _LOCK_FD = fd
    return True


# --- odczyt LVM (JSON) --------------------------------------------------------

def _report(stdout, key):
    data = json.loads(stdout or '{}')
    return [row for rep in data.get('report', []) for row in rep.get(key, [])]


def _bytes(value):
    return int(str(value).strip().lstrip('<>').split('.')[0] or 0)


def list_vgs():
    """Lista grup LVM: [{'vg_name': str, 'vg_size': int B, 'vg_free': int B}]."""
    p = run(['vgs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,vg_size,vg_free'])
    return [{'vg_name': r['vg_name'], 'vg_size': _bytes(r['vg_size']), 'vg_free': _bytes(r['vg_free'])}
            for r in _report(p.stdout, 'vg')]


def lv_exists(vg, name):
    p = run(['lvs', '--reportformat', 'json', '-o', 'lv_name,vg_name'])
    return any(r.get('vg_name') == vg and r.get('lv_name') == name for r in _report(p.stdout, 'lv'))


# --- walidacja (czyste funkcje) ------------------------------------------------

def verify_name(s):
    return (isinstance(s, str) and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_+.-]{0,63}', s) is not None
            and s not in LVM_RESERVED_NAMES and not s.startswith(LVM_RESERVED_PREFIXES)
            and not any(p in s for p in LVM_RESERVED_PARTS))


def verify_vg_name(s):
    # LVM dopuszcza nazwy VG zaczynające się od cyfry lub _, ale nie od '-'.
    return (isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9_+.][a-zA-Z0-9_+.-]{0,126}', s) is not None
            and s not in LVM_RESERVED_NAMES)


def normalize_mount(s):
    s = (s or '').strip() if isinstance(s, str) else ''
    if not s.startswith('/'):
        raise ValueError('Punkt montowania musi być ścieżką bezwzględną (zaczynać się od /).')
    if any(c.isspace() or ord(c) < 32 or c == '\\' for c in s):
        raise ValueError('Punkt montowania nie może zawierać spacji, znaków sterujących ani „\\”.')
    if '..' in s.split('/'):
        raise ValueError('Punkt montowania nie może zawierać „..”.')
    path = '/' + posixpath.normpath(s).lstrip('/')
    if path == '/':
        raise ValueError('Nie można montować w katalogu głównym /.')
    return path


def mount_allowed(path):
    return any(re.fullmatch(p, path) for p in ALLOWED_MOUNT_PATTERNS)


def _fstab_fields(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            fields = line.split()
            if len(fields) >= 2:
                yield fields


def _unescape(s):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), s)


def fstab_has_mount(text, mount):
    target = posixpath.normpath(mount)
    return any(posixpath.normpath(_unescape(f[1])) == target for f in _fstab_fields(text))


def fstab_has_uuid(text, uuid):
    u = uuid.lower()
    wanted = (f'uuid={u}', f'/dev/disk/by-uuid/{u}')
    return any(_unescape(f[0]).replace('"', '').lower() in wanted for f in _fstab_fields(text))


def fstab_line(uuid, mount):
    return f'UUID={uuid} {mount} ext4 defaults,nofail 0 2\n'


def size_ok(size_gib, free_bytes, reserve_gib=RESERVE_GIB):
    return size_gib >= 1 and (size_gib + reserve_gib) * GIB <= free_bytes


def parse_size(s):
    s = str(s).strip()
    if not re.fullmatch(r'[0-9]+', s) or not 1 <= int(s) <= 1000000:
        raise ValueError('Rozmiar musi być liczbą całkowitą GiB z zakresu 1–1000000.')
    return int(s)


# --- walidacja z I/O -----------------------------------------------------------

def check_mount_target(mount):
    """Rzuca ValueError, jeśli katalogu nie można bezpiecznie użyć."""
    current = '/'
    for part in mount.strip('/').split('/'):
        current = posixpath.join(current, part)
        if os.path.islink(current):
            raise ValueError(f'{current} jest dowiązaniem symbolicznym — odmowa.')
        if not os.path.lexists(current):
            break
    if os.path.lexists(mount):
        if not os.path.isdir(mount):
            raise ValueError(f'{mount} istnieje i nie jest katalogiem.')
        if os.path.ismount(mount):
            raise ValueError(f'{mount} jest już punktem montowania.')
        if os.listdir(mount):
            raise ValueError(f'Katalog {mount} nie jest pusty.')


def missing_dirs(mount):
    """Brakujące katalogi ścieżki, od najwyższego."""
    out, current = [], '/'
    for part in mount.strip('/').split('/'):
        current = posixpath.join(current, part)
        if not os.path.lexists(current):
            out.append(current)
    return out


def read_raw(path):
    # newline='' zachowuje oryginalne końce linii (np. CRLF) bajt w bajt.
    with open(path, encoding='utf-8', errors='surrogateescape', newline='') as f:
        return f.read()


def read_fstab():
    return read_raw(FSTAB)


def check_new_name(vg, name):
    if not verify_name(name):
        raise ValueError(f'Niedozwolona nazwa wolumenu {name!r} ({NAME_HELP}).')
    if lv_exists(vg, name):
        raise ValueError(f'Wolumen /dev/{vg}/{name} już istnieje — nie zostanie sformatowany.')
    return name


def check_new_mount(raw):
    mount = normalize_mount(raw)
    if not mount_allowed(mount):
        raise ValueError(f'{mount} nie jest na liście dozwolonych lokalizacji: ' + ', '.join(ALLOWED_MOUNT_PATTERNS))
    check_mount_target(mount)
    if fstab_has_mount(read_fstab(), mount):
        raise ValueError(f'{mount} jest już wpisany w {FSTAB}.')
    return mount


def validate_request(vg, name, size_gib, mount):
    """Pełna walidacja przed zmianami. Zwraca plan (dict) lub rzuca ValueError."""
    if not verify_vg_name(vg):
        raise ValueError(f'Niedozwolona nazwa grupy LVM {vg!r}.')
    if not verify_name(name):
        raise ValueError(f'Niedozwolona nazwa wolumenu {name!r} ({NAME_HELP}).')
    size = parse_size(size_gib)
    mount = check_new_mount(mount)
    vgs = {v['vg_name']: v for v in list_vgs()}
    if vg not in vgs:
        raise ValueError(f'Grupa LVM {vg} nie istnieje. Dostępne: {", ".join(vgs) or "brak"}.')
    check_new_name(vg, name)
    free = vgs[vg]['vg_free']
    if not size_ok(size, free):
        raise ValueError(f'Za mało miejsca w {vg}: wolne {fmt_gib(free)}, potrzeba {size} GiB + {RESERVE_GIB} GiB '
                         f'rezerwy (maksymalnie {max(free // GIB - RESERVE_GIB, 0)} GiB).')
    if run(['findmnt', '--verify', '--tab-file', str(FSTAB)], check=False).returncode:
        raise ValueError(f'findmnt --verify zgłasza błędy w obecnym {FSTAB} — napraw je przed dodaniem nowego wpisu.')
    return {'vg': vg, 'name': name, 'size': size, 'mount': mount, 'lvpath': f'/dev/{vg}/{name}', 'free': free}


# --- zapis plików ----------------------------------------------------------------

def atomic_write(path, text):
    """Atomowa podmiana pliku (tymczasowy plik w tym samym katalogu + os.replace),
    z zachowaniem uprawnień i właściciela oryginału."""
    path = Path(path)
    if DRY_RUN:
        print(f'[DRY-RUN] zapis {path} ({len(text)} znaków)')
        return
    st = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode) if st else 0o644)
        if st and hasattr(os, 'chown'):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, str(path))
        tmp = None
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError:
            pass
    finally:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass


def restore_fstab(backup, original):
    text = read_raw(backup) if backup.exists() else original
    atomic_write(FSTAB, text)
    run(['systemctl', 'daemon-reload'], mutating=True)


def remove_dir(d):
    if DRY_RUN:
        print(f'[DRY-RUN] rmdir {d}')
    else:
        os.rmdir(d)  # tylko pusty katalog


def verify_mounted(mount, lvpath, uuid):
    src = run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', mount], check=False).stdout.strip()
    fs_uuid = run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', mount], check=False).stdout.strip()
    if fs_uuid.lower() != uuid.lower() and (not src or os.path.realpath(src) != os.path.realpath(lvpath)):
        raise RuntimeError(f'{mount} nie wskazuje na nowy wolumen (źródło: {src or "brak"}).')


# --- transakcja ---------------------------------------------------------------------

def _step(msg):
    print(f'→ {msg}')
    log.info('KROK: %s', msg)


def _execute(plan, undo):
    """Wykonuje kroki; po każdym udanym kroku dokłada do `undo` (opis, polecenie ręczne, funkcja)."""
    vg, name, mount, lvpath, size = plan['vg'], plan['name'], plan['mount'], plan['lvpath'], plan['size']
    _step(f'Tworzenie wolumenu {lvpath} ({size} GiB)')
    run(['lvcreate', '-L', f'{size}G', '-n', name, vg], mutating=True)
    undo.append((f'usunięcie nowego LV {lvpath}', f'lvremove {lvpath}',
                 lambda: run(['lvremove', '-y', lvpath], mutating=True)))
    if not DRY_RUN:
        run(['udevadm', 'settle'], check=False)
        rc = run(['blkid', '-p', lvpath], check=False).returncode
        if rc == 0 or rc == 8:
            raise RuntimeError('Na nowym LV wykryto sygnaturę systemu plików — przerwano przed formatowaniem.')
        if rc != 2:
            raise RuntimeError(f'Nie udało się sprawdzić sygnatur na nowym LV (blkid kod {rc}).')
    _step('Formatowanie ext4 (wyłącznie nowy LV)')
    run(['mkfs.ext4', '-F', '-m', '1', '-L', name[:16], lvpath], mutating=True)
    _step(f'Przygotowanie katalogu {mount}')
    for d in missing_dirs(mount):
        if DRY_RUN:
            print(f'[DRY-RUN] mkdir {d}')
        else:
            os.mkdir(d, 0o755)
        undo.append((f'usunięcie utworzonego katalogu {d}', f'rmdir {d}', lambda d=d: remove_dir(d)))
    if DRY_RUN:
        uuid = '<UUID-nowego-LV>'
    else:
        uuid = run(['blkid', '-p', '-s', 'UUID', '-o', 'value', lvpath]).stdout.strip()
        if not UUID_RE.fullmatch(uuid):
            raise RuntimeError(f'Nie udało się odczytać UUID nowego systemu plików ({uuid!r}).')
    original = read_fstab()
    if fstab_has_mount(original, mount) or fstab_has_uuid(original, uuid):
        raise RuntimeError(f'{FSTAB} zmienił się w trakcie pracy — już zawiera {mount} lub UUID {uuid}.')
    backup = FSTAB.with_name(f'{FSTAB.name}.backup-hvm-{datetime.now():%Y%m%d-%H%M%S}')
    _step(f'Kopia zapasowa {FSTAB} → {backup}')
    if DRY_RUN:
        print(f'[DRY-RUN] cp -p {FSTAB} {backup}')
    else:
        shutil.copy2(str(FSTAB), str(backup))
    line = fstab_line(uuid, mount)
    _step(f'Dopisanie do {FSTAB}: {line.strip()}')
    atomic_write(FSTAB, original + ('' if not original or original.endswith('\n') else '\n') + line)
    undo.append((f'przywrócenie {FSTAB} z kopii {backup}', f'cp -p {backup} {FSTAB} && systemctl daemon-reload',
                 lambda: restore_fstab(backup, original)))
    run(['systemctl', 'daemon-reload'], mutating=True)
    _step('Sprawdzenie fstab (findmnt --verify)')
    v = run(['findmnt', '--verify', '--tab-file', str(FSTAB)], check=False)
    if v.returncode:
        raise RuntimeError('findmnt --verify zgłasza błąd po zmianie fstab:\n' + (v.stdout + v.stderr).strip())
    _step(f'Montowanie {mount} (na podstawie wpisu w fstab)')
    run(['mount', mount], mutating=True)
    undo.append((f'odmontowanie {mount}', f'umount {mount}', lambda: run(['umount', mount], mutating=True)))
    if not DRY_RUN:
        verify_mounted(mount, lvpath, uuid)
    return backup


def _manual(steps):
    print('Wykonaj ręcznie (w tej kolejności), po sprawdzeniu stanu systemu:')
    for desc, manual in steps:
        print(f'  {manual:<60} # {desc}')
    print('Nie restartuj hosta, dopóki `findmnt --verify` nie przejdzie bez błędów.')


def rollback(undo, assume_yes=False):
    """Wycofuje kroki w odwrotnej kolejności. Zwraca True, jeśli wszystko się udało."""
    if not undo:
        print('Nie wprowadzono żadnych zmian — nic do wycofania.')
        return True
    print('Dotychczas wykonane zmiany:')
    for desc, _, _ in undo:
        print(f'  - do wycofania: {desc}')
    if not assume_yes:
        try:
            answer = input('Czy wycofać zmiany? [T/n]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); answer = ''
        if answer in ('n', 'nie', 'no'):
            warn('Zmiany NIE zostały wycofane.')
            _manual([(d, m) for d, m, _ in reversed(undo)])
            return False
    failed = []
    for desc, manual, fn in reversed(undo):
        try:
            fn()
            ok(f'  ✓ {desc}')
        except (Exception, KeyboardInterrupt) as exc:
            err(f'  ✗ {desc}: {_describe(exc)}')
            log.error('Rollback nieudany: %s: %s', desc, _describe(exc))
            failed.append((desc, manual))
    undo.clear()
    if failed:
        err('Nie wszystkie zmiany udało się wycofać.')
        _manual(failed)
        return False
    ok('Zmiany zostały wycofane.')
    return True


def print_plan(p):
    rows = [('Grupa LVM (VG)', p['vg']),
            ('Nowy wolumen (LV)', p['lvpath']),
            ('Rozmiar', f'{p["size"]} GiB'),
            ('System plików', f'ext4, etykieta {p["name"][:16]}, rezerwa root 1% (FORMATOWANIE nowego LV)'),
            ('Punkt montowania', p['mount']),
            (f'Wpis w {FSTAB}', fstab_line('<UUID nowego LV>', p['mount']).strip()),
            ('Wolne w VG po operacji', fmt_gib(p['free'] - p['size'] * GIB))]
    print('\n=== PLAN ===')
    for label, value in rows:
        print(f'  {label:<24} {value}')
    print('W razie błędu zmiany zostaną wycofane automatycznie.')


def create_volume(vg, name, size_gib, mount, assume_yes=False):
    """Waliduje, pokazuje plan, pyta o potwierdzenie i wykonuje transakcję z rollbackiem."""
    try:
        plan = validate_request(vg, name, size_gib, mount)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        err(_describe(exc))
        return False
    print_plan(plan)
    if assume_yes:
        warn('Potwierdzenie pominięte (--yes).')
    else:
        try:
            answer = input(f'Aby uruchomić, wpisz dokładnie {CONFIRM_PHRASE}: ').strip()
        except EOFError:
            answer = ''
        if answer != CONFIRM_PHRASE:
            print('Anulowano — nic nie zmieniono.')
            return False
    log.info('START tworzenia: %s', plan)
    undo = []
    try:
        backup = _execute(plan, undo)
    except (Exception, KeyboardInterrupt) as exc:
        print()
        err(f'Operacja nie powiodła się: {_describe(exc)}')
        log.error('Transakcja nieudana: %s', _describe(exc))
        rollback(undo, assume_yes)
        return False
    if DRY_RUN:
        ok('\nDRY-RUN zakończony — nic nie zostało zmienione.')
        return True
    log.info('SUKCES: %s zamontowany w %s', plan['lvpath'], plan['mount'])
    ok(f'\nGOTOWE. Nowy datastore: {plan["mount"]}. Kopia fstab: {backup}')
    output(['df', '-h', plan['mount']])
    print(MORPHEUS_HINT)
    return True


# --- tryb interaktywny ------------------------------------------------------------

def ask(prompt, default=None, convert=str):
    """Pyta do skutku: Enter = domyślna, q = anuluj, błąd = komunikat i ponowne pytanie."""
    hint = f' [{default}]' if default is not None else ''
    while True:
        try:
            raw = input(f'{prompt}{hint}: ').strip()
        except EOFError:
            raise Cancelled
        if raw.lower() == 'q':
            raise Cancelled
        try:
            return convert(raw or ('' if default is None else str(default)))
        except ValueError as exc:
            err(str(exc))


def pick_vg(raw, vgs):
    if raw.isdigit() and 1 <= int(raw) <= len(vgs):
        return vgs[int(raw) - 1]
    for v in vgs:
        if v['vg_name'] == raw:
            return v
    raise ValueError(f'Nie ma grupy {raw!r}. Podaj numer 1–{len(vgs)} lub nazwę.')


def show_status():
    for label, command in [
        ('Dyski', ['lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS']),
        ('Grupy LVM', ['vgs', '-o', 'vg_name,vg_size,vg_free', '--units', 'g']),
        ('Wolumeny LVM', ['lvs', '-o', 'vg_name,lv_name,lv_size,lv_path']),
        ('Zamontowane systemy plików', ['df', '-hT']),
    ]:
        print('\n===', label, '===')
        output(command)


def create_store():
    print('\nKreator tworzenia NOWEGO wolumenu LVM. Nie służy do przebudowy istniejących.')
    print('Enter = wartość domyślna w nawiasie, q = powrót do menu.')
    try:
        vgs = list_vgs()
        if not vgs:
            err('Nie znaleziono żadnej grupy LVM (VG).'); return
        print('\nGrupy LVM:')
        for i, v in enumerate(vgs, 1):
            print(f'  {i}) {v["vg_name"]} — rozmiar {fmt_gib(v["vg_size"])}, wolne {fmt_gib(v["vg_free"])}')
        vg = ask('Wybierz grupę (numer lub nazwa)', vgs[0]['vg_name'] if len(vgs) == 1 else None,
                 lambda s: pick_vg(s, vgs))
        max_size = vg['vg_free'] // GIB - RESERVE_GIB
        if max_size < 1:
            err(f'Brak miejsca w {vg["vg_name"]}: wolne {fmt_gib(vg["vg_free"])}, '
                f'wymagana rezerwa {RESERVE_GIB} GiB.'); return
        name = ask('Nazwa nowego wolumenu', 'vm-storage', lambda s: check_new_name(vg['vg_name'], s))

        def check_size(s):
            size = parse_size(s)
            if not size_ok(size, vg['vg_free']):
                raise ValueError(f'Za dużo — maksymalnie {max_size} GiB ({RESERVE_GIB} GiB zostaje w rezerwie).')
            return size
        size = ask(f'Rozmiar w GiB (maks. {max_size})', max_size, check_size)
        default_mount = f'/{name}' if mount_allowed(f'/{name}') else '/vm-storage'
        mount = ask('Punkt montowania', default_mount, check_new_mount)
    except Cancelled:
        print('\nAnulowano — powrót do menu.'); return
    except (KeyError, OSError, subprocess.CalledProcessError) as exc:
        err(_describe(exc)); return
    create_volume(vg['vg_name'], name, size, mount)


def interactive():
    print(f'HVM Storage Manager {__version__} — polski kreator LVM' + (' [DRY-RUN]' if DRY_RUN else ''))
    while True:
        print('\n1. Pokaż dyski i miejsce\n2. Utwórz nowy datastore LVM\n3. Pokaż pule libvirt\n'
              '4. Sprawdź fstab i mounty\n5. Pokaż maszyny VM\n0. Wyjście')
        try:
            choice = input('Wybór: ').strip()
        except EOFError:
            print(); return 0
        if choice == '1': show_status()
        elif choice == '2': create_store()
        elif choice == '3': output(['virsh', 'pool-list', '--all'])
        elif choice == '4':
            output(['findmnt', '--verify']); output(['findmnt', '-t', 'ext4,xfs'])
        elif choice == '5': output(['virsh', 'list', '--all'])
        elif choice == '0': return 0
        else: print('Nieznana opcja.')


# --- CLI ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog='hvm_storage_manager.py',
                                description='Polski kreator NOWYCH wolumenów LVM/ext4 dla HPE HVM. '
                                            'Bez podkomendy uruchamia menu interaktywne.')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    p.add_argument('--dry-run', action='store_true',
                   help='pokaż polecenia zmieniające system zamiast je wykonywać')
    p.add_argument('--log-file', default=LOG_FILE, metavar='PATH', help=f'plik logu (domyślnie {LOG_FILE})')
    sub = p.add_subparsers(dest='command', metavar='{create}')
    c = sub.add_parser('create', help='utwórz nowy wolumen bez menu (nieinteraktywnie)')
    c.add_argument('--vg', required=True, help='istniejąca grupa LVM, np. ubuntu-vg')
    c.add_argument('--name', required=True, help='nazwa nowego LV, np. vm-storage')
    c.add_argument('--size', required=True, type=int, help='rozmiar w GiB')
    c.add_argument('--mount', required=True, help='punkt montowania, np. /vm-storage')
    c.add_argument('--yes', action='store_true', help=f'nie pytaj o frazę {CONFIRM_PHRASE!r}')
    c.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    global DRY_RUN
    args = build_parser().parse_args(argv)
    DRY_RUN = bool(args.dry_run)
    setup_logging(args.log_file)
    log.info('start v%s, polecenie=%s, dry_run=%s', __version__, args.command or 'menu', DRY_RUN)
    try:
        if not DRY_RUN and not is_root():
            err('Program wymaga uprawnień administratora.')
            print('Uruchom program: sudo python3 hvm_storage_manager.py  (podgląd bez zmian: --dry-run)')
            return 1
        try:
            if not acquire_lock():
                err('Działa już inna instancja programu. Zaczekaj na jej zakończenie.'); return 1
        except OSError as exc:
            err(f'Nie można założyć blokady {LOCK_FILE}: {exc}'); return 1
        if DRY_RUN:
            warn('Tryb DRY-RUN — polecenia zmieniające system zostaną tylko wypisane.')
        if args.command == 'create':
            return 0 if create_volume(args.vg, args.name, args.size, args.mount, args.yes) else 1
        return interactive()
    except KeyboardInterrupt:
        print('\nPrzerwano.')
        return 130
    except EOFError:
        print('\nPrzerwano.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
