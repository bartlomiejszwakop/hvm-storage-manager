#!/usr/bin/env python3
"""Bezpieczny kreator wolumenów LVM/ext4 dla laboratorium HPE HVM: tworzenie NOWYCH
wolumenów, dokańczanie konfiguracji ISTNIEJĄCYCH oraz wykrywanie pul libvirt.

Tylko biblioteka standardowa Pythona 3.8+, jeden plik. Każde polecenie
zewnętrzne przechodzi przez run(), które je loguje i respektuje --dry-run.
"""
import argparse
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

__version__ = '0.4.0'

FSTAB = Path('/etc/fstab')
DEV_ROOT = '/dev'  # podmieniane w testach
LOG_FILE = '/var/log/hvm-storage-manager.log'
LOCK_FILE = '/run/hvm-storage-manager.lock'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
GIB = 1024 ** 3
RESERVE_GIB = 1
CONFIRM_PHRASE = 'TWORZ NOWY WOLUMEN'
DEFAULT_MOUNT = '/vm-storage'
# Krótka nazwa hosta (małe litery) → prefiks proponowanej nazwy datastore w Morpheus.
# Pozostałe hosty: nazwa hosta WIELKIMI literami.
HOST_ALIASES = {'hvm1': 'HVM1', 'node1': 'HVM1', 'hvm2': 'HVM2', 'node2': 'HVM2'}
DRY_UUID = '<UUID-nowego-LV>'
MORPHEUS_HINT = 'Dodaj w Morpheus: Infrastructure → Clusters → Storage → Datastores → ADD → Directory Pool.'
HOSTS_NOTE = ('UWAGA: oba hosty HVM mogą mieć pule o tej samej nazwie, ale każdy ma INNY, lokalny dysk — '
              'to nie jest współdzielony datastore.')

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


def nofail_warning(mount):
    warn(f'wpis w fstab ma opcję nofail — system uruchomi się także BEZ tego datastore. '
         f'Przed startem VM sprawdź: findmnt -M {mount}')


def _describe(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or '').strip()
        return f'polecenie `{shlex.join(exc.cmd)}` zakończone kodem {exc.returncode}' + (f': {detail}' if detail else '')
    if isinstance(exc, KeyboardInterrupt):
        return 'przerwano (Ctrl+C)'
    return str(exc) or type(exc).__name__


def ask_yes_no(prompt):
    """Pytanie [t/N]: domyślnie NIE (także przy EOF)."""
    try:
        return input(prompt).strip().lower() in ('t', 'tak', 'y', 'yes')
    except EOFError:
        print(); return False


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


