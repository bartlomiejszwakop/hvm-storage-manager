"""LVM: odczyt (JSON), nazwy, rozmiary, ścieżki urządzeń, konflikty w /dev i spójność device-mapper."""
import json
import os
import posixpath
import re
import stat

from hvm import runner, ui

GIB = 1024 ** 3
MIN_RESERVE_GIB = 1
PROC = '/proc'
LVM_RESERVED_NAMES = ('.', '..')
LVM_RESERVED_PREFIXES = ('snapshot', 'pvmove')
LVM_RESERVED_PARTS = ('_cdata', '_cmeta', '_corig', '_cpool', '_cvol', '_imeta', '_iorig', '_mlog',
                      '_mimage', '_pmspare', '_rimage', '_rmeta', '_tdata', '_tmeta', '_vorigin',
                      '_vdata', '_wcorig')
NAME_HELP = ('litera na początku, dalej litery, cyfry oraz _ + . -; maks. 64 znaki; '
             'bez nazw zastrzeżonych przez LVM (snapshot, pvmove, _rimage, _tmeta...)')
LVS_FIELDS = 'lv_name,vg_name,lv_attr,lv_size,lv_path,lv_dm_path,lv_kernel_major,lv_kernel_minor'


# --- nazwy i rozmiary (czyste funkcje) ---------------------------------------------------

def verify_name(s):
    return (isinstance(s, str) and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_+.-]{0,63}', s) is not None
            and s not in LVM_RESERVED_NAMES and not s.startswith(LVM_RESERVED_PREFIXES)
            and not any(p in s for p in LVM_RESERVED_PARTS))


def verify_vg_name(s):
    # LVM dopuszcza nazwy VG zaczynające się od cyfry lub _, ale nie od '-'.
    return (isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9_+.][a-zA-Z0-9_+.-]{0,126}', s) is not None
            and s not in LVM_RESERVED_NAMES)


