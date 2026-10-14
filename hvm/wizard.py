"""Kreator nowego magazynu (menu 1 / prepare), naprawa i dokończenie konfiguracji (menu 2 / repair)
oraz sprawdzenie magazynów (menu 4). Przebieg zawsze: diagnoza → plan → potwierdzenie → wykonanie."""
import shlex
import subprocess

from hvm import diagnostics as diag, disks, filesystem as fs, libvirt, lvm, mount as mnt, runner, safety, ui

GIB = lvm.GIB
DEFAULT_VG, DEFAULT_LV, DEFAULT_MOUNT = diag.DEFAULT_VG, diag.DEFAULT_LV, diag.DEFAULT_MOUNT
RECOMMENDED_GIB = 1200
FIXED_SIZES = (1000, 1200)
CAPACITY_MIN_RATIO = 0.9  # system plików ext4 ≥ 90% LV (narzut metadanych ext4 to ok. 1–2%)
NEXT_STEP = ('Co dalej: uruchom program ponownie i wybierz opcję 2 („Napraw lub dokończ konfigurację”) '
             '— albo polecenie: hvm-storage-manager repair.')


def nofail_warning(mount):
    ui.warn(f'wpis w fstab ma opcję nofail — system uruchomi się także BEZ tego magazynu. '
            f'Przed startem VM sprawdź: findmnt -M {mount}')


# --- rozmiar ------------------------------------------------------------------------------------

def _round_down(n):
    for step in (100, 10):
        if n >= 2 * step:
            return n // step * step
    return n


def size_options(max_gib):
    """([(GiB, etykieta)], zalecane). Zalecane = 1200 GiB, jeśli się mieści, inaczej okrągła wartość poniżej maks.
    Opcje, które się nie mieszczą, nie są pokazywane."""
    if max_gib < 1:
        return [], None
    rec = RECOMMENDED_GIB if max_gib >= RECOMMENDED_GIB else (_round_down(max_gib - 1) if max_gib > 10 else max_gib)
    sizes = sorted({s for s in FIXED_SIZES if s <= max_gib} | {rec})
    opts = [(s, f'{s} GiB' + (' — zalecane' + (' (cała dostępna przestrzeń)' if s == max_gib else '') if s == rec else ''))
            for s in sizes]
    if max_gib not in sizes:
        opts.append((max_gib, f'Maksymalna dostępna przestrzeń ({max_gib} GiB)'))
    return opts, rec


def resolve_size(spec, max_gib):
    spec = str(spec).strip().lower()
    if max_gib < 1:
        raise ValueError('Za mało wolnego miejsca w grupie LVM.')
    if spec in ('max', 'maks'):
        return max_gib
    if spec in ('recommended', 'zalecane'):
        return size_options(max_gib)[1]
    size = lvm.parse_size(spec)
    if size > max_gib:
        raise ValueError(f'Za dużo — maksymalnie {max_gib} GiB (reszta zostaje jako rezerwa LVM).')
    return size


# --- walidacja i konflikty -------------------------------------------------------------------------

def approve_conflicts(findings, vg, name, assume_yes, fix_dev):
    """Ścieżki pustych katalogów do usunięcia (po zgodzie) albo ValueError z diagnozą."""
    paths = []
    for f in findings:
        if not f['fixable']:
            raise ValueError(f['reason'])
        print(f'\nWykryto PUSTY katalog {f["path"]} w miejscu ścieżki urządzenia LV {vg}/{name}.\n'
              'To typowa przyczyna błędu lvcreate „device not cleared”: udev/LVM nie może tu utworzyć dowiązania.\n'
              f'Katalog jest pusty, nie jest punktem montowania i nie używa go żaden proces.')
        if fix_dev:
            ui.warn(f'usunę pusty katalog {f["path"]} (zgoda: --fix-dev-conflict).')
        elif assume_yes:
            raise ValueError(f'tryb --yes nie usuwa {f["path"]} automatycznie. Uruchom polecenie interaktywnie '
                             '(bez --yes) albo dodaj --fix-dev-conflict.')
        elif not ui.ask_yes_no('Usunąć wyłącznie ten pusty katalog (rmdir)? [t/N]: '):
            raise ValueError(f'Nie usunięto {f["path"]} — konfiguracja nie jest możliwa.')
        paths.append(f['path'])
    return paths


def check_protected(mount):
    """Odmowa, gdy punkt montowania pokrywa się z katalogiem chronionej puli (gdy libvirt odpowiada)."""
    if not libvirt.available():
        return
    p = libvirt.protected_overlap(mount, libvirt.list_pools())
    if p:
        raise ValueError(f'{mount} pokrywa się z katalogiem chronionej puli {p["name"]} ({p["target"]}) — odmowa.')


