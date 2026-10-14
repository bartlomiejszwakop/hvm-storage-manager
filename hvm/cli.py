"""HVM Storage Manager — argumenty wiersza poleceń i menu (punkt wejścia: hvm.cli:main)."""
import argparse
import json
import subprocess
import sys

from hvm import __version__, diagnostics as diag, disks, legacy, libvirt, lvm, mount as mnt, runner, ui, wizard

PROG = 'hvm-storage-manager'
LOG_PATH = [runner.LOG_FILE]  # ścieżka z --log-file (używana po wyłączeniu dry-run w menu)
MENU = ('1. Przygotuj nowy magazyn dla VM\n2. Napraw lub dokończ konfigurację\n3. Pokaż dostępne dyski i miejsce\n'
        '4. Sprawdź magazyny Morpheus\n5. Sprawdź stan serwera\n6. Zaawansowane\n0. Wyjście')


def header():
    info = diag.host_info()
    ui.box([f'HVM STORAGE MANAGER {".".join(__version__.split(".")[:2])}', 'Zarządzanie dyskami HPE HVM'])
    print(f'Serwer: {info["hostname"]}\nAdres IP: {info["ip"]}\nSystem: {info["os"]}')
    if runner.DRY_RUN:
        ui.warn('tryb podglądu (dry-run) — polecenia zmieniające system są tylko wypisywane.')
    print('\nSTATUS MAGAZYNÓW')
    for state, text in diag.header_status():
        print(ui.state_line(state, text))


def show_disks():
    try:
        disks.print_scan(disks.scan(libvirt.vm_sources() or []))
    except Exception as exc:  # noqa: BLE001
        ui.err(f'Nie można odczytać dysków: {ui.describe(exc)}')


def server_state():
    diag.print_report(diag.collect_report())


def advanced():
    while True:
        print(f'\nZAAWANSOWANE\n'
              f'1. Pokazuj polecenia przed wykonaniem: {"WŁ" if runner.SHOW_COMMANDS else "WYŁ"}\n'
              f'2. Tryb podglądu bez zmian (dry-run): {"WŁ" if runner.DRY_RUN else "WYŁ"}\n'
              '3. LVM: lsblk / pvs / vgs / lvs\n4. libvirt: pool-list i pool-info\n5. Sprawdź /etc/fstab\n'
              '6. Diagnostyka device-mapper i udev\n7. Eksport raportu JSON\n8. Lokalizacja logu operacji\n'
              '9. Narzędzia 0.4: utwórz wolumen (create)\n10. Narzędzia 0.4: dokończ konfigurację (resume)\n'
              '11. Narzędzia 0.4: pule libvirt (pools)\n12. Narzędzia 0.4: audyt (audit)\n0. Powrót')
        choice = ui.choose('Wybór: ', [str(i) for i in range(13)])
        try:
            if choice == '0':
                return
            if choice == '1':
                runner.SHOW_COMMANDS = not runner.SHOW_COMMANDS
            elif choice == '2':
                toggle_dry_run()
            elif choice == '3':
                for cmd in (['lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS'], ['pvs'], ['vgs'],
                            ['lvs', '-o', 'vg_name,lv_name,lv_size,lv_attr,lv_path']):
                    print(f'\n=== {" ".join(cmd)} ===')
                    runner.output(cmd)
            elif choice == '4':
                runner.output(libvirt.virsh('pool-list', '--all'))
                for name in libvirt.pool_names():
                    print(f'\n=== {name} ===')
                    runner.output(libvirt.virsh('pool-info', '--pool', name))
            elif choice == '5':
                check_fstab()
            elif choice == '6':
                dm_udev()
            elif choice == '7':
                diag.export_report(ui.ask('Plik raportu (Enter = domyślny w /var/log)', '', str) or None)
            elif choice == '8':
                print(f'Log operacji: {runner.log_path or "niedostępny (brak uprawnień do zapisu)"}')
            elif choice == '9':
                legacy.create_store()
            elif choice == '10':
                legacy.resume_volume(ui.ask('Grupa LVM', diag.DEFAULT_VG), ui.ask('Nazwa LV', diag.DEFAULT_LV),
                                     ui.ask('Punkt montowania', diag.DEFAULT_MOUNT, mnt.check_allowed_mount))
            elif choice == '11':
                legacy.pools_command(ui.ask('Punkt montowania', diag.DEFAULT_MOUNT, mnt.normalize_mount), refresh=True)
            elif choice == '12':
                legacy.audit_command(ui.ask('Punkt montowania', diag.DEFAULT_MOUNT, mnt.normalize_mount))
        except ui.Cancelled:
            print('\nAnulowano — powrót do menu.')
        except Exception as exc:  # noqa: BLE001 — menu nie może się wysypać
            ui.err(ui.describe(exc))


