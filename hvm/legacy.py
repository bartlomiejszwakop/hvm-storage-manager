"""Polecenia z wersji 0.4 (create / resume / pools / audit) — te same flagi, nowy silnik kroków.

Różnica względem 0.4: nieudane `create` NIE usuwa już automatycznie utworzonego LV (polityka 1.0);
w trybie interaktywnym program proponuje usunięcie po frazie USUN <lv>."""
import os
import subprocess

from hvm import diagnostics as diag, libvirt, lvm, mount as mnt, runner, ui, wizard

CONFIRM_PHRASE = 'TWORZ NOWY WOLUMEN'
GIB = lvm.GIB


def print_plan(info):
    rows = [('Grupa LVM (VG)', info['vg']), ('Nowy wolumen (LV)', info['lvpath']), ('Rozmiar', f'{info["size"]} GiB'),
            ('System plików', f'ext4, etykieta {info["lv"][:16]}, rezerwa root 1% (FORMATOWANIE nowego LV)'),
            ('Punkt montowania', info['mount']),
            (f'Wpis w {mnt.FSTAB}', mnt.fstab_line('<UUID nowego LV>', info['mount']).strip()),
            ('Wolne w VG po operacji', f'{(info["free"] - info["size"] * GIB) / GIB:.1f} GiB')]
    print('\n=== PLAN ===')
    for label, value in rows:
        print(f'  {label:<24} {value}')
    print('W razie błędu kroki niedestrukcyjne zostaną wycofane automatycznie, a stan systemu zweryfikowany. '
          'Utworzony LV nie jest usuwany automatycznie.')


def create_volume(vg, name, size_gib, mount, assume_yes=False, fix_dev=False):
    """create: nowy LV + ext4 + fstab + mount (bez puli libvirt)."""
    try:
        op, info = wizard.plan_new(vg, name, size_gib, mount, None, assume_yes, fix_dev, with_pool=False)
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        ui.err(ui.describe(exc))
        return False
    print_plan(info)
    decision = wizard.confirm(op, [], assume_yes, 'phrase', CONFIRM_PHRASE,
                              f'Aby uruchomić, wpisz dokładnie {CONFIRM_PHRASE}: ')
    if decision != 'run':
        return decision == 'dry'
    t = {'vg': vg, 'lv': name, 'mount': info['mount'], 'pool': None}
    if not wizard.execute(op, t, info['dirs'], assume_yes):
        return False
    ui.ok(f'\nGOTOWE. Nowy magazyn: {info["mount"]}. Kopia fstab: {op.ctx.get("backup")}')
    runner.output(['df', '-h', info['mount']])
    wizard.nofail_warning(info['mount'])
    print(diag.MORPHEUS_HINT)
    return True


def resume_volume(vg, name, mount, assume_yes=False, format_empty=None):
    """resume: idempotentne dokończenie konfiguracji ISTNIEJĄCEGO LV (bez puli libvirt).
    format_empty: przy --yes dokładnie '<vg>/<lv>' — tylko wtedy pusty LV może zostać sformatowany."""
    t = {'vg': vg, 'lv': name, 'mount': mount, 'pool': None}
    return wizard.repair(t, assume_yes, format_empty, with_pool=False, style='yesno')


def pools_command(mount=diag.DEFAULT_MOUNT, refresh=False, assume_yes=False):
    """Wykrywa pule dir na punkcie montowania; odświeża je (virsh pool-refresh) tylko po potwierdzeniu."""
    try:
        mount = mnt.normalize_mount(mount)
    except ValueError as exc:
        ui.err(str(exc))
        return False
    real = runner.host_path(mount)
    if os.path.islink(real) or not os.path.isdir(real):
        ui.err(f'{mount} nie istnieje lub nie jest katalogiem (dowiązania są odrzucane).')
        return False
    if not mnt.is_mounted(mount):
        ui.err(f'STOP: {mount} NIE jest osobnym punktem montowania — ten katalog leży na dysku systemowym.')
        print(f'Pule nie zostaną odświeżone, aby nie pomylić dysku systemowego z magazynem. Sprawdź: findmnt -M {mount}')
        return False
    print('\n=== MONTOWANIE ===')
    runner.output(['findmnt', '-M', mount])
    print('\n=== WOLNE MIEJSCE ===')
    runner.output(['df', '-hT', mount])
    try:
        pools = libvirt.discover_pools(mount)
    except RuntimeError as exc:
        ui.err(str(exc))
        return False
    if not pools:
        print(f'Nie znaleziono puli libvirt typu dir wskazującej na {mount}.')
        print('To polecenie NIE tworzy pul — użyj opcji 1/2 w menu (lub poleceń prepare/repair).')
        print(diag.HOSTS_NOTE)
        return True
    print(f'\n=== PULE LIBVIRT (type=dir, katalog {mount}) ===')
    cols = ('State', 'Capacity', 'Available', 'Autostart')
    print(f'  {"Pula":<20}' + ''.join(f' {c:<14}' for c in cols))
    for pool in pools:
        print(f'  {pool["name"]:<20}' + ''.join(f' {pool["info"].get(c, "?"):<14}' for c in cols))
    print(diag.HOSTS_NOTE)
    if not refresh:
        print('Odświeżenie: uruchom z --refresh.')
        return True
    if assume_yes:
        ui.warn('Potwierdzenie odświeżenia pominięte (--yes).')
    elif not ui.ask_yes_no(f'Odświeżyć wyłącznie wymienione pule ({len(pools)}) przez virsh pool-refresh? [t/N]: '):
        print('Anulowano — pule nie zostały odświeżone.')
        return True
    failed = 0
    for pool in pools:
        try:
            runner.run(libvirt.virsh('pool-refresh', '--pool', libvirt.check_pool_name(pool['name'])))
            ui.ok(f'Odświeżono: {pool["name"]}')
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
            ui.err(f'Odświeżenie {pool["name"]} nieudane: {ui.describe(exc)}')
            failed += 1
            continue
        if not runner.DRY_RUN:
            runner.output(libvirt.virsh('pool-info', '--pool', pool['name']))
    print('W panelu Morpheus odczyt pojemności może wymagać kolejnego cyklu synchronizacji.')
    return failed == 0