def validate_new(vg, name, size, mount):
    """Pełna walidacja NOWEGO wolumenu przed zmianami. Zwraca info (dict) albo rzuca ValueError."""
    if not lvm.verify_vg_name(vg):
        raise ValueError(f'Niedozwolona nazwa grupy LVM {vg!r}.')
    if not lvm.verify_name(name):
        raise ValueError(f'Niedozwolona nazwa wolumenu {name!r} ({lvm.NAME_HELP}).')
    size = lvm.parse_size(size)
    mount = mnt.check_new_mount(mount)
    vgs = {v['vg_name']: v for v in lvm.list_vgs()}
    if vg not in vgs:
        raise ValueError(f'Grupa LVM {vg} nie istnieje. Dostępne: {", ".join(vgs) or "brak"}.')
    if lvm.lv_exists(vg, name):
        raise ValueError(f'Wolumen /dev/{vg}/{name} już istnieje — nie zostanie utworzony ponownie ani sformatowany '
                         '(aby dokończyć jego konfigurację, użyj opcji 2 / repair / resume).')
    free = vgs[vg]['vg_free']
    if not lvm.size_ok(size, free):
        raise ValueError(f'Za mało miejsca w {vg}: wolne {free / GIB:.1f} GiB, potrzeba {size} GiB + '
                         f'{lvm.reserve_for(free)} GiB rezerwy (maksymalnie {lvm.max_size_gib(free)} GiB).')
    if not mnt.fstab_verify_ok():
        raise ValueError(f'findmnt --verify zgłasza błędy w obecnym {mnt.FSTAB} — napraw je przed dodaniem nowego wpisu.')
    check_protected(mount)
    return {'vg': vg, 'lv': name, 'size': size, 'mount': mount, 'free': free, 'dirs': mnt.missing_dirs(mount),
            'lvpath': f'/dev/{vg}/{name}'}


# --- kroki ------------------------------------------------------------------------------------------

def _rmdir_step(op, path):
    op.add(f'Usunięcie pustego katalogu {path} (blokuje ścieżkę urządzenia)',
           lambda: lvm.remove_conflict_dir(path), [f'rmdir {path}'])


def lvcreate_args(vg, name, size):
    # --yes: bez pytań (stdin = /dev/null); -Wy -Zy: LVM czyści sygnatury i zeruje początek WYŁĄCZNIE
    # nowego LV (jego własnych, właśnie przydzielonych ekstentów) — nie dotyka innych wolumenów.
    return ['lvcreate', '--yes', '-Wy', '-Zy', '-L', f'{size}G', '-n', name, vg]


def _add_create_steps(op, info):
    vg, name, size = info['vg'], info['lv'], info['size']
    lvpath, mapper = lvm.dev_paths(vg, name)
    cmd = lvcreate_args(vg, name, size)

    def create():
        if lvm.lv_exists(vg, name):
            raise RuntimeError(f'Wolumen {lvpath} pojawił się w trakcie pracy — przerwano, nie jest nasz.')
        blocking = lvm.conflict_findings(vg, name)
        if blocking:
            raise RuntimeError(f'{blocking[0]["path"]} blokuje utworzenie LV ({blocking[0]["state"]}) — '
                               'przerwano. Uruchom naprawę (opcja 2).')
        ok = False
        try:
            runner.run(cmd)
            ok = True
        finally:  # także przy Ctrl+C: zapamiętaj, że LV powstał w TYM uruchomieniu
            try:
                exists = lvm.lv_exists(vg, name)
            except Exception as exc:  # noqa: BLE001 — diagnoza nie może zasłonić pierwotnego błędu
                exists = None
                ui.warn(f'nie udało się sprawdzić, czy LV powstał: {ui.describe(exc)}')
            if exists:
                op.ctx['lv_created'] = True
                if not ok:
                    ui.warn(f'lvcreate zgłosił błąd, ale LV {lvpath} istnieje — utworzony częściowo przez TO '
                            'uruchomienie. Nie zostanie usunięty automatycznie.')

    def check_empty():
        sig, desc = fs.detect_signature(op.ctx['device'])
        if sig != 'empty':
            raise RuntimeError(f'Na nowym LV wykryto sygnaturę ({desc}) — przerwano przed formatowaniem.')

    op.add(f'Utworzenie wolumenu LVM {vg}/{name} ({size} GiB)', create, [shlex.join(cmd)])
    op.add('Oczekiwanie na urządzenie nowego wolumenu (udev)', lambda: op.ctx.update(device=lvm.wait_device(vg, name)),
           ['udevadm settle --timeout=30'])
    op.add('Kontrola: nowy wolumen jest pusty (bez sygnatur)', check_empty,
           [f'wipefs -n --noheadings --output TYPE {mapper}', f'blkid -p -o export {mapper}'])
    _add_format_step(op, vg, name, mapper)
    _add_linux_tail(op, info['mount'], info['dirs'], fstab_missing=True, mounted=False)