def reserve_for(free_bytes):
    """Rezerwa pozostawiana w VG (GiB): 1% wolnego miejsca zaokrąglony w górę, ale co najmniej 1 GiB."""
    return max(MIN_RESERVE_GIB, -(-int(free_bytes) // (100 * GIB)))


def max_size_gib(free_bytes):
    return max(int(free_bytes) // GIB - reserve_for(free_bytes), 0)


def size_ok(size_gib, free_bytes, reserve_gib=None):
    reserve = reserve_for(free_bytes) if reserve_gib is None else reserve_gib
    return size_gib >= 1 and (size_gib + reserve) * GIB <= free_bytes


def parse_size(s):
    s = str(s).strip()
    if not re.fullmatch(r'[0-9]+', s) or not 1 <= int(s) <= 1000000:
        raise ValueError('Rozmiar musi być liczbą całkowitą GiB z zakresu 1–1000000.')
    return int(s)


# --- odczyt LVM (JSON) ----------------------------------------------------------------------

def _report(stdout, key):
    data = json.loads(stdout or '{}')
    return [row for rep in data.get('report', []) for row in rep.get(key, [])]


def _bytes(value):
    return int(str(value).strip().lstrip('<>').split('.')[0] or 0)


def _json(cmd, fields, key):
    p = runner.run([cmd, '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', fields])
    return _report(p.stdout, key)


def list_vgs():
    """[{'vg_name', 'vg_size' (B), 'vg_free' (B)}]"""
    return [{'vg_name': r['vg_name'], 'vg_size': _bytes(r['vg_size']), 'vg_free': _bytes(r['vg_free'])}
            for r in _json('vgs', 'vg_name,vg_size,vg_free', 'vg')]


def list_pvs():
    return [{'pv_name': r.get('pv_name', ''), 'vg_name': r.get('vg_name', ''), 'pv_size': _bytes(r.get('pv_size', 0))}
            for r in _json('pvs', 'pv_name,vg_name,pv_size', 'pv')]


def list_lvs():
    out = []
    for r in _json('lvs', LVS_FIELDS, 'lv'):
        major, minor = str(r.get('lv_kernel_major', '')).strip(), str(r.get('lv_kernel_minor', '')).strip()
        out.append({'vg': r.get('vg_name', ''), 'name': r.get('lv_name', ''),
                    'attr': str(r.get('lv_attr', '')).ljust(10, '-'), 'size': _bytes(r.get('lv_size', 0)),
                    'path': r.get('lv_path', ''), 'dm_path': r.get('lv_dm_path', ''),
                    'majmin': f'{major}:{minor}' if major.isdigit() and minor.isdigit() else None})
    return out


def find_lv(vg, name, lvs=None):
    return next((lv for lv in (list_lvs() if lvs is None else lvs) if lv['vg'] == vg and lv['name'] == name), None)


def lv_exists(vg, name):
    return find_lv(vg, name) is not None


# --- ścieżki urządzeń ----------------------------------------------------------------------

def dm_name(vg, name):
    """Nazwa device-mapper: myślniki w nazwach VG i LV są podwajane, łącznik to jeden '-'."""
    return f"{vg.replace('-', '--')}-{name.replace('-', '--')}"


def dev_paths(vg, name):
    """(/dev/<vg>/<lv>, /dev/mapper/<dm>)"""
    return f'/dev/{vg}/{name}', f'/dev/mapper/{dm_name(vg, name)}'


def _majmin(st):
    major, minor = getattr(os, 'major', None), getattr(os, 'minor', None)
    return f'{major(st.st_rdev)}:{minor(st.st_rdev)}' if major and minor else '?:?'


def node_info(path):
    """(rodzaj, szczegół) obiektu pod ścieżką — lstat, bez podążania za dowiązaniem.
    Rodzaje: missing, link (cel), block (maj:min), dir (empty/nonempty/noaccess), file, other, error."""
    real = runner.host_path(path)
    try:
        st = os.lstat(real)
    except FileNotFoundError:
        return 'missing', ''
    except OSError as exc:
        return 'error', exc.strerror or str(exc)
    if stat.S_ISLNK(st.st_mode):
        try:
            return 'link', os.readlink(real)
        except OSError:
            return 'link', '?'
    if stat.S_ISBLK(st.st_mode):
        return 'block', _majmin(st)
    if stat.S_ISDIR(st.st_mode):
        try:
            return 'dir', 'nonempty' if os.listdir(real) else 'empty'
        except OSError:
            return 'dir', 'noaccess'
    return ('file', '') if stat.S_ISREG(st.st_mode) else ('other', '')


def path_state(path):
    kind, extra = node_info(path)
    return {'missing': 'brak', 'link': f'dowiązanie → {extra}', 'block': f'urządzenie blokowe {extra}',
            'file': 'zwykły plik', 'other': 'inny obiekt', 'error': f'błąd odczytu ({extra})',
            'dir': {'empty': 'katalog (pusty)', 'nonempty': 'katalog (niepusty)'}.get(extra, 'katalog (brak dostępu)'),
            }[kind]


def block_majmin(path):
    """'maj:min' urządzenia blokowego pod ścieżką (z podążaniem za dowiązaniami) albo None."""
    try:
        st = os.stat(runner.host_path(path))
    except OSError:
        return None
    return _majmin(st) if stat.S_ISBLK(st.st_mode) else None


def same_device(a, b):
    if not a or not b:
        return False
    if posixpath.normpath(a) == posixpath.normpath(b):
        return True
    ma = block_majmin(a)
    return ma is not None and ma == block_majmin(b)


def processes_using(path):
    """PID-y procesów, których cwd/root/otwarty plik leży w `path` (lub pod nim), na podstawie /proc.
    None = nie da się tego wiarygodnie sprawdzić (brak /proc lub brak dostępu)."""
    proc, target = runner.host_path(PROC), posixpath.normpath(path)
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    if not pids:
        return None
    found, denied = set(), False
    for pid in pids:
        base, links = posixpath.join(proc, pid), []
        for name in ('cwd', 'root'):
            try:
                links.append(os.readlink(posixpath.join(base, name)))
            except FileNotFoundError:
                pass
            except OSError:
                denied = True
        try:
            fds = os.listdir(posixpath.join(base, 'fd'))
        except FileNotFoundError:
            fds = []
        except OSError:
            denied, fds = True, []
        for fd in fds:
            try:
                links.append(os.readlink(posixpath.join(base, 'fd', fd)))
            except OSError:
                pass
        if any(link == target or link.startswith(target + '/') for link in links):
            found.add(int(pid))
    if denied and not found:
        return None
    return sorted(found)


# --- konflikt /dev/<vg>/<lv> (przypadek A) --------------------------------------------------

def _hint(path):
    return f'Sprawdź ręcznie: ls -la {path}; lvs; dmsetup ls'


def conflict_fixable(path):
    """(True, opis) wyłącznie dla PUSTEGO, niezamontowanego katalogu, którego nie używa żaden proces."""
    kind, extra = node_info(path)
    state = path_state(path)
    if kind in ('link', 'block'):
        return False, (f'{path} istnieje ({state}), a LV nie istnieje w LVM — nieaktualny węzeł urządzenia '
                       f'(pozostałość po usuniętym LV?). Nie usuwam go automatycznie. {_hint(path)}')
    if kind != 'dir':
        return False, f'{path} istnieje ({state}) i blokuje ścieżkę urządzenia — odmowa. {_hint(path)}'
    if os.path.ismount(runner.host_path(path)):
        return False, f'{path} jest punktem montowania — odmowa. {_hint(path)}'
    if extra != 'empty':
        return False, f'{path} istnieje ({state}) i blokuje ścieżkę urządzenia — odmowa (niepusty katalog). {_hint(path)}'
    users = processes_using(path)
    if users is None:
        return False, f'nie można sprawdzić (/proc), czy {path} jest używany przez procesy — odmowa. {_hint(path)}'
    if users:
        return False, f'{path} jest używany przez procesy (PID {", ".join(map(str, users))}) — odmowa.'
    return True, f'{path}: pusty katalog, nieużywany przez procesy — można go bezpiecznie usunąć (rmdir)'


def conflict_findings(vg, name):
    """Obiekty blokujące ścieżki urządzenia LV, który NIE istnieje w LVM: [{path, state, fixable, reason}]."""
    out = []
    for path in dev_paths(vg, name):
        if node_info(path)[0] == 'missing':
            continue
        fixable, reason = conflict_fixable(path)
        out.append({'path': path, 'state': path_state(path), 'fixable': fixable, 'reason': reason})
        runner.log.info('Konflikt ścieżki urządzenia: %s = %s', path, path_state(path))
    return out


def remove_conflict_dir(path):
    """rmdir pustego katalogu blokującego ścieżkę urządzenia (kontrole powtarzane tuż przed usunięciem)."""
    fixable, reason = conflict_fixable(path)
    if not fixable:
        raise RuntimeError(reason)
    if runner.DRY_RUN:
        print(f'[DRY-RUN] rmdir {path}')
        return
    os.rmdir(runner.host_path(path))  # tylko pusty katalog — nigdy rm -r
    runner.log.info('Usunięto pusty katalog blokujący ścieżkę urządzenia: %s', path)
    if node_info(path)[0] != 'missing':
        raise RuntimeError(f'{path} nadal istnieje po rmdir ({path_state(path)}) — odmowa.')
    ui.ok(f'Usunięto pusty katalog {path}.')


# --- urządzenie istniejącego LV (przypadek B) -----------------------------------------------

def wait_device(vg, name):
    """Czeka na urządzenie blokowe NOWEGO LV (preferowane /dev/mapper/...). Zwraca ścieżkę lub rzuca RuntimeError."""
    lvpath, mapper = dev_paths(vg, name)
    runner.run(['udevadm', 'settle', '--timeout=30'], check=False)
    if block_majmin(mapper):
        return _verified(vg, name, mapper)
    ui.warn(f'brak urządzenia {mapper} — odtwarzam węzły (vgscan --mknodes).')
    p = runner.run(['vgscan', '--mknodes'], check=False)
    runner.run(['udevadm', 'settle', '--timeout=30'], check=False)
    for dev in (mapper, lvpath):
        if block_majmin(dev):
            return _verified(vg, name, dev)
    raise RuntimeError(f'Urządzenie blokowe {mapper} nie pojawiło się (vgscan --mknodes: '
                       f'{(p.stderr or "").strip() or f"kod {p.returncode}"}). Sprawdź: lvs {vg}; dmsetup ls.')


def _verified(vg, name, dev):
    """Zwraca `dev`, jeśli jego maj:min zgadza się z numerami jądra LV w metadanych LVM."""
    lv = find_lv(vg, name)
    have = block_majmin(dev)
    if not lv or not lv['majmin'] or have != lv['majmin']:
        raise RuntimeError(f'{dev} ({have or "brak"}) nie odpowiada LV {vg}/{name} w LVM '
                           f'({(lv or {}).get("majmin") or "brak"}) — przerwano.')
    return dev


def holders(majmin):
    """Urządzenia zbudowane na tym LV (/sys/class/block/dm-N/holders); None = nie da się sprawdzić."""
    if not majmin or ':' not in majmin:
        return None
    path = runner.host_path(f'/sys/class/block/dm-{majmin.split(":")[1]}/holders')
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def device_check(vg, name, lv):
    """Spójność węzłów /dev istniejącego LV z metadanymi LVM (lv_kernel_major:minor).
    Zwraca dict: device (ścieżka do użycia albo None), case (None | 'A' | 'B'), lvpath, mapper, detail."""
    lvpath, mapper = dev_paths(vg, name)
    want, have = lv.get('majmin'), block_majmin(mapper)
    kind = node_info(lvpath)[0]
    res = {'device': None, 'case': None, 'lvpath': path_state(lvpath), 'mapper': path_state(mapper), 'detail': ''}
    if not want:
        res['detail'] = f'LV {vg}/{name} nie ma numerów urządzenia w jądrze (nieaktywny?)'
        return res
    if have != want:
        res['detail'] = (f'{mapper}: {"brak urządzenia" if have is None else f"urządzenie {have}"}, a LVM oczekuje '
                         f'{want} — niespójność device-mapper. Sprawdź: dmsetup info {dm_name(vg, name)}; lvs {vg}')
        return res
    res['device'] = mapper
    if kind == 'link' and block_majmin(lvpath) == want:
        res['detail'] = f'{mapper} i {lvpath} zgodne z LVM ({want})'
    elif kind == 'missing':
        res['case'] = 'B'
        res['detail'] = f'brak dowiązania {lvpath}; {mapper} zgodny z metadanymi LVM ({want}) — używam {mapper}'
    elif kind == 'dir':
        res['case'] = 'A'
        res['detail'] = f'{lvpath} to {res["lvpath"]} zamiast dowiązania; {mapper} zgodny z LVM ({want}) — używam {mapper}'
    else:
        res['device'] = None
        res['detail'] = f'{lvpath}: {res["lvpath"]} — niezgodne z metadanymi LVM ({want}). {_hint(lvpath)}'
    return res
