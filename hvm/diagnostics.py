"""Diagnostyka: model stanu magazynu (wolumen Linux / pula libvirt / Morpheus), raport tekstowy
i JSON, status do nagłówka menu oraz porównanie stanu po nieudanej operacji. Wyłącznie odczyt."""
import hashlib
import json
import os
import socket
from datetime import datetime

from hvm import __version__, disks, filesystem as fs, libvirt, lvm, morpheus, mount as mnt, runner, ui

DEFAULT_VG = 'ubuntu-vg'
DEFAULT_LV = 'vm-storage'
DEFAULT_MOUNT = '/vm-storage'
OS_RELEASE = '/etc/os-release'
GIB = lvm.GIB
REPORT_DIR = '/var/log'
MORPHEUS_NOTE = ('Pula libvirt to zasób lokalnego hosta; Datastore w panelu Morpheus to osobna rejestracja. '
                 'Program nie łączy się z Morpheus API, więc nie potwierdza widoczności magazynu w panelu.')
MORPHEUS_HINT = ('Rejestracja w Morpheus: Infrastructure → Clusters → Storage → Datastores → ADD → Directory Pool '
                 '(ścieżka = punkt montowania).')
HOSTS_NOTE = ('UWAGA: każdy host HVM ma własny, LOKALNY dysk — magazyny o tej samej ścieżce na dwóch hostach '
              'to nie jest współdzielony datastore.')
D_MESSAGE = 'Magazyn jest już poprawnie skonfigurowany.\nNie trzeba wykonywać dodatkowych operacji.'