def format_guard(vg, name, dev):
    """Kontrole tuż przed mkfs: LV zwykły (nie migawka/pula), nieotwarty, bez „holders”, maj:min zgodny
    z LVM, niebędący dyskiem żadnej maszyny (także zatrzymanej). RuntimeError = odmowa."""
    lv = lvm.find_lv(vg, name)
    if not lv:
        raise RuntimeError(f'LV {vg}/{name} zniknął z LVM — przerwano przed formatowaniem.')
    attr = lv['attr']
    if attr[0] not in ('-', 'V'):
        raise RuntimeError(f'LV {vg}/{name} nie jest zwykłym wolumenem (lv_attr {attr}: migawka, origin, pula '
                           'lub inny typ) — odmowa formatowania.')
    if attr[5] == 'o':
        raise RuntimeError(f'LV {vg}/{name} jest otwarty (lv_attr {attr}) — odmowa formatowania.')
    have = lvm.block_majmin(dev)
    if not lv['majmin'] or have != lv['majmin']:
        raise RuntimeError(f'{dev} ({have or "brak"}) nie odpowiada LV {vg}/{name} ({lv["majmin"] or "brak"}) — '
                           'odmowa formatowania.')
    users = lvm.holders(lv['majmin'])
    if users:
        raise RuntimeError(f'Na {dev} zbudowano inne urządzenia ({", ".join(users)}) — odmowa formatowania.')
    vms = libvirt.vm_usage(None, [dev, lv['path'], lv['dm_path'], lvm.dev_paths(vg, name)[0]])
    if vms:
        raise RuntimeError(f'Wolumen jest dyskiem maszyny {vms[0][0]} ({vms[0][1]}) — odmowa formatowania.')
    if vms is None:
        raise RuntimeError('Nie można sprawdzić dysków maszyn (libvirt nie odpowiada) — odmowa formatowania.')


def _add_format_step(op, vg, name, device):
    def do():
        dev = op.ctx['device']
        format_guard(vg, name, dev)
        op.ctx['formatted'] = True
        fs.mkfs_ext4(dev, name)  # świeża kontrola sygnatur + jednorazowe uzbrojenie wewnątrz
        op.ctx['uuid'] = fs.read_uuid(dev)
    op.add(f'Formatowanie ext4 (wyłącznie pusty wolumen {device})', do, [shlex.join(fs.mkfs_args(device, name))])


def _add_linux_tail(op, mount, dirs, fstab_missing, mounted):
    if dirs:
        op.add(f'Utworzenie katalogu {mount}', lambda: mnt.make_dirs(dirs, op.undo), [f'mkdir {d}' for d in dirs])
    if fstab_missing:
        def fstab():
            op.ctx['backup'] = mnt.add_fstab_entry(op.ctx['uuid'], mount, op.undo)
        op.add(f'Wpis w {mnt.FSTAB} (kopia zapasowa, zapis atomowy, findmnt --verify)', fstab,
               [f'cp -p {mnt.FSTAB} {mnt.FSTAB}.backup-hvm-<data>',
                f'dopisanie: {mnt.fstab_line("<UUID>", mount).strip()}', 'systemctl daemon-reload',
                f'findmnt --verify --tab-file {mnt.FSTAB}'])
    if not mounted:
        op.add(f'Montowanie {mount} i weryfikacja',
               lambda: mnt.mount_and_verify(mount, op.ctx['device'], op.ctx.get('uuid'), op.undo),
               [f'mount {mount}', f'findmnt -M {mount}'])


def _pool_precheck(name, mount, expect_bytes, kind):
    entry = mnt.mount_at(mount)
    if not entry:
        raise RuntimeError(f'{mount} nie jest osobnym punktem montowania — pula nie zostanie zarejestrowana '
                           '(katalog leżałby na dysku systemowym).')
    if expect_bytes and entry['size'] < CAPACITY_MIN_RATIO * expect_bytes:
        raise RuntimeError(f'Pojemność {mount} ({entry["size"] / GIB:.1f} GiB) nie odpowiada wolumenowi '
                           f'({expect_bytes / GIB:.1f} GiB) — przerwano.')
    if not libvirt.available():
        raise RuntimeError('libvirt nie odpowiada (virsh uri) — sprawdź usługę libvirtd i uruchom naprawę (opcja 2).')
    now = libvirt.pool_decision(name, mount, libvirt.list_pools())[0]
    if now != kind and not {now, kind} <= {'existing', 'reuse'}:
        raise RuntimeError('Stan pul libvirt zmienił się w trakcie pracy — przerwano; uruchom naprawę (opcja 2).')


def _virsh_step(op, title, cmd):
    op.add(title, lambda: runner.run(cmd), [shlex.join(cmd)], 2)