def mount_size_bytes(mount):
    entry = mnt.mount_at(mount)
    return entry['size'] if entry and entry['size'] else None


def audit_command(mount=diag.DEFAULT_MOUNT):
    """Audyt lokalnego magazynu na tym hoście — wyłącznie odczyt."""
    try:
        mount = mnt.normalize_mount(mount)
    except ValueError as exc:
        ui.err(str(exc))
        return False
    host = diag.hostname()
    print('\n=== AUDYT LOKALNEGO MAGAZYNU (tylko odczyt) ===')
    print(f'Host: {host}')
    for label, command in [('Punkt montowania', ['findmnt', '-M', mount]), ('Miejsce', ['df', '-hT', mount]),
                           ('Wolumeny LVM', ['lvs', '-o', 'lv_name,vg_name,lv_size,lv_path']),
                           ('Pule libvirt', libvirt.virsh('pool-list', '--all'))]:
        print(f'\n--- {label} ---')
        runner.output(command)
    if not mnt.is_mounted(mount):
        ui.err(f'STOP: {mount} NIE jest osobnym punktem montowania — katalog leży na dysku systemowym.')
        print(f'Proponowana nazwa (bez rozmiaru): {libvirt.try_suggest(host) or "podaj ją ręcznie (--pool-name)"}')
        return False
    size = mount_size_bytes(mount)
    suggestion = libvirt.try_suggest(host, round(size / GIB) if size else None)
    ui.ok(f'\nProponowana nazwa lokalnego magazynu: {suggestion or "brak (nazwa hosta nic nie mówi) — podaj ją ręcznie"}')
    try:
        pools = libvirt.discover_pools(mount)
    except RuntimeError as exc:
        ui.err(str(exc))
        return False
    short = host.split('.')[0].lower()
    tags = {short, libvirt.HOST_ALIASES.get(short, short).lower()}
    print(f'\nPule libvirt typu dir wskazujące dokładnie {mount}:')
    for pool in pools:
        unique = any(t and t in pool['name'].lower() for t in tags)
        print(f'  - {pool["name"]}' + ('' if unique else
                                       '   ← nazwa nie zawiera nazwy hosta; na drugim hoście może być identyczna'))
    if not pools:
        print('  (brak) — pulę można zarejestrować opcją 2 w menu (lub poleceniem repair).')
    print('\n' + diag.MORPHEUS_NOTE)
    print('Nie usuwaj obecnej puli ani jej plików — może być używana przez VM.')
    print(diag.HOSTS_NOTE)
    print('Nie zmieniono żadnych zasobów.')
    return True


def create_store():
    """Interaktywne create z 0.4 (menu Zaawansowane): VG, nazwa, rozmiar, punkt montowania."""
    print('\nTworzenie NOWEGO wolumenu LVM (bez puli libvirt). Enter = wartość domyślna, q = powrót.')
    try:
        vgs = lvm.list_vgs()
        if not vgs:
            ui.err('Nie znaleziono żadnej grupy LVM (VG).')
            return False
        for i, v in enumerate(vgs, 1):
            print(f'  {i}) {v["vg_name"]} — rozmiar {v["vg_size"] / GIB:.1f} GiB, wolne {v["vg_free"] / GIB:.1f} GiB')

        def pick(raw):
            if raw.isdigit() and 1 <= int(raw) <= len(vgs):
                return vgs[int(raw) - 1]
            match = [v for v in vgs if v['vg_name'] == raw]
            if not match:
                raise ValueError(f'Nie ma grupy {raw!r}. Podaj numer 1–{len(vgs)} lub nazwę.')
            return match[0]
        vg = ui.ask('Wybierz grupę (numer lub nazwa)', vgs[0]['vg_name'] if len(vgs) == 1 else None, pick)

        def check_name(s):
            if not lvm.verify_name(s):
                raise ValueError(f'Niedozwolona nazwa wolumenu {s!r} ({lvm.NAME_HELP}).')
            if lvm.lv_exists(vg['vg_name'], s):
                raise ValueError(f'Wolumen /dev/{vg["vg_name"]}/{s} już istnieje — użyj „dokończ konfigurację”.')
            return s
        name = ui.ask('Nazwa wolumenu', diag.DEFAULT_LV, check_name)
        max_size = lvm.max_size_gib(vg['vg_free'])
        if max_size < 1:
            ui.err(f'Brak miejsca w {vg["vg_name"]}.')
            return False
        size = ui.ask(f'Rozmiar w GiB (maks. {max_size})', max_size, lambda s: wizard.resolve_size(s, max_size))
        default_mount = f'/{name}' if mnt.mount_allowed(f'/{name}') else diag.DEFAULT_MOUNT
        mount = ui.ask('Punkt montowania', default_mount, mnt.check_new_mount)
    except ui.Cancelled:
        print('\nAnulowano — powrót do menu.')
        return False
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        ui.err(ui.describe(exc))
        return False
    return create_volume(vg['vg_name'], name, size, mount)