def lv_info(vg, name):
    """{'attr': lv_attr, 'size': bajty} albo None, gdy LV nie istnieje."""
    p = run(['lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'lv_name,vg_name,lv_attr,lv_size'])
    for r in _report(p.stdout, 'lv'):
        if r.get('vg_name') == vg and r.get('lv_name') == name:
            return {'attr': str(r.get('lv_attr', '')).ljust(10, '-'), 'size': _bytes(r.get('lv_size', 0))}
    return None


# --- ścieżki urządzeń -----------------------------------------------------------

def dm_name(vg, name):
    """Nazwa device-mapper: myślniki w nazwach VG i LV są podwajane, łącznik to jeden '-'."""
    return f"{vg.replace('-', '--')}-{name.replace('-', '--')}"


def dev_paths(vg, name):
    """(/dev/<vg>/<lv>, /dev/mapper/<dm>) — względem DEV_ROOT."""
    return posixpath.join(DEV_ROOT, vg, name), posixpath.join(DEV_ROOT, 'mapper', dm_name(vg, name))


def _majmin(st):
    major, minor = getattr(os, 'major', None), getattr(os, 'minor', None)
    return f'{major(st.st_rdev)}:{minor(st.st_rdev)}' if major and minor else '?:?'


def path_state(path):
    """Opis obiektu pod ścieżką (lstat — bez podążania za dowiązaniem)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return 'brak'
    except OSError as exc:
        return f'błąd odczytu ({exc.strerror or exc})'
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = '?'
        return f'dowiązanie → {target}'
    if stat.S_ISBLK(st.st_mode):
        return f'urządzenie blokowe {_majmin(st)}'
    if stat.S_ISDIR(st.st_mode):
        try:
            return 'katalog (niepusty)' if os.listdir(path) else 'katalog (pusty)'
        except OSError:
            return 'katalog (brak dostępu)'
    return 'zwykły plik' if stat.S_ISREG(st.st_mode) else 'inny obiekt'


def _is_block(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def check_dev_conflict(vg, name, assume_yes=False, fix=False):
    """Gdy LV nie istnieje w LVM, ścieżki /dev/<vg>/<lv> i /dev/mapper/<dm> muszą być wolne.
    Jedyna naprawa: rmdir PUSTEGO katalogu po osobnym potwierdzeniu (w trybie --yes tylko
    z flagą --fix-dev-conflict). Rzuca ValueError z diagnozą."""
    if lv_exists(vg, name):
        return  # LV istnieje — ścieżki należą do niego (obsługa po stronie wywołującego)
    for path in dev_paths(vg, name):
        state = path_state(path)
        if state == 'brak':
            continue
        log.info('Konflikt ścieżki urządzenia: %s = %s', path, state)
        hint = f'Sprawdź ręcznie: ls -la {path}; dmsetup info {dm_name(vg, name)}; lvs {vg}'
        if state.startswith(('dowiązanie', 'urządzenie blokowe')):
            raise ValueError(f'{path} istnieje ({state}), a LV {vg}/{name} NIE istnieje w LVM — nieaktualny węzeł '
                             f'urządzenia (pozostałość po usuniętym LV?). Nie usuwam go automatycznie. {hint}')
        if state.startswith('katalog') and os.path.ismount(path):
            raise ValueError(f'{path} jest punktem montowania — odmowa. {hint}')
        if state != 'katalog (pusty)':
            raise ValueError(f'{path} istnieje ({state}) i blokuje utworzenie LV — odmowa. {hint}')
        print(f'\nWykryto PUSTY katalog {path}, a LV {vg}/{name} nie istnieje w LVM.\n'
              'To typowa przyczyna błędu lvcreate „device not cleared”: udev/LVM nie może tu utworzyć dowiązania.')
        if fix:
            warn(f'usuwam pusty katalog {path} (zgoda: --fix-dev-conflict).')
        elif assume_yes:
            raise ValueError(f'tryb --yes nie usuwa {path} automatycznie. Uruchom polecenie interaktywnie (bez --yes) '
                             'albo dodaj --fix-dev-conflict.')
        elif not ask_yes_no('Usunąć wyłącznie ten pusty katalog (rmdir)? [t/N]: '):
            raise ValueError(f'Nie usunięto {path} — utworzenie LV nie jest możliwe. {hint}')
        remove_dir(path)
        log.info('Usunięto pusty katalog blokujący ścieżkę urządzenia: %s', path)
        if DRY_RUN:
            continue
        if lv_exists(vg, name):
            raise ValueError(f'Po rmdir {path} LV {vg}/{name} pojawił się w LVM — przerwano, sprawdź stan ręcznie.')
        if path_state(path) != 'brak':
            raise ValueError(f'{path} nadal istnieje po rmdir ({path_state(path)}) — odmowa. {hint}')
        ok(f'Usunięto pusty katalog {path}.')


def wait_device(vg, name):
    """Czeka na urządzenie blokowe LV (preferowane /dev/mapper/...). Zwraca ścieżkę lub rzuca RuntimeError."""
    lvpath, mapper = dev_paths(vg, name)
    run(['udevadm', 'settle', '--timeout=30'], check=False)
    if _is_block(mapper):
        return mapper
    warn(f'brak urządzenia {mapper} — odtwarzam węzły (vgscan --mknodes).')
    p = run(['vgscan', '--mknodes'], check=False, mutating=True)
    run(['udevadm', 'settle', '--timeout=30'], check=False)
    for dev in (mapper, lvpath):
        if _is_block(dev):
            return dev
    raise RuntimeError(f'Urządzenie blokowe {mapper} nie pojawiło się (vgscan --mknodes: '
                       f'{(p.stderr or "").strip() or f"kod {p.returncode}"}). Sprawdź: lvs {vg}; dmsetup ls.')


def is_mounted(mount):
    return run(['findmnt', '-n', '-M', mount], check=False).returncode == 0


def detect_signature(device):
    """('empty' | 'ext4' | 'other', opis) — wyłącznie odczyt (wipefs -n, blkid -p)."""
    w = run(['wipefs', '-n', '--noheadings', '--output', 'TYPE', device], check=False)
    if w.returncode:
        raise RuntimeError(f'wipefs -n nie odczytał sygnatur {device}: {w.stderr.strip() or f"kod {w.returncode}"}')
    types = {t.strip() for t in w.stdout.splitlines() if t.strip()}
    b = run(['blkid', '-p', '-o', 'export', device], check=False)
    if b.returncode not in (0, 2):
        return 'other', f'blkid -p kod {b.returncode} (niejednoznaczne lub uszkodzone sygnatury)'
    info = dict(line.split('=', 1) for line in b.stdout.splitlines() if '=' in line) if b.returncode == 0 else {}
    found = types | {v for k, v in info.items() if k in ('TYPE', 'PTTYPE') and v}
    if not found and b.returncode == 2:
        return 'empty', 'brak sygnatur (pusty wolumen)'
    if info.get('TYPE') == 'ext4' and 'PTTYPE' not in info and types <= {'ext4'}:
        return 'ext4', 'ext4' + (f' (UUID {info["UUID"]})' if info.get('UUID') else '')
    return 'other', ', '.join(sorted(found)) or 'nieznana sygnatura'


def read_uuid(device):
    uuid = run(['blkid', '-p', '-s', 'UUID', '-o', 'value', device]).stdout.strip()
    if not UUID_RE.fullmatch(uuid):
        raise RuntimeError(f'Nie udało się odczytać UUID systemu plików na {device} ({uuid!r}).')
    return uuid


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


def check_allowed_mount(raw):
    mount = normalize_mount(raw)
    if not mount_allowed(mount):
        raise ValueError(f'{mount} nie jest na liście dozwolonych lokalizacji: ' + ', '.join(ALLOWED_MOUNT_PATTERNS))
    return mount


def _fstab_fields(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            fields = line.split()
            if len(fields) >= 2:
                yield fields


def _unescape(s):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), s)


def _is_uuid_source(field, uuid):
    u = uuid.lower()
    return _unescape(field).replace('"', '').lower() in (f'uuid={u}', f'/dev/disk/by-uuid/{u}')


def fstab_has_mount(text, mount):
    target = posixpath.normpath(mount)
    return any(posixpath.normpath(_unescape(f[1])) == target for f in _fstab_fields(text))


def fstab_has_uuid(text, uuid):
    return any(_is_uuid_source(f[0], uuid) for f in _fstab_fields(text))


def fstab_check(text, mount, uuid=None):
    """'ok' = dokładnie jeden wpis dla `mount` ze źródłem UUID=<uuid>; 'missing' = brak wpisu dla
    `mount` i dla UUID. Każdy inny stan (inne źródło, duplikaty, UUID w innym miejscu) → ValueError."""
    target = posixpath.normpath(mount)
    rows = list(_fstab_fields(text))
    by_mount = [f for f in rows if posixpath.normpath(_unescape(f[1])) == target]
    by_uuid = [f for f in rows if uuid and _is_uuid_source(f[0], uuid)]
    if not by_mount and not by_uuid:
        return 'missing'
    if uuid and len(by_mount) == 1 and by_uuid == by_mount:
        return 'ok'
    found = '; '.join(' '.join(f[:3]) for f in by_mount + [f for f in by_uuid if f not in by_mount])
    raise ValueError(f'{FSTAB} zawiera inny lub zdublowany wpis dla {mount}' + (f' / UUID {uuid}' if uuid else '')
                     + f': {found}. Wymagany ręczny przegląd {FSTAB} — program go nie zmieni.')


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
        raise ValueError(f'Wolumen /dev/{vg}/{name} już istnieje — nie zostanie sformatowany '
                         '(aby dokończyć jego konfigurację, użyj „resume”).')
    return name


def check_new_mount(raw):
    mount = check_allowed_mount(raw)
    check_mount_target(mount)
    if fstab_has_mount(read_fstab(), mount):
        raise ValueError(f'{mount} jest już wpisany w {FSTAB}.')
    return mount


def fstab_verify_ok():
    return run(['findmnt', '--verify', '--tab-file', str(FSTAB)], check=False).returncode == 0


def validate_request(vg, name, size_gib, mount, assume_yes=False, fix_dev=False):
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
    if not fstab_verify_ok():
        raise ValueError(f'findmnt --verify zgłasza błędy w obecnym {FSTAB} — napraw je przed dodaniem nowego wpisu.')
    check_dev_conflict(vg, name, assume_yes, fix_dev)  # na końcu: ewentualny rmdir dopiero po reszcie walidacji
    return {'kind': 'create', 'vg': vg, 'name': name, 'size': size, 'mount': mount, 'lvpath': f'/dev/{vg}/{name}',
            'free': free}


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


def make_dirs(dirs, undo):
    for d in dirs:
        if DRY_RUN:
            print(f'[DRY-RUN] mkdir {d}')
        else:
            os.mkdir(d, 0o755)
        undo.append((f'usunięcie utworzonego katalogu {d}', f'rmdir {d}', lambda d=d: remove_dir(d)))


def add_fstab_entry(uuid, mount, undo, plan):
    """Kopia + atomowe dopisanie wpisu + daemon-reload + findmnt --verify (z wpisem do undo)."""
    original = read_fstab()
    if fstab_has_mount(original, mount) or fstab_has_uuid(original, uuid):
        raise RuntimeError(f'{FSTAB} zmienił się w trakcie pracy — już zawiera {mount} lub UUID {uuid}.')
    backup = FSTAB.with_name(f'{FSTAB.name}.backup-hvm-{datetime.now():%Y%m%d-%H%M%S}')
    _step(f'Kopia zapasowa {FSTAB} → {backup}')
    if DRY_RUN:
        print(f'[DRY-RUN] cp -p {FSTAB} {backup}')
    else:
        shutil.copy2(str(FSTAB), str(backup))
        plan['backup'] = backup
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
    return backup


def mount_and_verify(mount, device, uuid, undo):
    _step(f'Montowanie {mount} (na podstawie wpisu w fstab)')
    run(['mount', mount], mutating=True)
    undo.append((f'odmontowanie {mount}', f'umount {mount}', lambda: run(['umount', mount], mutating=True)))
    if not DRY_RUN:
        verify_mounted(mount, device, uuid)


def verify_mounted(mount, device, uuid):
    src = run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', mount], check=False).stdout.strip()
    fs_uuid = run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', mount], check=False).stdout.strip()
    if (not uuid or fs_uuid.lower() != uuid.lower()) and (not src or os.path.realpath(src) != os.path.realpath(device)):
        raise RuntimeError(f'{mount} nie wskazuje na wolumen {device} (źródło: {src or "brak"}).')


# --- transakcja ---------------------------------------------------------------------

def _step(msg):
    print(f'→ {msg}')
    log.info('KROK: %s', msg)


def _lvremove_undo(lvpath):
    return (f'usunięcie LV {lvpath} utworzonego przez tę operację', f'lvremove {lvpath}',
            lambda: run(['lvremove', '-y', lvpath], mutating=True))


def _execute(plan, undo, assume_yes=False, fix_dev=False):
    """Wykonuje kroki; po każdym udanym kroku dokłada do `undo` (opis, polecenie ręczne, funkcja)."""
    vg, name, mount, lvpath, size = plan['vg'], plan['name'], plan['mount'], plan['lvpath'], plan['size']
    if not DRY_RUN:  # ponowna kontrola tuż przed lvcreate (stan mógł się zmienić od walidacji)
        if lv_exists(vg, name):
            raise RuntimeError(f'Wolumen {lvpath} pojawił się w trakcie pracy — przerwano, nie jest nasz.')
        check_dev_conflict(vg, name, assume_yes, fix_dev)
    _step(f'Tworzenie wolumenu {lvpath} ({size} GiB)')
    try:
        run(['lvcreate', '-L', f'{size}G', '-n', name, vg], mutating=True)
    except (Exception, KeyboardInterrupt):
        if not DRY_RUN:
            try:
                partial = lv_exists(vg, name)
            except Exception as exc:  # noqa: BLE001 — diagnoza nie może zasłonić pierwotnego błędu
                partial = None
                warn(f'nie udało się sprawdzić, czy LV powstał mimo błędu: {_describe(exc)}')
            if partial:
                warn(f'lvcreate zgłosił błąd, ale LV {lvpath} istnieje — utworzony częściowo przez TO uruchomienie, '
                     'zostanie usunięty przy wycofaniu.')
                undo.append(_lvremove_undo(lvpath))
        raise
    undo.append(_lvremove_undo(lvpath))
    device = dev_paths(vg, name)[1] if DRY_RUN else wait_device(vg, name)
    if not DRY_RUN:
        rc = run(['blkid', '-p', device], check=False).returncode
        if rc == 0 or rc == 8:
            raise RuntimeError('Na nowym LV wykryto sygnaturę systemu plików — przerwano przed formatowaniem.')
        if rc != 2:
            raise RuntimeError(f'Nie udało się sprawdzić sygnatur na nowym LV (blkid kod {rc}).')
    _step(f'Formatowanie ext4 (wyłącznie nowy LV, {device})')
    run(['mkfs.ext4', '-F', '-m', '1', '-L', name[:16], device], mutating=True)
    _step(f'Przygotowanie katalogu {mount}')
    make_dirs(missing_dirs(mount), undo)
    uuid = DRY_UUID if DRY_RUN else read_uuid(device)
    plan['uuid'] = uuid
    backup = add_fstab_entry(uuid, mount, undo, plan)
    mount_and_verify(mount, device, uuid, undo)
    return backup


def _manual(steps):
    print('Wykonaj ręcznie (w tej kolejności), po sprawdzeniu stanu systemu:')
    for desc, manual in steps:
        print(f'  {manual:<60} # {desc}')
    print('Nie restartuj hosta, dopóki `findmnt --verify` nie przejdzie bez błędów.')


def rollback(undo, assume_yes=False):
    """Wycofuje kroki w odwrotnej kolejności. Zwraca True, jeśli każdy krok wycofania się udał
    (rzeczywisty stan systemu sprawdza osobno diagnose_state())."""
    if not undo:
        print('Brak zarejestrowanych kroków do wycofania — sprawdzam rzeczywisty stan systemu.')
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
    print('Kroki wycofania wykonane — weryfikuję stan systemu.')
    return True


# --- diagnoza stanu -------------------------------------------------------------------

def _fs_type(device):
    return run(['blkid', '-p', '-s', 'TYPE', '-o', 'value', device], check=False).stdout.strip() or 'brak'


def snapshot(plan):
    """Stan istotny dla operacji: LV, ścieżka /dev, montowanie, fstab, katalogi (i FS przy resume)."""
    text = read_fstab()
    s = {'lv': lv_exists(plan['vg'], plan['name']),
         'dev': path_state(dev_paths(plan['vg'], plan['name'])[0]),
         'mounted': is_mounted(plan['mount']),
         'fstab': hashlib.sha256(text.encode('utf-8', 'surrogateescape')).hexdigest(),
         'dirs': tuple(d for d in plan['dirs'] if os.path.lexists(d))}
    if plan['kind'] == 'resume':
        s['fs'] = _fs_type(plan['device'])
    return s


def diagnose_state(plan):
    """Porównuje stan po wycofaniu ze stanem sprzed operacji. True = zgodny (zweryfikowany)."""
    print('\n=== WERYFIKACJA STANU ===')
    vg, name, mount, lvpath = plan['vg'], plan['name'], plan['mount'], plan['lvpath']
    devpath = dev_paths(vg, name)[0]
    run(['udevadm', 'settle', '--timeout=30'], check=False)
    try:
        after, text = snapshot(plan), read_fstab()
    except (Exception, KeyboardInterrupt) as exc:
        err(f'Nie udało się zweryfikować stanu systemu: {_describe(exc)}')
        print(f'Sprawdź ręcznie: lvs {vg}; ls -la {devpath}; findmnt -M {mount}; grep {mount} {FSTAB}')
        return False
    before = plan['before']
    yn = lambda b: 'tak' if b else 'nie'  # noqa: E731
    uuid = plan.get('uuid') if UUID_RE.fullmatch(plan.get('uuid') or '') else None
    fstab_desc = (('bez zmian' if after['fstab'] == before['fstab'] else 'ZMIENIONY')
                  + f'; wpis dla {mount}: {yn(fstab_has_mount(text, mount))}'
                  + (f'; wpis z UUID {uuid}: {yn(fstab_has_uuid(text, uuid))}' if uuid else ''))
    rows = [('lv', f'LV {lvpath} w LVM', yn(after['lv'])),
            ('dev', f'Ścieżka {devpath}', after['dev']),
            ('mounted', f'{mount} zamontowany', yn(after['mounted'])),
            ('fstab', str(FSTAB), fstab_desc),
            ('dirs', 'Katalogi utworzone przez program', ', '.join(after['dirs']) or 'brak')]
    if 'fs' in after:
        rows.append(('fs', f'System plików na {plan["device"]}', after['fs']))
    diff = [key for key, _, _ in rows if before.get(key) != after.get(key)]
    for key, label, value in rows:
        print(f'  {label:<44} {value}' + ('   ← INACZEJ niż przed operacją' if key in diff else ''))
    if not diff:
        ok('Wszystkie zmiany wycofane — stan zweryfikowany.' if plan.get('changed')
           else 'Nie wprowadzono żadnych zmian — stan zweryfikowany.')
        return True
    warn('stan systemu RÓŻNI SIĘ od stanu sprzed operacji.')
    manual = []
    if 'mounted' in diff:
        manual.append((f'{mount} jest zamontowany', f'umount {mount}') if after['mounted']
                      else (f'{mount} był zamontowany przed operacją', f'mount {mount}'))
    if 'fstab' in diff:
        bk = plan.get('backup')
        manual.append((f'{FSTAB} różni się od stanu sprzed operacji',
                       f'cp -p {bk} {FSTAB} && systemctl daemon-reload' if bk
                       else f'przejrzyj {FSTAB} ręcznie, potem: findmnt --verify'))
    for d in reversed(after['dirs']):
        if d not in before['dirs']:
            manual.append((f'katalog {d} utworzony przez program', f'rmdir {d}'))
    if 'lv' in diff and after['lv']:
        manual.append(('LV utworzony przez tę operację nadal istnieje', f'lvremove {lvpath}'))
    if 'dev' in diff:
        manual.append((f'ścieżka była „{before["dev"]}”, jest „{after["dev"]}”', f'ls -la {devpath}'))
    if 'fs' in diff:
        manual.append((f'na LV pozostał system plików {after["fs"]} utworzony w tym uruchomieniu (mkfs nie da się '
                       f'cofnąć; LV był pusty) — można ponowić „resume”', f'blkid -p {plan["device"]}'))
    _manual(manual)
    return False


def _fail_and_recover(plan, undo, exc, assume_yes):
    print()
    err(f'Operacja nie powiodła się: {_describe(exc)}')
    log.error('Transakcja nieudana: %s', _describe(exc))
    plan['changed'] = plan.get('changed') or bool(undo)
    rollback(undo, assume_yes)
    if plan.get('formatted'):
        warn(f'w tym uruchomieniu uruchomiono mkfs.ext4 na {plan["device"]} — tego nie da się cofnąć '
             '(LV był pusty). LV NIE został usunięty.')
    verified = diagnose_state(plan)
    log.info('Diagnoza po wycofaniu: %s', 'stan zgodny' if verified else 'stan RÓŻNY od wyjściowego')


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
    print('W razie błędu zmiany zostaną wycofane automatycznie, a stan systemu zweryfikowany.')


def create_volume(vg, name, size_gib, mount, assume_yes=False, fix_dev=False):
    """Waliduje, pokazuje plan, pyta o potwierdzenie i wykonuje transakcję z rollbackiem."""
    try:
        plan = validate_request(vg, name, size_gib, mount, assume_yes, fix_dev)
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
    plan['dirs'] = missing_dirs(plan['mount'])
    try:
        plan['before'] = snapshot(plan)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        err(f'Nie można odczytać stanu wyjściowego: {_describe(exc)}')
        return False
    undo = []
    try:
        backup = _execute(plan, undo, assume_yes, fix_dev)
    except (Exception, KeyboardInterrupt) as exc:
        _fail_and_recover(plan, undo, exc, assume_yes)
        return False
    if DRY_RUN:
        ok('\nDRY-RUN zakończony — nic nie zostało zmienione.')
        return True
    log.info('SUKCES: %s zamontowany w %s', plan['lvpath'], plan['mount'])
    ok(f'\nGOTOWE. Nowy datastore: {plan["mount"]}. Kopia fstab: {backup}')
    output(['df', '-h', plan['mount']])
    nofail_warning(plan['mount'])
    print(MORPHEUS_HINT)
    return True


# --- dokończenie konfiguracji istniejącego LV (resume) -----------------------------------

def plan_resume(vg, name, mount):
    """Sprawdza istniejący LV i wylicza potrzebne kroki. Tylko odczyt (poza vgscan --mknodes)."""
    if not verify_vg_name(vg):
        raise ValueError(f'Niedozwolona nazwa grupy LVM {vg!r}.')
    if not verify_name(name):
        raise ValueError(f'Niedozwolona nazwa wolumenu {name!r} ({NAME_HELP}).')
    mount = check_allowed_mount(mount)
    if vg not in {v['vg_name'] for v in list_vgs()}:
        raise ValueError(f'Grupa LVM {vg} nie istnieje.')
    lvpath = f'/dev/{vg}/{name}'
    info = lv_info(vg, name)
    if not info:
        raise ValueError(f'Wolumen {lvpath} nie istnieje — do utworzenia nowego użyj „create”.')
    attr = info['attr']
    if attr[4] != 'a':
        raise ValueError(f'LV {lvpath} nie jest aktywny (lv_attr {attr}). Aktywuj ręcznie: lvchange -ay {vg}/{name}')
    device = wait_device(vg, name)
    sig, sigdesc = detect_signature(device)
    if sig == 'other':
        raise ValueError(f'Na {device} wykryto: {sigdesc}. To nie jest pusty LV ani ext4 — odmowa '
                         '(program nie formatuje i nie montuje takich wolumenów).')
    uuid = read_uuid(device) if sig == 'ext4' else None
    targets = run(['findmnt', '-rn', '-o', 'TARGET', '-S', device], check=False).stdout.split()
    others = [t for t in targets if posixpath.normpath(_unescape(t)) != mount]
    if others:
        raise ValueError(f'{lvpath} jest zamontowany w innym miejscu: {", ".join(others)} — odmowa.')
    mounted = is_mounted(mount)
    if mounted:
        if sig != 'ext4':
            raise ValueError(f'{mount} jest zamontowany, a LV nie zawiera ext4 — pod {mount} jest INNE urządzenie.')
        try:
            verify_mounted(mount, device, uuid)
        except RuntimeError as exc:
            raise ValueError(f'Pod {mount} jest zamontowane INNE urządzenie — odmowa. ({exc})')
    else:
        if attr[5] == 'o':
            raise ValueError(f'LV {lvpath} jest otwarty (lv_attr {attr}) przez inny proces, montowanie lub VM, '
                             f'a nie jest zamontowany w {mount} — odmowa.')
        check_mount_target(mount)
    status = fstab_check(read_fstab(), mount, uuid)
    if status == 'missing' and not fstab_verify_ok():
        raise ValueError(f'findmnt --verify zgłasza błędy w obecnym {FSTAB} — napraw je przed dodaniem nowego wpisu.')
    return {'kind': 'resume', 'vg': vg, 'name': name, 'mount': mount, 'lvpath': lvpath, 'device': device,
            'attr': attr, 'size': info['size'], 'sig': sig, 'sigdesc': sigdesc, 'uuid': uuid, 'fstab': status,
            'mounted': mounted, 'dirs': [] if mounted else missing_dirs(mount), 'format': sig == 'empty'}


def resume_steps(plan):
    return plan['format'] or bool(plan['dirs']) or plan['fstab'] == 'missing' or not plan['mounted']


def print_resume_plan(p):
    rows = [('Wolumen (LV)', f'{p["lvpath"]} ({fmt_gib(p["size"])}, lv_attr {p["attr"]})'),
            ('Urządzenie', p['device']),
            ('Sygnatury', p['sigdesc']),
            ('Formatowanie ext4', f'WYKONAM — pusty LV (wymaga frazy FORMATUJ {p["name"]})' if p['format']
             else 'pominięte — wykryto ext4'),
            ('Katalog', f'utworzę: {", ".join(p["dirs"])}' if p['dirs'] else 'pominięte — już istnieje'),
            (f'Wpis w {FSTAB}', f'dopiszę: {fstab_line(p["uuid"] or "<UUID po formatowaniu>", p["mount"]).strip()}'
             if p['fstab'] == 'missing' else 'pominięte — poprawny wpis już istnieje'),
            ('Montowanie', f'mount {p["mount"]}' if not p['mounted'] else 'pominięte — ten LV jest już zamontowany')]
    print('\n=== PLAN DOKOŃCZENIA KONFIGURACJI ===')
    for label, value in rows:
        print(f'  {label:<24} {value}')
    print('W razie błędu cofnę wyłącznie kroki z tego uruchomienia (umount, fstab, katalogi). '
          'LV nigdy nie jest usuwany.')


def _resume_execute(plan, undo):
    device, mount, name = plan['device'], plan['mount'], plan['name']
    if plan['format']:
        if not DRY_RUN:
            sig, desc = detect_signature(device)
            if sig != 'empty':
                raise RuntimeError(f'Stan {device} zmienił się ({desc}) — przerwano przed formatowaniem.')
        _step(f'Formatowanie ext4 pustego LV {device}')
        plan['changed'] = plan['formatted'] = True
        run(['mkfs.ext4', '-F', '-m', '1', '-L', name[:16], device], mutating=True)
        plan['uuid'] = DRY_UUID if DRY_RUN else read_uuid(device)
    else:
        print('Wykryto ext4 — formatowanie pominięte.')
    uuid = plan['uuid']
    if plan['dirs']:
        _step(f'Przygotowanie katalogu {mount}')
        make_dirs(plan['dirs'], undo)
    if plan['fstab'] == 'missing':
        add_fstab_entry(uuid, mount, undo, plan)
    elif fstab_check(read_fstab(), mount, uuid) == 'ok':
        print(f'Wpis w {FSTAB} jest już poprawny — bez zmian.')
    else:
        raise RuntimeError(f'Wpis dla {mount} zniknął z {FSTAB} w trakcie pracy — przerwano.')
    if plan['mounted']:
        print(f'{mount} jest już zamontowany z tego LV — montowanie pominięte.')
    else:
        mount_and_verify(mount, device, uuid, undo)


def resume_volume(vg, name, mount, assume_yes=False, format_empty=False):
    """Idempotentnie dokańcza konfigurację ISTNIEJĄCEGO LV: ext4 (tylko pusty LV), katalog, fstab, mount."""
    try:
        plan = plan_resume(vg, name, mount)
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        err(_describe(exc))
        return False
    print_resume_plan(plan)
    if not resume_steps(plan):
        ok('Nic do zrobienia — konfiguracja jest kompletna, nie wprowadzono zmian.')
        nofail_warning(plan['mount'])
        return True
    if plan['format'] and assume_yes and not format_empty:
        err('LV jest pusty i wymaga formatowania. W trybie --yes dodaj --format-empty albo uruchom bez --yes.')
        return False
    phrase = f'FORMATUJ {name}'
    if assume_yes:
        warn('Potwierdzenie pominięte (--yes).' + (f' Formatowanie {plan["device"]} dozwolone flagą --format-empty.'
                                                   if plan['format'] else ''))
    else:
        if not ask_yes_no('Wykonać powyższe kroki? [t/N]: '):
            print('Anulowano — nic nie zmieniono.'); return False
        if plan['format']:
            try:
                answer = input(f'Formatowanie {plan["device"]} jest NIEODWRACALNE. Wpisz dokładnie {phrase}: ').strip()
            except EOFError:
                answer = ''
            if answer != phrase:
                print('Anulowano — nic nie zmieniono.'); return False
    log.info('START dokończenia: %s', plan)
    try:
        plan['before'] = snapshot(plan)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        err(f'Nie można odczytać stanu wyjściowego: {_describe(exc)}')
        return False
    undo = []
    try:
        _resume_execute(plan, undo)
    except (Exception, KeyboardInterrupt) as exc:
        _fail_and_recover(plan, undo, exc, assume_yes)
        return False
    if DRY_RUN:
        ok('\nDRY-RUN zakończony — nic nie zostało zmienione.')
        return True
    log.info('SUKCES dokończenia: %s zamontowany w %s', plan['lvpath'], plan['mount'])
    ok(f'\nGOTOWE. Datastore {plan["mount"]} skonfigurowany' + (f'. Kopia fstab: {plan["backup"]}'
                                                                  if plan.get('backup') else '.'))
    output(['df', '-h', plan['mount']])
    nofail_warning(plan['mount'])
    print(MORPHEUS_HINT)
    return True


# --- pule libvirt ---------------------------------------------------------------------

def parse_pool_xml(text):
    """(typ, ścieżka docelowa) z XML `virsh pool-dumpxml`; None przy niepoprawnym XML."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    return root.get('type'), (root.findtext('./target/path') or '').strip()


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            out[key.strip()] = value.strip()
    return out


def discover_pools(mount):
    """Istniejące pule libvirt typu dir, których katalog docelowy to dokładnie `mount` (tylko odczyt)."""
    p = run(['virsh', 'pool-list', '--all', '--name'], check=False)
    if p.returncode:
        raise RuntimeError(f'Nie można pobrać listy pul libvirt: {p.stderr.strip() or f"kod {p.returncode}"}')
    target, found = os.path.realpath(mount), []
    for name in (n.strip() for n in p.stdout.splitlines()):
        if not name:
            continue
        x = run(['virsh', 'pool-dumpxml', '--pool', name], check=False)
        parsed = parse_pool_xml(x.stdout) if x.returncode == 0 else None
        if parsed is None:
            warn(f'nie można odczytać definicji puli {name} — pominięto.'); continue
        ptype, path = parsed
        if ptype != 'dir' or not path or os.path.realpath(path) != target:
            continue
        info = run(['virsh', 'pool-info', '--pool', name], check=False)
        found.append({'name': name, 'path': path, 'info': parse_kv(info.stdout) if info.returncode == 0 else {}})
    return found


def pools_command(mount=DEFAULT_MOUNT, refresh=False, assume_yes=False):
    """Wykrywa pule dir na punkcie montowania; odświeża je (virsh pool-refresh) tylko po potwierdzeniu."""
    try:
        mount = normalize_mount(mount)
    except ValueError as exc:
        err(str(exc)); return False
    if os.path.islink(mount) or not os.path.isdir(mount):
        err(f'{mount} nie istnieje lub nie jest katalogiem (dowiązania są odrzucane).'); return False
    if not is_mounted(mount):
        err(f'STOP: {mount} NIE jest osobnym punktem montowania — ten katalog leży na dysku systemowym.')
        print(f'Pule nie zostaną odświeżone, aby nie pomylić dysku systemowego z datastore. Sprawdź: findmnt -M {mount}')
        return False
    print('\n=== MONTOWANIE ==='); output(['findmnt', '-M', mount])
    print('\n=== WOLNE MIEJSCE ==='); output(['df', '-hT', mount])
    try:
        pools = discover_pools(mount)
    except RuntimeError as exc:
        err(str(exc)); return False
    if not pools:
        print(f'Nie znaleziono puli libvirt typu dir wskazującej na {mount}.')
        print('Zarejestruj Directory Pool w panelu Morpheus lub sprawdź konfigurację klastra. '
              'Program NIE tworzy pul i nie wywołuje Morpheus API.')
        print(HOSTS_NOTE)
        return True
    print(f'\n=== PULE LIBVIRT (type=dir, katalog {mount}) ===')
    cols = ('State', 'Capacity', 'Available', 'Autostart')
    print(f'  {"Pula":<20}' + ''.join(f' {c:<14}' for c in cols))
    for pool in pools:
        print(f'  {pool["name"]:<20}' + ''.join(f' {pool["info"].get(c, "?"):<14}' for c in cols))
    print(HOSTS_NOTE)
    if not refresh:
        print('Odświeżenie: uruchom z --refresh.'); return True
    if assume_yes:
        warn('Potwierdzenie odświeżenia pominięte (--yes).')
    elif not ask_yes_no(f'Odświeżyć wyłącznie wymienione pule ({len(pools)}) przez virsh pool-refresh? [t/N]: '):
        print('Anulowano — pule nie zostały odświeżone.'); return True
    failed = 0
    for pool in pools:
        try:
            run(['virsh', 'pool-refresh', '--pool', pool['name']], mutating=True)
            ok(f'Odświeżono: {pool["name"]}')
        except subprocess.CalledProcessError as exc:
            err(f'Odświeżenie {pool["name"]} nieudane: {_describe(exc)}'); failed += 1
            continue
        if not DRY_RUN:
            output(['virsh', 'pool-info', '--pool', pool['name']])
    print('W panelu Morpheus odczyt pojemności może wymagać kolejnego cyklu synchronizacji.')
    return failed == 0


def suggest_datastore_name(hostname, size_bytes=None):
    """Np. HVM1-Local-1200 — unikalna per host, żeby nazwy z dwóch hostów się nie nakładały."""
    short = (hostname or '').split('.')[0].strip().lower() or 'host'
    prefix = HOST_ALIASES.get(short, short.upper())
    return f'{prefix}-Local' + (f'-{round(size_bytes / GIB)}' if size_bytes else '')


def mount_size_bytes(mount):
    """Rozmiar urządzenia zamontowanego pod `mount` (lsblk -b) albo None."""
    src = run(['findmnt', '-n', '-o', 'SOURCE', '-M', mount], check=False).stdout.strip()
    if not src:
        return None
    p = run(['lsblk', '-b', '-d', '-n', '-o', 'SIZE', src], check=False)
    value = p.stdout.strip().splitlines()[0].strip() if p.returncode == 0 and p.stdout.strip() else ''
    return int(value) if value.isdigit() else None


def audit_command(mount=DEFAULT_MOUNT):
    """Audyt lokalnego datastore na tym hoście — wyłącznie odczyt, niczego nie zmienia."""
    try:
        mount = normalize_mount(mount)
    except ValueError as exc:
        err(str(exc)); return False
    host = socket.gethostname()
    print('\n=== AUDYT LOKALNEGO DATASTORE (tylko odczyt) ===')
    print(f'Host: {host}')
    for label, command in [('Punkt montowania', ['findmnt', '-M', mount]), ('Miejsce', ['df', '-hT', mount]),
                           ('Wolumeny LVM', ['lvs', '-o', 'lv_name,vg_name,lv_size,lv_path']),
                           ('Pule libvirt', ['virsh', 'pool-list', '--all'])]:
        print(f'\n--- {label} ---'); output(command)
    if not is_mounted(mount):
        err(f'STOP: {mount} NIE jest osobnym punktem montowania — katalog leży na dysku systemowym.')
        print(f'Proponowana nazwa (bez rozmiaru): {suggest_datastore_name(host)}')
        return False
    ok(f'\nProponowana nazwa lokalnego datastore w Morpheus: {suggest_datastore_name(host, mount_size_bytes(mount))}')
    try:
        pools = discover_pools(mount)
    except RuntimeError as exc:
        err(str(exc)); return False
    short = host.split('.')[0].lower()
    tags = {short, HOST_ALIASES.get(short, short).lower()}
    print(f'\nPule libvirt typu dir wskazujące dokładnie {mount}:')
    for pool in pools:
        unique = any(t and t in pool['name'].lower() for t in tags)
        print(f'  - {pool["name"]}' + ('' if unique else
              '   ← nazwa nie zawiera nazwy hosta; na drugim hoście może być identyczna'))
    if not pools:
        print('  (brak) — pula wymaga osobnej konfiguracji (Directory Pool w Morpheus).')
    print('\nUWAGA: nazwa puli libvirt na hoście i nazwa Datastore w Morpheus to dwie różne rzeczy.')
    print('Samo virsh pool-define ani zmiana nazwy puli NIE tworzy datastore per host w Morpheus.')
    print('Przed migracją sprawdź, czy Twoja wersja HPE Morpheus 9.x obsługuje datastore przypisany do węzła.')
    print('Nie usuwaj obecnej puli ani jej plików — może być używana przez inne hosty/VM.')
    print(HOSTS_NOTE)
    print('Nie zmieniono żadnych zasobów.')
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
        ('Wolumeny LVM', ['lvs', '-o', 'vg_name,lv_name,lv_size,lv_attr,lv_path']),
        ('Zamontowane systemy plików', ['df', '-hT']),
    ]:
        print('\n===', label, '===')
        output(command)


def _syntax_name(s):
    if not verify_name(s):
        raise ValueError(f'Niedozwolona nazwa wolumenu {s!r} ({NAME_HELP}).')
    return s


def create_store():
    print('\nKreator: tworzenie NOWEGO wolumenu LVM albo dokończenie konfiguracji ISTNIEJĄCEGO.')
    print('Istniejący wolumen nigdy nie jest formatowany, jeśli zawiera jakąkolwiek sygnaturę.')
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
        while True:
            name = ask('Nazwa wolumenu', 'vm-storage', _syntax_name)
            if not lv_exists(vg['vg_name'], name):
                break
            print(f'Wolumen /dev/{vg["vg_name"]}/{name} już istnieje:')
            output(['lvs', '-o', 'lv_name,lv_size,lv_attr', f'{vg["vg_name"]}/{name}'])
            if ask_yes_no('Dokończyć konfigurację ISTNIEJĄCEGO LV? [t/N]: '):
                mount = ask('Punkt montowania', f'/{name}' if mount_allowed(f'/{name}') else DEFAULT_MOUNT,
                            check_allowed_mount)
                resume_volume(vg['vg_name'], name, mount)
                return
            print('Podaj inną nazwę nowego wolumenu (q = powrót).')
        max_size = vg['vg_free'] // GIB - RESERVE_GIB
        if max_size < 1:
            err(f'Brak miejsca w {vg["vg_name"]}: wolne {fmt_gib(vg["vg_free"])}, '
                f'wymagana rezerwa {RESERVE_GIB} GiB.'); return

        def check_size(s):
            size = parse_size(s)
            if not size_ok(size, vg['vg_free']):
                raise ValueError(f'Za dużo — maksymalnie {max_size} GiB ({RESERVE_GIB} GiB zostaje w rezerwie).')
            return size
        size = ask(f'Rozmiar w GiB (maks. {max_size})', max_size, check_size)
        default_mount = f'/{name}' if mount_allowed(f'/{name}') else DEFAULT_MOUNT
        mount = ask('Punkt montowania', default_mount, check_new_mount)
    except Cancelled:
        print('\nAnulowano — powrót do menu.'); return
    except (KeyError, OSError, subprocess.CalledProcessError) as exc:
        err(_describe(exc)); return
    create_volume(vg['vg_name'], name, size, mount)


def check_mounts():
    output(['findmnt', '--verify'])
    output(['findmnt', '-t', 'ext4,xfs'])
    mount = ask('Punkt montowania do sprawdzenia', DEFAULT_MOUNT, normalize_mount)
    if output(['findmnt', '-M', mount]):
        warn(f'{mount} NIE jest zamontowany — nie uruchamiaj VM korzystających z tego datastore.')


def interactive():
    print(f'HVM Storage Manager {__version__} — polski kreator LVM' + (' [DRY-RUN]' if DRY_RUN else ''))
    while True:
        print('\n1. Pokaż dyski i miejsce\n2. Utwórz / dokończ datastore LVM\n3. Pokaż pule libvirt\n'
              '4. Sprawdź fstab i mounty\n5. Pokaż maszyny VM\n6. Wykryj i odśwież pule libvirt\n'
              '7. Audyt lokalnego datastore na tym hoście\n0. Wyjście')
        try:
            choice = input('Wybór: ').strip()
        except EOFError:
            print(); return 0
        try:
            if choice == '1': show_status()
            elif choice == '2': create_store()
            elif choice == '3': output(['virsh', 'pool-list', '--all'])
            elif choice == '4': check_mounts()
            elif choice == '5': output(['virsh', 'list', '--all'])
            elif choice == '6': pools_command(ask('Punkt montowania', DEFAULT_MOUNT, normalize_mount), refresh=True)
            elif choice == '7': audit_command(ask('Punkt montowania', DEFAULT_MOUNT, normalize_mount))
            elif choice == '0': return 0
            else: print('Nieznana opcja.')
        except Cancelled:
            print('\nAnulowano — powrót do menu.')


# --- CLI ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog='hvm_storage_manager.py',
                                description='Polski kreator wolumenów LVM/ext4 dla HPE HVM. '
                                            'Bez podkomendy uruchamia menu interaktywne.')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    p.add_argument('--dry-run', action='store_true',
                   help='pokaż polecenia zmieniające system zamiast je wykonywać')
    p.add_argument('--log-file', default=LOG_FILE, metavar='PATH', help=f'plik logu (domyślnie {LOG_FILE})')
    sub = p.add_subparsers(dest='command', metavar='{create,resume,pools,audit}')
    c = sub.add_parser('create', help='utwórz nowy wolumen bez menu (nieinteraktywnie)')
    c.add_argument('--vg', required=True, help='istniejąca grupa LVM, np. ubuntu-vg')
    c.add_argument('--name', required=True, help='nazwa nowego LV, np. vm-storage')
    c.add_argument('--size', required=True, type=int, help='rozmiar w GiB')
    c.add_argument('--mount', required=True, help='punkt montowania, np. /vm-storage')
    c.add_argument('--yes', action='store_true', help=f'nie pytaj o frazę {CONFIRM_PHRASE!r}')
    c.add_argument('--fix-dev-conflict', action='store_true',
                   help='pozwól usunąć (rmdir) PUSTY katalog blokujący /dev/<vg>/<lv> także w trybie --yes')
    r = sub.add_parser('resume', help='dokończ konfigurację ISTNIEJĄCEGO LV (ext4, katalog, fstab, mount)')
    r.add_argument('--vg', required=True, help='grupa LVM, np. ubuntu-vg')
    r.add_argument('--name', required=True, help='nazwa istniejącego LV, np. vm-storage')
    r.add_argument('--mount', required=True, help='punkt montowania, np. /vm-storage')
    r.add_argument('--yes', action='store_true', help='nie pytaj o potwierdzenie kroków')
    r.add_argument('--format-empty', action='store_true',
                   help='w trybie --yes pozwól sformatować LV BEZ żadnych sygnatur (bez frazy FORMATUJ <nazwa>)')
    pl = sub.add_parser('pools', help='wykryj pule libvirt typu dir na punkcie montowania i opcjonalnie odśwież')
    pl.add_argument('--mount', default=DEFAULT_MOUNT, help=f'punkt montowania (domyślnie {DEFAULT_MOUNT})')
    pl.add_argument('--refresh', action='store_true', help='odśwież znalezione pule (virsh pool-refresh)')
    pl.add_argument('--yes', action='store_true', help='nie pytaj o potwierdzenie odświeżenia')
    a = sub.add_parser('audit', help='audyt lokalnego datastore na tym hoście i propozycja nazwy (tylko odczyt)')
    a.add_argument('--mount', default=DEFAULT_MOUNT, help=f'punkt montowania (domyślnie {DEFAULT_MOUNT})')
    for s in (c, r, pl, a):
        s.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
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
            return 0 if create_volume(args.vg, args.name, args.size, args.mount, args.yes,
                                      fix_dev=args.fix_dev_conflict) else 1
        if args.command == 'resume':
            return 0 if resume_volume(args.vg, args.name, args.mount, args.yes,
                                      format_empty=args.format_empty) else 1
        if args.command == 'pools':
            return 0 if pools_command(args.mount, args.refresh, args.yes) else 1
        if args.command == 'audit':
            return 0 if audit_command(args.mount) else 1
        return interactive()
    except KeyboardInterrupt:
        print('\nPrzerwano.')
        return 130
    except EOFError:
        print('\nPrzerwano.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