def _add_pool_steps(op, decision, name, mount, expect_bytes, refresh):
    kind, pool = decision
    check = [f'findmnt -M {mount}', shlex.join(libvirt.virsh('uri')),
             shlex.join(libvirt.virsh('pool-list', '--all', '--name'))]
    if kind == 'create':
        libvirt.check_pool_name(name)
        define_cmd = libvirt.virsh('pool-define-as', '--name', name, '--type', 'dir', '--target', mount)

        def define():
            runner.run(define_cmd)
            op.ctx['pool_defined'] = name
            cmd = libvirt.virsh('pool-undefine', '--pool', name)
            op.undo(f'usunięcie definicji puli {name} zdefiniowanej w tym uruchomieniu (pliki nietknięte)',
                    shlex.join(cmd), lambda: (safety.arm(cmd), runner.run(cmd)))

        def start():
            runner.run(libvirt.virsh('pool-start', '--pool', name))
            if op.ctx.get('pool_defined') == name:
                cmd = libvirt.virsh('pool-destroy', '--pool', name)
                op.undo(f'zatrzymanie puli {name} (pool-destroy nie usuwa plików)', shlex.join(cmd),
                        lambda: (safety.arm(cmd), runner.run(cmd)))
        op.add('Kontrola przed rejestracją puli (montowanie, pojemność, libvirt)',
               lambda: _pool_precheck(name, mount, expect_bytes, kind), check, 2)
        op.add(f'Definicja puli libvirt {name} (katalog {mount})', define, [shlex.join(define_cmd)], 2)
        op.add(f'Uruchomienie puli {name}', start, [shlex.join(libvirt.virsh('pool-start', '--pool', name))], 2)
        _virsh_step(op, f'Autostart puli {name}', libvirt.virsh('pool-autostart', '--pool', name))
        _virsh_step(op, f'Odświeżenie puli {name}', libvirt.virsh('pool-refresh', '--pool', name))
        return
    name = libvirt.check_pool_name(pool['name'])
    start = pool['state'] != 'running'
    steps = []
    if start:
        steps.append((f'Uruchomienie istniejącej puli {name}', libvirt.virsh('pool-start', '--pool', name)))
    if pool['autostart'] != 'yes':
        steps.append((f'Autostart istniejącej puli {name}', libvirt.virsh('pool-autostart', '--pool', name)))
    if refresh or start:
        steps.append((f'Odświeżenie puli {name}', libvirt.virsh('pool-refresh', '--pool', name)))
    if steps:
        op.add(f'Kontrola przed użyciem puli {name}', lambda: _pool_precheck(name, mount, expect_bytes, kind), check, 2)
    for title, cmd in steps:
        _virsh_step(op, title, cmd)


# --- plany ------------------------------------------------------------------------------------------

def plan_new(vg, name, size, mount, pool, assume_yes=False, fix_dev=False, with_pool=True):
    """Plan NOWEGO magazynu: (Operation, info). Rzuca ValueError (nic nie zostało zmienione)."""
    info = validate_new(vg, name, size, mount)
    decision = None
    if with_pool:
        libvirt.check_pool_name(pool)
        if not libvirt.available():
            raise ValueError('libvirt nie odpowiada (virsh uri) — nie można zarejestrować puli. Sprawdź usługę '
                             'libvirtd i uruchom kreator ponownie.')
        decision = libvirt.pool_decision(pool, info['mount'], libvirt.list_pools())
        if decision[0] == 'refuse':
            raise ValueError(decision[1])
        if decision[0] == 'reuse':
            print(f'Katalog {info["mount"]} jest już celem puli {decision[1]["name"]} — zostanie ona użyta '
                  '(bez tworzenia duplikatu).')
    rmdirs = approve_conflicts(lvm.conflict_findings(vg, name), vg, name, assume_yes, fix_dev)
    op = safety.Operation()
    for path in rmdirs:
        _rmdir_step(op, path)
    _add_create_steps(op, info)
    if with_pool:
        _add_pool_steps(op, decision, pool, info['mount'], info['size'] * GIB, refresh=True)
        info['pool'] = decision[1]['name'] if decision[0] == 'reuse' else pool
    return op, info