def toggle_dry_run():
    if not runner.DRY_RUN:
        runner.DRY_RUN = True
        runner.setup_logging(None)
        return
    if not runner.is_root():
        ui.err('Bez uprawnień root można pracować tylko w trybie podglądu.')
        return
    runner.DRY_RUN = False
    try:
        locked = runner.acquire_lock()
    except OSError as exc:
        locked, why = False, str(exc)
    else:
        why = 'działa inna instancja programu'
    if not locked:
        runner.DRY_RUN = True
        ui.err(f'Nie można wyłączyć trybu podglądu: {why}.')
        return
    runner.setup_logging(LOG_PATH[0])


def check_fstab():
    runner.output(['findmnt', '--verify', '--tab-file', mnt.FSTAB])
    t = diag.locate_target()
    s = diag.inspect_storage(t, with_pool=False)
    print(f'Wpis dla {t["mount"]}: ' + {'ok': 'poprawny (UUID zgodny z wolumenem)', 'missing': 'brak',
                                        'conflict': 'NIEZGODNY — wymagany ręczny przegląd'}.get(s['fstab'], '?'))


def dm_udev():
    t = diag.locate_target()
    lvpath, mapper = lvm.dev_paths(t['vg'], t['lv'])
    print(f'{lvpath}: {lvm.path_state(lvpath)}\n{mapper}: {lvm.path_state(mapper)}')
    runner.output(['dmsetup', 'ls'])
    runner.output(['dmsetup', 'info', lvm.dm_name(t['vg'], t['lv'])])
    runner.output(['udevadm', 'info', '--query=all', f'--name={mapper}'])


def menu():
    while True:
        try:
            header()
        except Exception as exc:  # noqa: BLE001
            ui.err(f'Nie udało się odczytać stanu: {ui.describe(exc)}')
        print('\n' + MENU)
        choice = ui.choose('Wybór: ', ('0', '1', '2', '3', '4', '5', '6'))
        try:
            if choice == '0':
                return 0
            {'1': wizard.prepare_interactive, '2': wizard.repair, '3': show_disks, '4': wizard.check_storages,
             '5': server_state, '6': advanced}[choice]()
        except ui.Cancelled:
            print('\nAnulowano — powrót do menu.')
        except Exception as exc:  # noqa: BLE001 — menu nie może się wysypać
            ui.err(ui.describe(exc))
        try:
            input('\nEnter = powrót do menu ')
        except EOFError:
            return 0