def gib(b):
    return int(b // GIB)


def _safe(errors, label, fn, default=None):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — diagnostyka zbiera błędy, nie przerywa
        errors.append(f'{label}: {ui.describe(exc)}')
        return default


# --- host ------------------------------------------------------------------------------------

def hostname():
    try:
        return socket.gethostname()
    except OSError:
        return ''


def primary_ip():
    p = runner.run(['ip', '-j', '-4', 'addr', 'show'], check=False)
    if p.returncode == 0:
        try:
            for iface in json.loads(p.stdout or '[]'):
                if iface.get('ifname') == 'lo':
                    continue
                for a in iface.get('addr_info', []):
                    if a.get('scope') == 'global' and a.get('local'):
                        return a['local']
        except (ValueError, AttributeError, TypeError):
            pass
    p = runner.run(['hostname', '-I'], check=False)
    return next((t for t in p.stdout.split() if not t.startswith('127.') and ':' not in t), '?')


def os_name():
    try:
        text = mnt.read_raw(OS_RELEASE)
    except OSError:
        return '?'
    for line in text.splitlines():
        if line.startswith('PRETTY_NAME='):
            return line.split('=', 1)[1].strip().strip('"\'')
    return '?'


def host_info():
    host = hostname()
    return {'hostname': host, 'label': libvirt.host_label(host), 'ip': primary_ip(), 'os': os_name()}


# --- lokalizacja i inspekcja magazynu ------------------------------------------------------------

def default_vg(names):
    if DEFAULT_VG in names:
        return DEFAULT_VG
    return names[0] if len(names) == 1 else None


def locate_target(vg=None, lv=None, mount=None, pool=None):
    """Magazyn do sprawdzenia: LV zamontowany w `mount`, jedyny LV o nazwie `lv`, albo wartości domyślne."""
    mount = mnt.normalize_mount(mount or DEFAULT_MOUNT)
    lvs = _safe([], 'lvs', lvm.list_lvs, [])
    if not (vg and lv):
        entry = _safe([], 'findmnt', lambda: mnt.mount_at(mount))
        found = None
        if entry:
            found = next((x for x in lvs if lvm.same_device(entry['source'], x['dm_path'])
                          or lvm.same_device(entry['source'], x['path'])), None)
        if not found:
            named = [x for x in lvs if x['name'] == (lv or DEFAULT_LV) and (not vg or x['vg'] == vg)]
            found = named[0] if len(named) == 1 else None
        if found:
            vg, lv = found['vg'], found['name']
    if not vg:
        vg = default_vg([v['vg_name'] for v in _safe([], 'vgs', lvm.list_vgs, [])]) or DEFAULT_VG
    lv = lv or DEFAULT_LV
    if pool:
        libvirt.check_pool_name(pool)  # ValueError — nazwa z --pool-name trafia do virsh tylko po walidacji
    else:
        pools = _safe([], 'virsh', lambda: libvirt.list_pools() if libvirt.available() else [], [])
        same = [p for p in pools if p['type'] == 'dir' and not p['protected'] and libvirt.same_dir(p['target'], mount)
                and libvirt.POOL_NAME_RE.fullmatch(p['name'])]
        info = lvm.find_lv(vg, lv, lvs)
        pool = same[0]['name'] if same else (libvirt.try_suggest(hostname(), gib(info['size'])) if info else None)
    return {'vg': vg, 'lv': lv, 'mount': mount, 'pool': pool}


def inspect_storage(t, with_pool=True):
    """Pełny stan magazynu `t` (tylko odczyt). blockers = powody odmowy zmian; errors = brak danych."""
    vg, name, mount = t['vg'], t['lv'], t['mount']
    s = {'target': t, 'lv': None, 'dev': None, 'conflicts': [], 'sig': None, 'sigdesc': '', 'uuid': None,
         'mount': None, 'mounted_ours': False, 'elsewhere': [], 'fstab': None, 'dirs': [], 'libvirt': None,
         'pools': [], 'decision': None, 'vms': [], 'blockers': [], 'errors': []}
    errors, blockers = s['errors'], s['blockers']
    lvs = _safe(errors, 'LVM (lvs)', lvm.list_lvs)
    mounts = _safe(errors, 'montowania (findmnt)', mnt.list_mounts, [])
    lv = s['lv'] = lvm.find_lv(vg, name, lvs) if lvs is not None else None
    device = None
    if lvs is not None and not lv:
        s['conflicts'] = lvm.conflict_findings(vg, name)
    elif lv:
        s['dev'] = dev = lvm.device_check(vg, name, lv)
        device = dev['device']
        if lv['attr'][4] != 'a':
            blockers.append(f'LV /dev/{vg}/{name} nie jest aktywny (lv_attr {lv["attr"]}). '
                            f'Aktywuj ręcznie: lvchange -ay {vg}/{name}')
        elif not device:
            blockers.append(dev['detail'])
        else:
            sig = _safe(errors, 'sygnatury', lambda: fs.detect_signature(device))
            if sig:
                s['sig'], s['sigdesc'] = sig
                if sig[0] == 'ext4':
                    s['uuid'] = _safe(errors, 'UUID', lambda: fs.read_uuid(device))
                    dups = fs.devices_with_uuid(s['uuid']) if s['uuid'] else []
                    if len(dups) > 1:
                        blockers.append(f'UUID {s["uuid"]} ma {len(dups)} urządzenia ({", ".join(dups)}) — '
                                        'migawka lub klon LV. Montowanie po UUID byłoby niejednoznaczne — odmowa.')
                elif sig[0] == 'other':
                    blockers.append(f'Na {device} wykryto: {sig[1]}. To nie jest pusty LV ani ext4 — odmowa '
                                    '(program nie formatuje i nie montuje takich wolumenów).')
            s['elsewhere'] = [x for x in mnt.targets_of(device, mounts) if x != mount]
            if s['elsewhere']:
                blockers.append(f'/dev/{vg}/{name} jest zamontowany w innym miejscu: {", ".join(s["elsewhere"])} — odmowa.')
    if lv and device:
        s['vms'] = libvirt.vm_usage(mount, [device, lvm.dev_paths(vg, name)[0], lv['dm_path'], lv['path']])
        if s['sig'] == 'empty' and s['vms']:
            blockers.append(f'Wolumen jest dyskiem maszyny {s["vms"][0][0]} ({s["vms"][0][1]}) — odmowa formatowania.')
        elif s['sig'] == 'empty' and s['vms'] is None:
            blockers.append('Nie można sprawdzić, czy wolumen jest dyskiem którejś maszyny (libvirt nie odpowiada) '
                            '— odmowa formatowania istniejącego LV.')
    entry = s['mount'] = mnt.mount_at(mount, mounts)
    if entry:
        ours = bool(lv and device and mnt.is_our_mount(entry, device, s['uuid']))
        s['mounted_ours'] = ours
        if lv and not ours:
            blockers.append(f'Pod {mount} jest zamontowane INNE urządzenie ({entry["source"]}) — odmowa.')
    elif lv:
        if lv['attr'][5] == 'o' and not s['elsewhere']:
            blockers.append(f'LV /dev/{vg}/{name} jest otwarty (lv_attr {lv["attr"]}) przez inny proces lub VM, '
                            f'a nie jest zamontowany w {mount} — odmowa.')
        s['dirs'] = mnt.missing_dirs(mount)
        try:
            mnt.check_mount_target(mount)
        except ValueError as exc:
            blockers.append(str(exc))
    try:
        s['fstab'] = mnt.fstab_check(mnt.read_fstab(), mount, s['uuid'], lv['majmin'] if lv else None)
    except ValueError as exc:
        s['fstab'] = 'conflict'
        if lv:
            blockers.append(str(exc))
    except OSError as exc:
        errors.append(f'{mnt.FSTAB}: {exc}')
    if with_pool:
        s['libvirt'] = _safe(errors, 'libvirt', libvirt.available, False)
        if s['libvirt']:
            s['pools'] = _safe(errors, 'pule libvirt', libvirt.list_pools, [])
            if t.get('pool'):
                s['decision'] = libvirt.pool_decision(t['pool'], mount, s['pools'])
    return s


# --- trzy niezależne stany ---------------------------------------------------------------------

def linux_state(s):
    t, lv = s['target'], s['lv']
    if not lv:
        if s['errors']:
            return 'unknown', 'brak danych LVM: ' + s['errors'][0]
        return 'error', f'wolumen {t["vg"]}/{t["lv"]} nie istnieje'
    if s['blockers']:
        return 'error', s['blockers'][0]
    missing = [txt for cond, txt in ((s['sig'] != 'ext4', 'brak systemu plików ext4'),
                                     (not s['mounted_ours'], f'niezamontowany w {t["mount"]}'),
                                     (s['fstab'] != 'ok', f'brak wpisu w {mnt.FSTAB}')) if cond]
    if missing:
        return 'error', '; '.join(missing)
    return 'ok', f'{gib(lv["size"])} GiB, zamontowany w {t["mount"]}'


def matched_pool(s):
    kind, val = s['decision'] or (None, None)
    if kind in ('existing', 'reuse'):
        return val
    same = [p for p in s['pools'] if p['type'] == 'dir' and libvirt.same_dir(p['target'], s['target']['mount'])]
    return same[0] if same else None


def pool_state(s):
    if s['libvirt'] is None:
        return 'unknown', 'nie sprawdzano'
    if not s['libvirt']:
        return 'unknown', 'libvirt nie odpowiada (virsh uri)'
    pool = matched_pool(s)
    if pool:
        if pool['state'] != 'running':
            return 'error', f'pula {pool["name"]} nieaktywna'
        if pool['autostart'] != 'yes':
            return 'error', f'pula {pool["name"]} bez autostartu'
        return 'ok', f'pula {pool["name"]} aktywna'
    kind, val = s['decision'] or (None, None)
    if kind == 'refuse':
        return 'error', val
    return 'error', f'brak puli libvirt dla {s["target"]["mount"]}'


def storage_entry(s):
    t, lv = s['target'], s['lv']
    pool = matched_pool(s) if s['libvirt'] else None
    name = (pool or {}).get('name') or t.get('pool') or libvirt.try_suggest(hostname()) or f'{t["vg"]}/{t["lv"]}'
    lstate, ldetail = linux_state(s)
    pstate, pdetail = pool_state(s)
    return {'name': name, 'vg': t['vg'], 'lv': t['lv'], 'mount': t['mount'], 'size_bytes': lv['size'] if lv else None,
            'linux': {'state': lstate, 'detail': ldetail},
            'libvirt': {'state': pstate, 'detail': pdetail, 'pool': pool['name'] if pool else None,
                        'running': bool(pool and pool['state'] == 'running')},
            'morpheus': morpheus.registration_state(name, t['mount']),
            'vms': [{'vm': d, 'source': src} for d, src in (s['vms'] or [])]}


def pool_entry(p, mounts):
    """Magazyn znany tylko z puli libvirt (inny katalog niż domyślny)."""
    entry = mnt.mount_at(p['target'], mounts) if p['target'] else None
    linux = ({'state': 'ok', 'detail': f'osobny system plików ({gib(entry["size"])} GiB)'} if entry else
             {'state': 'error', 'detail': f'{p["target"]} nie jest osobnym punktem montowania'})
    ok_ = p['state'] == 'running' and p['autostart'] == 'yes'
    return {'name': p['name'], 'vg': None, 'lv': None, 'mount': p['target'], 'size_bytes': entry['size'] if entry else None,
            'linux': linux,
            'libvirt': {'state': 'ok' if ok_ else 'error', 'pool': p['name'], 'running': p['state'] == 'running',
                        'detail': f'pula {p["name"]} aktywna' if ok_ else f'pula {p["name"]}: stan {p["state"]}, '
                                                                          f'autostart {p["autostart"]}'},
            'morpheus': morpheus.registration_state(p['name'], p['target']), 'vms': []}


def storage_entries(s):
    out = [storage_entry(s)]
    mounts = _safe([], 'findmnt', mnt.list_mounts, [])
    for p in s['pools']:
        if p['type'] == 'dir' and not p['protected'] and not libvirt.same_dir(p['target'], s['target']['mount']):
            out.append(pool_entry(p, mounts))
    return out


def print_storage(e):
    print(f'\n{e["name"]}')
    print(ui.state_line(e['linux']['state'], 'Wolumen Linux gotowy' if e['linux']['state'] == 'ok'
                        else f'Wolumen Linux: {e["linux"]["detail"]}'))
    print(ui.state_line(e['libvirt']['state'], 'Pula libvirt aktywna' if e['libvirt']['state'] == 'ok'
                        else f'Pula libvirt: {e["libvirt"]["detail"]}'))
    print(ui.state_line(e['morpheus']['state'], e['morpheus']['detail']))
    for vm in e['vms']:
        print(f'  Informacja: używany przez VM {vm["vm"]} ({vm["source"]}) — dane VM nie są zmieniane.')


# --- nagłówek menu i raport ------------------------------------------------------------------------

def header_status():
    rows = []
    scan = _safe([], 'dyski', disks.scan)
    sysd = [d for d in scan['disks'] if d['status'] == 'system'] if scan else []
    rows.append(('ok', 'Dysk systemowy: ' + ', '.join(f'{d["name"]} {gib(d["size"])} GiB' for d in sysd)) if sysd
                else ('unknown', 'Dysk systemowy: nie rozpoznano'))
    t = locate_target()
    s = inspect_storage(t)
    lv = s['lv']
    if lv:
        rows.append(('ok', f'Magazyn VM: {gib(lv["size"])} GiB ({t["vg"]}/{t["lv"]})'))
    elif s['errors']:
        rows.append(('unknown', f'Magazyn VM: brak danych ({t["mount"]})'))
    else:
        rows.append(('error', f'Magazyn VM: nie utworzono ({t["mount"]})'))
    rows.append(('ok', f'Zamontowany: {t["mount"]}') if s['mounted_ours'] else ('error', f'Niezamontowany: {t["mount"]}'))
    state, detail = pool_state(s)
    rows.append((state, f'Dostępny w libvirt: {"tak, " + detail if state == "ok" else detail}'))
    return rows


def collect_report():
    errors = []
    rep = {'tool': 'hvm-storage-manager', 'version': __version__,
           'generated_at': datetime.now().isoformat(timespec='seconds'), 'root': runner.is_root(),
           'host': host_info(), 'warnings': []}
    if not rep['root']:
        rep['warnings'].append('Uruchomiono bez uprawnień root — część informacji (LVM, blkid) może być niedostępna.')
    sources = _safe(errors, 'VM (virsh)', libvirt.vm_sources)
    rep['disks'] = _safe(errors, 'dyski', lambda: disks.scan(sources or []))
    s = inspect_storage(locate_target())
    rep['storages'] = storage_entries(s)
    rep['pools'] = [{k: p[k] for k in ('name', 'type', 'target', 'state', 'autostart', 'capacity', 'available',
                                        'protected')} for p in s['pools']]
    verify = _safe(errors, 'findmnt --verify', mnt.fstab_verify_ok)
    rep['fstab_verify'] = 'unknown' if verify is None else ('ok' if verify else 'error')
    rep['errors'] = errors + s['errors']
    return rep


def print_report(rep):
    h = rep['host']
    print(f'\nRAPORT STANU SERWERA — {rep["generated_at"]} (wersja {rep["version"]})')
    print(f'Serwer: {h["hostname"]} ({h["label"]})\nAdres IP: {h["ip"]}\nSystem: {h["os"]}')
    for w in rep['warnings']:
        ui.warn(w)
    if rep['disks']:
        disks.print_scan(rep['disks'])
    print('\nMAGAZYNY')
    for e in rep['storages']:
        print_storage(e)
    print('\nPULE LIBVIRT')
    for p in rep['pools']:
        print(f'  {p["name"]:<24} {p["type"]:<8} {p["state"]:<10} autostart {p["autostart"]:<4} {p["target"]}'
              + ('  (chroniona)' if p['protected'] else ''))
    if not rep['pools']:
        print('  (brak danych)')
    print(ui.state_line(rep['fstab_verify'], f'{mnt.FSTAB}: findmnt --verify'
                        + {'ok': ' bez błędów', 'error': ' zgłasza błędy', 'unknown': ' — nie sprawdzono'}[rep['fstab_verify']]))
    for e in rep['errors']:
        ui.warn(f'brak danych — {e}')
    print('\n' + MORPHEUS_NOTE)


def export_report(path=None):
    """Zapisuje raport JSON (domyślnie /var/log/..., w razie braku dostępu — bieżący katalog)."""
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    name = f'hvm-storage-manager-report-{ts}.json'
    paths = [path] if path else [f'{REPORT_DIR}/{name}', os.path.join(os.getcwd(), name)]
    text = json.dumps(collect_report(), ensure_ascii=False, indent=2) + '\n'
    if runner.DRY_RUN:
        ui.warn('tryb podglądu (dry-run) — raport NIE jest zapisywany do pliku; wypisuję go poniżej.')
        print(text)
        return None
    last = None
    for p in paths:
        try:
            with open(runner.host_path(p), 'x', encoding='utf-8') as f:
                f.write(text)
            ui.ok(f'Zapisano raport: {p}')
            return p
        except OSError as exc:
            last = exc
    ui.err(f'Nie udało się zapisać raportu: {last}')
    return None


# --- stan przed/po operacji ---------------------------------------------------------------------

def snapshot(t, dirs=()):
    vg, name, mount = t['vg'], t['lv'], t['mount']
    text = mnt.read_fstab()
    lv = lvm.find_lv(vg, name)
    s = {'lv': lv is not None, 'dev': lvm.path_state(lvm.dev_paths(vg, name)[0]), 'mounted': mnt.is_mounted(mount),
         'fstab': hashlib.sha256(text.encode('utf-8', 'surrogateescape')).hexdigest(),
         'dirs': tuple(d for d in dirs if os.path.lexists(runner.host_path(d))),
         'fs': fs.blkid_export(lvm.dev_paths(vg, name)[1])[1].get('TYPE', 'brak') if lv else 'brak'}
    if t.get('pool'):
        names = _safe([], 'virsh', libvirt.pool_names)
        s['pool'] = None if names is None else t['pool'] in names
    return s


def diagnose_after(t, before, dirs, op):
    """Porównuje stan po nieudanej operacji (i wycofaniu) ze stanem sprzed niej. True = zgodny."""
    print('\n=== WERYFIKACJA STANU ===')
    vg, name, mount = t['vg'], t['lv'], t['mount']
    lvpath = lvm.dev_paths(vg, name)[0]
    runner.run(['udevadm', 'settle', '--timeout=30'], check=False)
    try:
        after, text = snapshot(t, dirs), mnt.read_fstab()
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        ui.err(f'Nie udało się zweryfikować stanu systemu: {ui.describe(exc)}')
        print(f'Sprawdź ręcznie: lvs {vg}; ls -la {lvpath}; findmnt -M {mount}; grep {mount} {mnt.FSTAB}')
        return False
    yn = lambda b: 'tak' if b else ('nie' if b is not None else '?')  # noqa: E731
    uuid = op.ctx.get('uuid') if fs.UUID_RE.fullmatch(op.ctx.get('uuid') or '') else None
    fstab_desc = (('bez zmian' if after['fstab'] == before['fstab'] else 'ZMIENIONY')
                  + f'; wpis dla {mount}: {yn(mnt.fstab_has_mount(text, mount))}'
                  + (f'; wpis z UUID {uuid}: {yn(mnt.fstab_has_uuid(text, uuid))}' if uuid else ''))
    rows = [('lv', f'LV {lvpath} w LVM', yn(after['lv'])), ('dev', f'Ścieżka {lvpath}', after['dev']),
            ('fs', 'System plików na LV', after['fs']), ('mounted', f'{mount} zamontowany', yn(after['mounted'])),
            ('fstab', mnt.FSTAB, fstab_desc), ('dirs', 'Katalogi utworzone przez program', ', '.join(after['dirs']) or 'brak')]
    if 'pool' in after:
        rows.append(('pool', f'Pula libvirt {t["pool"]}', yn(after['pool'])))
    diff = [key for key, _, _ in rows if before.get(key) != after.get(key)]
    for key, label, value in rows:
        print(f'  {label:<44} {value}' + ('   ← INACZEJ niż przed operacją' if key in diff else ''))
    if not diff and op.ctx.get('formatted'):
        ui.warn('w tym uruchomieniu wykonano mkfs.ext4 — tego nie da się cofnąć; stan NIE jest identyczny '
                'z wyjściowym (nowy system plików na pustym wcześniej wolumenie).')
        return False
    if not diff:
        ui.ok('Wszystkie zmiany wycofane — stan zweryfikowany.' if op.changed
              else 'Nie wprowadzono żadnych zmian — stan zweryfikowany.')
        return True
    ui.warn('stan systemu RÓŻNI SIĘ od stanu sprzed operacji.')
    manual = []
    if 'mounted' in diff:
        manual.append((f'{mount} jest zamontowany', f'umount {mount}') if after['mounted']
                      else (f'{mount} był zamontowany przed operacją', f'mount {mount}'))
    if 'fstab' in diff:
        bk = op.ctx.get('backup')
        manual.append((f'{mnt.FSTAB} różni się od stanu sprzed operacji',
                       f'cp -p {bk} {mnt.FSTAB} && systemctl daemon-reload' if bk
                       else f'przejrzyj {mnt.FSTAB} ręcznie, potem: findmnt --verify'))
    for d in reversed(after['dirs']):
        if d not in before['dirs']:
            manual.append((f'katalog {d} utworzony przez program', f'rmdir {d}'))
    if 'pool' in diff and after.get('pool'):
        manual.append((f'pula {t["pool"]} zdefiniowana w tym uruchomieniu (pliki nietknięte)',
                       f'virsh pool-destroy {t["pool"]}; virsh pool-undefine {t["pool"]}'))
    if 'lv' in diff and after['lv']:
        manual.append(('LV utworzony w tym uruchomieniu nadal istnieje — nie jest usuwany automatycznie; '
                       'dokończ konfigurację (opcja 2) albo usuń go ręcznie', f'lvremove {lvpath}'))
    if 'dev' in diff:
        manual.append((f'ścieżka była „{before["dev"]}”, jest „{after["dev"]}”', f'ls -la {lvpath}'))
    if 'fs' in diff and not ('lv' in diff and after['lv']):
        manual.append((f'na LV pozostał system plików {after["fs"]} utworzony w tym uruchomieniu (mkfs nie da się '
                       'cofnąć; LV był pusty) — można dokończyć konfigurację (opcja 2)', f'blkid -p {lvpath}'))
    print('Do sprawdzenia / naprawy:')
    for desc, cmd in manual:
        print(f'  {cmd:<60} # {desc}')
    return False