def plan_repair(t, assume_yes=False, format_empty=None, fix_dev=False, with_pool=True):
    """Plan naprawy ISTNIEJĄCEGO magazynu: (Operation, stan, uwagi). Rzuca ValueError przy odmowie.
    format_empty: w trybie --yes musi być dokładnie '<vg>/<lv>' celu, inaczej pusty LV nie zostanie sformatowany."""
    vg, name = t['vg'], t['lv']
    if with_pool and t.get('pool'):
        libvirt.check_pool_name(t['pool'])
    if not lvm.verify_vg_name(vg):
        raise ValueError(f'Niedozwolona nazwa grupy LVM {vg!r}.')
    if not lvm.verify_name(name):
        raise ValueError(f'Niedozwolona nazwa wolumenu {name!r} ({lvm.NAME_HELP}).')
    mount = t['mount'] = mnt.check_allowed_mount(t['mount'])
    check_protected(mount)
    s = diag.inspect_storage(t, with_pool)
    if s['errors']:
        raise ValueError('Nie można odczytać stanu systemu: ' + '; '.join(s['errors']))
    op, notes, unchanged = safety.Operation(), [], []
    op.ctx['unchanged'] = unchanged
    lvpath = lvm.dev_paths(vg, name)[0]
    if not s['lv']:
        if vg not in {v['vg_name'] for v in lvm.list_vgs()}:
            raise ValueError(f'Grupa LVM {vg} nie istnieje.')
        if not s['conflicts']:
            raise ValueError(f'Wolumen {lvpath} nie istnieje — do utworzenia nowego magazynu użyj opcji 1 '
                             '(polecenie prepare lub create).')
        for path in approve_conflicts(s['conflicts'], vg, name, assume_yes, fix_dev):
            _rmdir_step(op, path)
        op.ctx['after'] = 'Ścieżka urządzenia jest wolna — teraz można utworzyć magazyn (opcja 1).'
        return op, s, notes
    if s['blockers']:
        raise ValueError('\n'.join(s['blockers']))
    dev = s['dev']
    op.ctx.update(device=dev['device'], uuid=s['uuid'])
    if dev['case']:
        print(f'Urządzenie: {dev["detail"]}')
    mknodes = dev['case'] == 'B'
    if dev['case'] == 'A':
        fixable, reason = lvm.conflict_fixable(lvpath)
        paths = []
        if fixable:
            try:
                paths = approve_conflicts([{'path': lvpath, 'fixable': True, 'reason': reason}], vg, name,
                                          assume_yes, fix_dev)
            except ValueError as exc:
                reason = str(exc)
        for path in paths:
            _rmdir_step(op, path)
        mknodes = bool(paths)
        if not paths:
            notes.append(f'{reason} LV pozostaje dostępny przez {dev["device"]}.')
    if mknodes:
        def mknodes_step():
            runner.run(['vgscan', '--mknodes'], check=False)
            runner.run(['udevadm', 'settle', '--timeout=30'], check=False)
            if lvm.node_info(lvpath)[0] != 'link':
                raise safety.Skip(f'dowiązanie {lvpath} nadal nie istnieje — używam {dev["device"]}')
        op.add(f'Odtworzenie dowiązania {lvpath} narzędziem LVM (vgscan --mknodes)', mknodes_step,
               ['vgscan --mknodes', 'udevadm settle --timeout=30'])
    if s['sig'] == 'empty':
        if assume_yes and format_empty != f'{vg}/{name}':
            raise ValueError(f'LV {vg}/{name} jest pusty i wymaga formatowania. W trybie --yes podaj dokładnie '
                             f'--format-empty {vg}/{name} albo uruchom bez --yes (fraza FORMATUJ {name}).')
        op.ctx['needs_format'] = True
        _add_format_step(op, vg, name, dev['device'])
    else:
        unchanged.append('Wykryto ext4 — formatowanie pominięte.')
    mounted = s['mounted_ours']
    if s['fstab'] == 'missing' and not mnt.fstab_verify_ok():
        raise ValueError(f'findmnt --verify zgłasza błędy w obecnym {mnt.FSTAB} — napraw je przed dodaniem nowego wpisu.')
    _add_linux_tail(op, mount, [] if mounted else s['dirs'], s['fstab'] == 'missing', mounted)
    if s['fstab'] == 'ok':
        unchanged.append(f'Wpis w {mnt.FSTAB} jest już poprawny — bez zmian.')
    if mounted:
        unchanged.append(f'{mount} jest już zamontowany z tego LV — montowanie pominięte.')
    if with_pool:
        _plan_repair_pool(op, s, t, notes, unchanged)
    return op, s, notes


def _plan_repair_pool(op, s, t, notes, unchanged):
    if not s['libvirt']:
        notes.append('libvirt nie odpowiada (virsh uri) — pula nie została sprawdzona ani utworzona.')
        return
    if not t.get('pool'):
        t['pool'] = libvirt.try_suggest(diag.hostname(), diag.gib(s['lv']['size']))
        if not t['pool']:
            notes.append('Nie można zaproponować nazwy puli z nazwy hosta — podaj ją jawnie (--pool-name).')
            return
        s['decision'] = libvirt.pool_decision(t['pool'], t['mount'], s['pools'])
    kind, val = s['decision']
    if kind == 'refuse':
        notes.append(f'Pula libvirt: {val}')
        return
    before = len(op.steps)
    _add_pool_steps(op, s['decision'], t['pool'], t['mount'], s['lv']['size'], refresh=bool(op.steps))
    if kind == 'reuse':
        print(f'Katalog {t["mount"]} jest już celem puli {val["name"]} — zostanie ona użyta (bez duplikatu).')
    if kind != 'create' and len(op.steps) == before:
        unchanged.append(f'Pula libvirt {val["name"]} aktywna, z autostartem — bez zmian.')


# --- potwierdzenie i wykonanie -------------------------------------------------------------------------

