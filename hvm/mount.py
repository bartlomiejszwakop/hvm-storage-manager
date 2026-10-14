"""Punkty montowania: allowlista, /etc/fstab (parsowanie, kontrola, atomowy zapis z kopią),
lista montowań (findmnt JSON), mount + weryfikacja."""
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
from datetime import datetime

from hvm import lvm, runner

FSTAB = '/etc/fstab'
FINDMNT_ALL = ['findmnt', '-J', '-l', '-b', '-o', 'TARGET,SOURCE,FSTYPE,SIZE,UUID']

# Allowlista punktów montowania: wyrażenia regularne dopasowywane do CAŁEJ znormalizowanej
# ścieżki. Aby dopuścić nową lokalizację, dopisz wzorzec (tylko małe litery, cyfry, '_' i '-').
ALLOWED_MOUNT_PATTERNS = [
    r'^/vm[a-z0-9_-]*$',                       # /vm-storage, /vm_data, /vm2
    r'^/srv/[a-z0-9_-]+$',                     # /srv/vm-storage
    r'^/data/[a-z0-9_-]+$',                    # /data/vm-storage
]
# Katalogi pul chronionych (local, morpheus-*) — punkt montowania nie może być w nich, nad nimi ani
# nimi samymi, niezależnie od tego, czy libvirt w tej chwili odpowiada.
PROTECTED_TARGETS = ('/var/lib/libvirt/images', '/var/morpheus')


# --- walidacja ścieżki (czyste funkcje) ------------------------------------------------------

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