def build_parser():
    p = argparse.ArgumentParser(prog=PROG, description='Lokalne magazyny VM (LVM + ext4 + pula libvirt) dla HPE HVM. '
                                                       'Bez podkomendy uruchamia menu.')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    p.add_argument('--dry-run', action='store_true', help='pokaż polecenia zmieniające system zamiast je wykonywać')
    p.add_argument('--log-file', default=runner.LOG_FILE, metavar='PATH', help=f'plik logu (domyślnie {runner.LOG_FILE})')
    p.add_argument('--diagnose', action='store_true', help='raport stanu serwera (tylko odczyt)')
    p.add_argument('--json', action='store_true', help='z --diagnose: raport JSON na stdout')
    sub = p.add_subparsers(dest='command', metavar='{prepare,repair,create,resume,pools,audit}')
    pr = sub.add_parser('prepare', help='nowy magazyn: LV + ext4 + fstab + mount + pula libvirt (bez menu)')
    pr.add_argument('--size', default='recommended', help='GiB, max albo recommended (domyślnie)')
    pr.add_argument('--vg', help=f'grupa LVM (domyślnie {diag.DEFAULT_VG} lub jedyna z wolnym miejscem)')
    pr.add_argument('--name', help=f'nazwa LV (domyślnie {diag.DEFAULT_LV})')
    pr.add_argument('--mount', help=f'punkt montowania (domyślnie {diag.DEFAULT_MOUNT})')
    pr.add_argument('--pool-name', help='nazwa magazynu / puli libvirt (domyślnie <HOST>-Local-<GiB>)')
    pr.add_argument('--yes', action='store_true', help='bez pytań o potwierdzenie (formatuje wyłącznie NOWY LV)')
    pr.add_argument('--fix-dev-conflict', action='store_true',
                    help='pozwól usunąć (rmdir) PUSTY, nieużywany katalog blokujący /dev/<vg>/<lv> także z --yes')
    rp = sub.add_parser('repair', help='napraw lub dokończ konfigurację istniejącego magazynu')
    rp.add_argument('--vg', help='grupa LVM (domyślnie wykrywana)')
    rp.add_argument('--name', help=f'nazwa LV (domyślnie wykrywana / {diag.DEFAULT_LV})')
    rp.add_argument('--mount', help=f'punkt montowania (domyślnie {diag.DEFAULT_MOUNT})')
    rp.add_argument('--pool-name', help='nazwa puli libvirt (domyślnie istniejąca pula katalogu lub <HOST>-Local-<GiB>)')
    rp.add_argument('--yes', action='store_true', help='bez pytań o potwierdzenie')
    rp.add_argument('--format-empty', metavar='VG/LV',
                    help='z --yes pozwól sformatować istniejący LV BEZ żadnych sygnatur — dokładnie ten VG/LV')
    rp.add_argument('--fix-dev-conflict', action='store_true',
                    help='z --yes pozwól usunąć (rmdir) PUSTY, nieużywany katalog w miejscu /dev/<vg>/<lv>')
    c = sub.add_parser('create', help='[0.4] utwórz nowy wolumen (bez puli libvirt)')
    c.add_argument('--vg', required=True, help='istniejąca grupa LVM, np. ubuntu-vg')
    c.add_argument('--name', required=True, help='nazwa nowego LV, np. vm-storage')
    c.add_argument('--size', required=True, type=int, help='rozmiar w GiB')
    c.add_argument('--mount', required=True, help='punkt montowania, np. /vm-storage')
    c.add_argument('--yes', action='store_true', help=f'nie pytaj o frazę {legacy.CONFIRM_PHRASE!r}')
    c.add_argument('--fix-dev-conflict', action='store_true',
                   help='pozwól usunąć (rmdir) PUSTY katalog blokujący /dev/<vg>/<lv> także w trybie --yes')
    r = sub.add_parser('resume', help='[0.4] dokończ konfigurację ISTNIEJĄCEGO LV (ext4, katalog, fstab, mount)')
    r.add_argument('--vg', required=True, help='grupa LVM, np. ubuntu-vg')
    r.add_argument('--name', required=True, help='nazwa istniejącego LV, np. vm-storage')
    r.add_argument('--mount', required=True, help='punkt montowania, np. /vm-storage')
    r.add_argument('--yes', action='store_true', help='nie pytaj o potwierdzenie kroków')
    r.add_argument('--format-empty', metavar='VG/LV',
                   help='w trybie --yes pozwól sformatować LV BEZ żadnych sygnatur — dokładnie ten VG/LV')
    pl = sub.add_parser('pools', help='[0.4] wykryj pule libvirt typu dir na punkcie montowania i opcjonalnie odśwież')
    pl.add_argument('--mount', default=diag.DEFAULT_MOUNT, help=f'punkt montowania (domyślnie {diag.DEFAULT_MOUNT})')
    pl.add_argument('--refresh', action='store_true', help='odśwież znalezione pule (virsh pool-refresh)')
    pl.add_argument('--yes', action='store_true', help='nie pytaj o potwierdzenie odświeżenia')
    a = sub.add_parser('audit', help='[0.4] audyt lokalnego magazynu i propozycja nazwy (tylko odczyt)')
    a.add_argument('--mount', default=diag.DEFAULT_MOUNT, help=f'punkt montowania (domyślnie {diag.DEFAULT_MOUNT})')
    for s in (pr, rp, c, r, pl, a):
        s.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def _safe_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass


def diagnose(as_json):
    if not runner.is_root() and not as_json:
        ui.warn('bez uprawnień root część informacji (LVM, blkid) może być niedostępna.')
    rep = diag.collect_report()
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        diag.print_report(rep)
    return 0


def dispatch(args):
    cmd = args.command
    if cmd == 'prepare':
        return wizard.prepare_cli(args.size, args.vg, args.name, args.mount, args.pool_name, args.yes,
                                  args.fix_dev_conflict)
    if cmd == 'repair':
        target = diag.locate_target(args.vg, args.name, args.mount, args.pool_name)
        return wizard.repair(target, args.yes, args.format_empty, args.fix_dev_conflict)
    if cmd == 'create':
        return legacy.create_volume(args.vg, args.name, args.size, args.mount, args.yes, fix_dev=args.fix_dev_conflict)
    if cmd == 'resume':
        return legacy.resume_volume(args.vg, args.name, args.mount, args.yes, format_empty=args.format_empty)
    if cmd == 'pools':
        return legacy.pools_command(args.mount, args.refresh, args.yes)
    return legacy.audit_command(args.mount)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json and not args.diagnose:
        parser.error('--json działa tylko razem z --diagnose')
    if args.diagnose and args.command:
        parser.error('--diagnose nie łączy się z podkomendą')
    runner.DRY_RUN = bool(args.dry_run)
    LOG_PATH[0] = args.log_file
    no_write = runner.DRY_RUN or (args.diagnose and not runner.is_root())
    runner.setup_logging(None if no_write else args.log_file)  # dry-run / diagnoza bez root: nic nie zapisujemy
    _safe_stdout()
    runner.log.info('start v%s, polecenie=%s, dry_run=%s', __version__,
                    'diagnose' if args.diagnose else (args.command or 'menu'), runner.DRY_RUN)
    try:
        if args.diagnose:
            return diagnose(args.json)
        if not runner.DRY_RUN and not runner.is_root():
            ui.err('Program wymaga uprawnień administratora.')
            print(f'Uruchom: sudo {PROG}  (podgląd bez zmian: --dry-run, raport: --diagnose)')
            return 1
        try:
            if not runner.acquire_lock():
                ui.err('Działa już inna instancja programu. Zaczekaj na jej zakończenie.')
                return 1
        except OSError as exc:
            ui.err(f'Nie można założyć blokady {runner.LOCK_FILE}: {exc}')
            return 1
        if runner.DRY_RUN:
            ui.warn('Tryb DRY-RUN — polecenia zmieniające system zostaną tylko wypisane.')
        if args.command:
            try:
                return 0 if dispatch(args) else 1
            except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
                ui.err(ui.describe(exc))
                return 1
        return menu()
    except KeyboardInterrupt:
        print('\nPrzerwano.')
        return 130
    except EOFError:
        print('\nPrzerwano.')
        return 1