def confirm(op, lines, assume_yes, style='menu', phrase=None, phrase_prompt=None):
    """'run' | 'dry' | 'cancel'. Styl 'menu': [1] Wykonaj / [2] szczegóły / [0] Anuluj;
    'phrase': tylko fraza; 'yesno': pytanie [t/N]. Fraza (np. FORMATUJ <lv>) zawsze dodatkowo."""
    if lines:
        print('\n' + '\n'.join(lines))
    if runner.DRY_RUN:
        op.print_details(dry=True)
        ui.ok('\nDRY-RUN zakończony — nic nie zostało zmienione.')
        return 'dry'
    if assume_yes:
        ui.warn('Potwierdzenie pominięte (--yes).')
        return 'run'
    if style == 'menu':
        while True:
            print('[1] Wykonaj\n[2] Pokaż szczegóły techniczne\n[0] Anuluj')
            choice = ui.choose('Wybór: ', ('0', '1', '2'))
            if choice == '2':
                op.print_details()
                continue
            if choice == '0':
                print('Anulowano — nic nie zmieniono.')
                return 'cancel'
            break
    elif style == 'yesno' and not ui.ask_yes_no('Wykonać powyższe kroki? [t/N]: '):
        print('Anulowano — nic nie zmieniono.')
        return 'cancel'
    if phrase and not ui.ask_phrase(phrase_prompt or f'Wpisz dokładnie {phrase}: ', phrase):
        print('Anulowano — nic nie zmieniono.')
        return 'cancel'
    return 'run'


def offer_lvremove(t):
    """Interaktywnie (nigdy w --yes): usunięcie LV utworzonego w TYM uruchomieniu po frazie USUN <lv>."""
    vg, name = t['vg'], t['lv']
    lvpath = f'/dev/{vg}/{name}'
    try:
        lv = lvm.find_lv(vg, name)
    except Exception:  # noqa: BLE001
        return
    if not lv:
        return
    print(f'\nWolumen {lvpath} został utworzony w tym uruchomieniu i NIE został usunięty automatycznie.')
    if lv['attr'][5] == 'o':
        print('Wolumen jest otwarty (zamontowany lub używany) — nie można go usunąć.')
        return
    phrase = f'USUN {name}'
    if not ui.ask_phrase(f'Aby go usunąć (lvremove), wpisz dokładnie {phrase} (Enter = zostaw): ', phrase):
        print('Wolumen pozostawiony — można dokończyć konfigurację (opcja 2).')
        return
    cmd = ['lvremove', '-y', f'{vg}/{name}']
    safety.arm(cmd)
    try:
        runner.run(cmd)
        ui.ok(f'Usunięto {lvpath}.')
    except Exception as exc:  # noqa: BLE001
        ui.err(f'Nie udało się usunąć {lvpath}: {ui.describe(exc)}')


def execute(op, t, dirs, assume_yes):
    try:
        before = diag.snapshot(t, dirs)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        ui.err(f'Nie można odczytać stanu wyjściowego: {ui.describe(exc)}')
        return False
    runner.log.info('START: %s', t)
    success = op.run()
    op.report()
    if success:
        runner.log.info('SUKCES: %s', t)
        return True
    runner.log.error('NIEUDANE (faza %s): %s', op.failed_phase, t)
    with safety.no_interrupt():
        _after_failure(op, t, before, dirs)
    if op.ctx.get('lv_created') and not assume_yes and op.failed_phase != 2:
        offer_lvremove(t)
    print(NEXT_STEP)
    return False


def _after_failure(op, t, before, dirs):
    if op.failed_phase == 2:
        ui.warn('Wolumen Linux pozostaje przygotowany; rejestracja puli libvirt nie powiodła się '
                '(wycofano wyłącznie kroki dotyczące puli).')
    else:
        if op.ctx.get('formatted'):
            ui.warn(f'w tym uruchomieniu uruchomiono mkfs.ext4 na {op.ctx.get("device")} — formatowania nie da się '
                    'cofnąć (wolumen był pusty). Pełne wycofanie nie jest możliwe.')
        diag.diagnose_after(t, before, dirs, op)


def _summary_new(host_label, info):
    return ['PODSUMOWANIE OPERACJI', f'Serwer: {host_label}', f'Nowy magazyn: {info["pool"]}',
            f'Pojemność: {info["size"]} GiB', f'Lokalizacja: {info["mount"]}',
            'Operacja utworzy nowy magazyn i zarejestruje', 'jego pulę w lokalnym libvirt.',
            'Dysk systemowy i istniejące maszyny', 'nie zostaną celowo zmodyfikowane.']


def _finish_ok(mount, pool=None):
    ui.ok(f'\nGOTOWE. Magazyn {pool + " " if pool else ""}jest dostępny w {mount}.')
    runner.output(['df', '-h', mount])
    nofail_warning(mount)
    if pool:
        print(diag.MORPHEUS_NOTE)
        print(diag.MORPHEUS_HINT)