def _within(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def check_allowed_mount(raw):
    mount = normalize_mount(raw)
    for t in PROTECTED_TARGETS:
        if _within(mount, t) or _within(t, mount):
            raise ValueError(f'{mount} pokrywa się z chronionym katalogiem {t} (pule local / Morpheus) — odmowa.')
    if not mount_allowed(mount):
        raise ValueError(f'{mount} nie jest na liście dozwolonych lokalizacji: ' + ', '.join(ALLOWED_MOUNT_PATTERNS))
    return mount


# --- fstab: parsowanie i kontrola ------------------------------------------------------------

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


def _same_dev_source(field, majmin):
    """Wpis fstab ze źródłem /dev/... wskazującym (po dowiązaniach) na urządzenie `majmin`."""
    src = _unescape(field)
    return bool(majmin) and src.startswith('/dev/') and lvm.block_majmin(src) == majmin


def fstab_check(text, mount, uuid=None, majmin=None):
    """'ok' = dokładnie jeden wpis dla `mount` ze źródłem UUID=<uuid> i żaden inny wpis nie wskazuje na
    to urządzenie; 'missing' = brak wpisu dla `mount`, dla UUID i dla urządzenia (/dev/... rozwiązywane
    przez stat → maj:min). Każdy inny stan (inne źródło, duplikaty, to samo urządzenie pod inną
    ścieżką) → ValueError."""
    target = posixpath.normpath(mount)
    rows = list(_fstab_fields(text))
    by_mount = [f for f in rows if posixpath.normpath(_unescape(f[1])) == target]
    by_ref = [f for f in rows if (uuid and _is_uuid_source(f[0], uuid)) or _same_dev_source(f[0], majmin)]
    if not by_mount and not by_ref:
        return 'missing'
    if uuid and len(by_mount) == 1 and by_ref == by_mount and _is_uuid_source(by_mount[0][0], uuid):
        return 'ok'
    found = '; '.join(' '.join(f[:3]) for f in by_mount + [f for f in by_ref if f not in by_mount])
    raise ValueError(f'{FSTAB} zawiera inny lub zdublowany wpis dla {mount}' + (f' / UUID {uuid}' if uuid else '')
                     + f': {found}. Wymagany ręczny przegląd {FSTAB} — program go nie zmieni.')


def fstab_line(uuid, mount):
    return f'UUID={uuid} {mount} ext4 defaults,nofail 0 2\n'


def read_raw(path):
    # newline='' zachowuje oryginalne końce linii (np. CRLF) bajt w bajt.
    with open(runner.host_path(path), encoding='utf-8', errors='surrogateescape', newline='') as f:
        return f.read()


def read_fstab():
    return read_raw(FSTAB)


def fstab_verify_ok():
    return runner.run(['findmnt', '--verify', '--tab-file', FSTAB], check=False).returncode == 0


# --- montowania (findmnt) --------------------------------------------------------------------

def list_mounts():
    """[{'target', 'source', 'fstype', 'size' (B), 'uuid'}] — wszystkie zamontowane systemy plików."""
    p = runner.run(FINDMNT_ALL, check=False)
    if p.returncode and not (p.stdout or '').strip():
        if p.returncode == 1:
            return []
        raise RuntimeError(f'findmnt: {(p.stderr or "").strip() or f"kod {p.returncode}"}')
    data = json.loads(p.stdout or '{}')
    return [{'target': fs.get('target') or '', 'source': fs.get('source') or '', 'fstype': fs.get('fstype') or '',
             'size': lvm._bytes(fs.get('size') or 0), 'uuid': fs.get('uuid') or ''}
            for fs in data.get('filesystems', [])]


def mount_at(target, mounts=None):
    """Ostatnie (widoczne) montowanie w `target` albo None."""
    t = posixpath.normpath(target)
    hits = [m for m in (list_mounts() if mounts is None else mounts) if posixpath.normpath(m['target']) == t]
    return hits[-1] if hits else None


def is_mounted(target):
    return mount_at(target) is not None


def targets_of(device, mounts=None):
    return [m['target'] for m in (list_mounts() if mounts is None else mounts) if lvm.same_device(m['source'], device)]


def is_mountpoint(path):
    return is_mounted(path) or os.path.ismount(runner.host_path(path))


# --- katalog docelowy ------------------------------------------------------------------------

def check_mount_target(mount):
    """Rzuca ValueError, jeśli katalogu nie można bezpiecznie użyć jako nowego punktu montowania."""
    current = '/'
    for part in mount.strip('/').split('/'):
        current = posixpath.join(current, part)
        if os.path.islink(runner.host_path(current)):
            raise ValueError(f'{current} jest dowiązaniem symbolicznym — odmowa.')
        if not os.path.lexists(runner.host_path(current)):
            break
    real = runner.host_path(mount)
    if os.path.lexists(real):
        if not os.path.isdir(real):
            raise ValueError(f'{mount} istnieje i nie jest katalogiem.')
        if is_mountpoint(mount):
            raise ValueError(f'{mount} jest już punktem montowania.')
        if os.listdir(real):
            raise ValueError(f'Katalog {mount} nie jest pusty.')


def missing_dirs(mount):
    """Brakujące katalogi ścieżki, od najwyższego."""
    out, current = [], '/'
    for part in mount.strip('/').split('/'):
        current = posixpath.join(current, part)
        if not os.path.lexists(runner.host_path(current)):
            out.append(current)
    return out


def check_new_mount(raw):
    mount = check_allowed_mount(raw)
    check_mount_target(mount)
    if fstab_has_mount(read_fstab(), mount):
        raise ValueError(f'{mount} jest już wpisany w {FSTAB}.')
    return mount


# --- zapis plików i kroki z wycofaniem ------------------------------------------------------------

def atomic_write(path, text):
    """Atomowa podmiana pliku (tymczasowy plik w tym samym katalogu + os.replace),
    z zachowaniem uprawnień i właściciela oryginału."""
    if runner.DRY_RUN:
        print(f'[DRY-RUN] zapis {path} ({len(text)} znaków)')
        return
    real = runner.host_path(path)
    st = os.stat(real) if os.path.exists(real) else None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real) or '.', prefix=f'.{os.path.basename(real)}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode) if st else 0o644)
        if st and hasattr(os, 'chown'):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, real)
        tmp = None
        try:
            dfd = os.open(os.path.dirname(real) or '.', os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def restore_fstab(backup, original):
    text = read_raw(backup) if os.path.exists(runner.host_path(backup)) else original
    atomic_write(FSTAB, text)
    runner.run(['systemctl', 'daemon-reload'])


def remove_dir(d):
    if runner.DRY_RUN:
        print(f'[DRY-RUN] rmdir {d}')
    else:
        os.rmdir(runner.host_path(d))  # tylko pusty katalog


def make_dirs(dirs, undo):
    for d in dirs:
        if runner.DRY_RUN:
            print(f'[DRY-RUN] mkdir {d}')
        else:
            os.mkdir(runner.host_path(d), 0o755)
        undo(f'usunięcie utworzonego katalogu {d}', f'rmdir {d}', lambda d=d: remove_dir(d))


def backup_name():
    base = f'{FSTAB}.backup-hvm-{datetime.now():%Y%m%d-%H%M%S}'
    name, i = base, 1
    while os.path.lexists(runner.host_path(name)):
        name, i = f'{base}-{i}', i + 1
    return name


def add_fstab_entry(uuid, mount, undo):
    """Kopia + atomowe dopisanie wpisu + daemon-reload + findmnt --verify (z wpisem do undo). Zwraca kopię."""
    original = read_fstab()
    if fstab_has_mount(original, mount) or fstab_has_uuid(original, uuid):
        raise RuntimeError(f'{FSTAB} zmienił się w trakcie pracy — już zawiera {mount} lub UUID {uuid}.')
    backup = backup_name()
    if runner.DRY_RUN:
        print(f'[DRY-RUN] cp -p {FSTAB} {backup}')
    else:
        shutil.copy2(runner.host_path(FSTAB), runner.host_path(backup))
    # wycofanie rejestrowane PRZED zapisem (przywrócenie jest idempotentne) — przerwanie w trakcie
    # atomic_write też zostanie wycofane
    undo(f'przywrócenie {FSTAB} z kopii {backup}', f'cp -p {backup} {FSTAB} && systemctl daemon-reload',
         lambda: restore_fstab(backup, original))
    atomic_write(FSTAB, original + ('' if not original or original.endswith('\n') else '\n') + fstab_line(uuid, mount))
    runner.run(['systemctl', 'daemon-reload'])
    v = runner.run(['findmnt', '--verify', '--tab-file', FSTAB], check=False)
    if v.returncode:
        raise RuntimeError('findmnt --verify zgłasza błąd po zmianie fstab:\n' + (v.stdout + v.stderr).strip())
    return backup


def is_our_mount(entry, device, uuid=None):
    """Montowanie pochodzi z TEGO urządzenia (maj:min / ścieżka), a UUID — jeśli znany — się zgadza."""
    if not entry or not lvm.same_device(entry['source'], device):
        return False
    return not (uuid and entry['uuid'] and entry['uuid'].lower() != uuid.lower())


def verify_mounted(mount, device, uuid):
    entry = mount_at(mount)
    if not is_our_mount(entry, device, uuid):
        raise RuntimeError(f'{mount} nie wskazuje na wolumen {device} (źródło: {entry["source"] if entry else "brak"}).')


def mount_and_verify(mount, device, uuid, undo):
    runner.run(['mount', mount])
    undo(f'odmontowanie {mount}', f'umount {mount}', lambda: runner.run(['umount', mount]))
    if not runner.DRY_RUN:
        verify_mounted(mount, device, uuid)