def run_new(op, info, assume_yes):
    t = {'vg': info['vg'], 'lv': info['lv'], 'mount': info['mount'], 'pool': info['pool']}
    decision = confirm(op, _summary_new(libvirt.host_label(diag.hostname()), info), assume_yes, 'menu',
                       f'FORMATUJ {info["lv"]}',
                       f'Nowy wolumen {info["lvpath"]} zostanie sformatowany (ext4). Wpisz dokładnie FORMATUJ {info["lv"]}: ')
    if decision != 'run':
        return decision == 'dry'
    if not execute(op, t, info['dirs'], assume_yes):
        return False
    _finish_ok(info['mount'], info['pool'])
    return True


# --- menu 1: nowy magazyn ---------------------------------------------------------------------------

def _pick_vg(cands):
    names = [c['name'] for c in cands]
    if DEFAULT_VG in names:
        return cands[names.index(DEFAULT_VG)]
    if len(cands) == 1:
        return cands[0]
    print('Grupy LVM z wolnym miejscem:')
    for i, c in enumerate(cands, 1):
        print(f'[{i}] {c["name"]} — do wykorzystania {c["max_gib"]} GiB')

    def pick(raw):
        if raw.isdigit() and 1 <= int(raw) <= len(cands):
            return cands[int(raw) - 1]
        raise ValueError(f'Podaj numer 1–{len(cands)}.')
    return ui.ask('Wybierz grupę (numer)', None, pick)


def _ask_size(max_gib):
    opts, _ = size_options(max_gib)
    print('Ile miejsca przeznaczyć na maszyny?')
    for i, (_, label) in enumerate(opts, 1):
        print(f'[{i}] {label}')
    custom = len(opts) + 1
    print(f'[{custom}] Wpisz własną wartość\n[0] Wróć')
    choice = ui.choose('Wybór: ', [str(i) for i in range(custom + 1)])
    if choice == '0':
        raise ui.Cancelled
    if choice == str(custom):
        return ui.ask(f'Rozmiar w GiB (1–{max_gib})', None, lambda s: resolve_size(s, max_gib))
    return opts[int(choice) - 1][0]


def prepare_interactive():
    """Menu 1: wykrycie → wybór rozmiaru i nazwy → podsumowanie → wykonanie."""
    try:
        host = diag.hostname()
        t = diag.locate_target()
        if lvm.lv_exists(t['vg'], t['lv']):
            print(f'\nMagazyn {t["vg"]}/{t["lv"]} już istnieje — nie zostanie utworzony ponownie ani sformatowany.\n'
                  'Przechodzę do sprawdzenia i dokończenia konfiguracji.')
            return repair(t)
        data = disks.scan(libvirt.vm_sources() or [])
        print('\nNOWY MAGAZYN DANYCH\nWykryto:')
        print(f'Serwer: {libvirt.host_label(host)}')
        blocked = [d for d in data['disks'] if d['status'] != 'free']
        for d in blocked:
            print(ui.state_line('error', f'{d["name"]}: {d["reason"]}'))
        free_disks = [d for d in data['disks'] if d['status'] == 'free']
        for d in free_disks:
            print(ui.state_line('unknown', f'{d["name"]}: {d["reason"]}'))
        if not data['candidates']:
            ui.err('Brak wolnego miejsca w grupach LVM — nie można utworzyć magazynu w wersji 1.0.')
            return False
        vg = _pick_vg(data['candidates'])
        if lvm.lv_exists(vg['name'], DEFAULT_LV):
            return repair(diag.locate_target(vg['name'], DEFAULT_LV))
        print(f'Dostępne miejsce: {vg["free"] // GIB} GiB (grupa {vg["name"]}; po odjęciu rezerwy LVM '
              f'{vg["reserve_gib"]} GiB: {vg["max_gib"]} GiB)')
        size = _ask_size(vg['max_gib'])
        pool = ui.ask('Nazwa magazynu (pula libvirt)', libvirt.try_suggest(host, size), libvirt.check_pool_name)
        op, info = plan_new(vg['name'], DEFAULT_LV, size, DEFAULT_MOUNT, pool)
    except ui.Cancelled:
        print('\nAnulowano — powrót do menu.')
        return False
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        ui.err(ui.describe(exc))
        return False
    return run_new(op, info, assume_yes=False)


def prepare_cli(size=None, vg=None, name=None, mount=None, pool=None, assume_yes=False, fix_dev=False):
    """Polecenie prepare: kreator bez pytań o parametry (potwierdzenie — chyba że --yes)."""
    name, mount = name or DEFAULT_LV, mount or DEFAULT_MOUNT
    try:
        vgs = {v['vg_name']: v for v in lvm.list_vgs()}
        if not vg:
            fitting = [n for n, v in vgs.items() if lvm.max_size_gib(v['vg_free']) >= 1]
            vg = DEFAULT_VG if DEFAULT_VG in vgs else (fitting[0] if len(fitting) == 1 else None)
            if not vg:
                raise ValueError('Nie można jednoznacznie wybrać grupy LVM — podaj --vg.')
        if pool:
            libvirt.check_pool_name(pool)
        if vg in vgs and lvm.lv_exists(vg, name):
            print(f'Wolumen /dev/{vg}/{name} już istnieje — nie zostanie utworzony ponownie ani sformatowany. '
                  'Uruchamiam naprawę.')
            return repair(diag.locate_target(vg, name, mount, pool), assume_yes, fix_dev=fix_dev)
        if vg not in vgs:
            raise ValueError(f'Grupa LVM {vg} nie istnieje. Dostępne: {", ".join(vgs) or "brak"}.')
        size = resolve_size(size or 'recommended', lvm.max_size_gib(vgs[vg]['vg_free']))
        op, info = plan_new(vg, name, size, mount, pool or libvirt.suggest_name(diag.hostname(), size),
                            assume_yes, fix_dev)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        ui.err(ui.describe(exc))
        return False
    return run_new(op, info, assume_yes)


# --- menu 2: naprawa ---------------------------------------------------------------------------------

def print_repair_plan(op, s):
    t, lv = s['target'], s['lv']
    print('\nPODSUMOWANIE NAPRAWY')
    print(f'Serwer: {libvirt.host_label(diag.hostname())}')
    print(f'Magazyn: {t["vg"]}/{t["lv"]} → {t["mount"]}' + (f' (pula {t["pool"]})' if t.get('pool') else ''))
    if lv:
        print(f'Wolumen: {lv["size"] / GIB:.1f} GiB, lv_attr {lv["attr"]}, sygnatury: {s["sigdesc"] or "?"}')
    print('Kroki do wykonania:')
    for i, step in enumerate(op.steps, 1):
        print(f'  {i}. {step.title}')
    for line in op.ctx.get('unchanged', []):
        print(f'  {ui.sym("skip")} {line}')
    print('Istniejące dane, wolumeny i pule nie zostaną usunięte ani nadpisane.')


def repair(t=None, assume_yes=False, format_empty=None, fix_dev=False, with_pool=True, style='menu'):
    """Menu 2 / repair: diagnoza przypadków A–D i dokończenie brakujących kroków."""
    try:
        t = dict(t) if t else diag.locate_target()
        print(f'\nSprawdzam magazyn: {t["vg"]}/{t["lv"]} → {t["mount"]}')
        op, s, notes = plan_repair(t, assume_yes, format_empty, fix_dev, with_pool)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        ui.err(ui.describe(exc))
        return False
    if with_pool and s['lv']:
        diag.print_storage(diag.storage_entry(s))
    for n in notes:
        ui.warn(n)
    if not op.steps:
        if style == 'yesno':
            ui.ok('Nic do zrobienia — konfiguracja jest kompletna, nie wprowadzono zmian.')
            nofail_warning(t['mount'])
            return True
        if notes:
            return False
        print(f'{ui.sym("ok")} {diag.D_MESSAGE}')
        return True
    print_repair_plan(op, s)
    phrase = f'FORMATUJ {t["lv"]}' if op.ctx.get('needs_format') else None
    decision = confirm(op, [], assume_yes, style, phrase,
                       f'Formatowanie {op.ctx.get("device")} jest NIEODWRACALNE. Wpisz dokładnie {phrase}: ')
    if decision != 'run':
        return decision == 'dry'
    if not execute(op, t, s['dirs'], assume_yes):
        return False
    if op.ctx.get('after'):
        ui.ok(op.ctx['after'])
        return True
    _finish_ok(t['mount'], t.get('pool') if with_pool else None)
    return True


# --- menu 4: magazyny Morpheus ---------------------------------------------------------------------------

def check_storages():
    t = diag.locate_target()
    s = diag.inspect_storage(t)
    entries = diag.storage_entries(s)
    print('\nMAGAZYNY NA TYM HOŚCIE')
    for e in entries:
        diag.print_storage(e)
    pools = [e['libvirt']['pool'] for e in entries if e['libvirt'].get('pool') and e['libvirt'].get('running')]
    if pools and ui.ask_yes_no(f'\nOdświeżyć pojemność pul w libvirt ({", ".join(pools)})? [t/N]: '):
        for name in pools:
            try:
                runner.run(libvirt.virsh('pool-refresh', '--pool', libvirt.check_pool_name(name)))
                info = libvirt.pool_info(name)
                ui.ok(f'Odświeżono {name}: pojemność {info.get("Capacity", "?")}, wolne {info.get("Available", "?")}')
            except Exception as exc:  # noqa: BLE001
                ui.err(f'Odświeżenie {name} nieudane: {ui.describe(exc)}')
    print('\n' + diag.MORPHEUS_NOTE)
    print(diag.HOSTS_NOTE)
    return True
