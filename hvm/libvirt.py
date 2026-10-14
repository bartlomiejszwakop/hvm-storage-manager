"""libvirt: wykrywanie pul, decyzja o puli magazynu, nazwy per host, użycie dysków przez maszyny wirtualne.

Pula libvirt to zasób LOKALNEGO hosta. Program nie zakłada, że hosty tworzą klaster ani że
dzielą dyski — każdy host dostaje własną pulę z własną nazwą (np. HVM1-Local-1200).
Każde wywołanie: `virsh -c qemu:///system ...`, nazwy wyłącznie jako wartości --pool/--name/--domain.
"""
import os
import re
import xml.etree.ElementTree as ET

from hvm import lvm, runner, ui
from hvm.safety import PROTECTED_POOLS, VIRSH_URI

GIB = lvm.GIB
# Krótka nazwa hosta (małe litery) → prefiks nazwy magazynu. Pozostałe hosty: nazwa WIELKIMI literami.
HOST_ALIASES = {'hvm1': 'HVM1', 'node1': 'HVM1', 'hvm2': 'HVM2', 'node2': 'HVM2'}
GENERIC_HOSTNAMES = ('', 'localhost', 'host', 'ubuntu')
POOL_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}')


def virsh(*args):
    return ['virsh', '-c', VIRSH_URI] + [str(a) for a in args]


# --- nazwy -----------------------------------------------------------------------------------

def host_label(hostname):
    short = (hostname or '').split('.')[0].strip().lower()
    return HOST_ALIASES.get(short, re.sub(r'[^A-Za-z0-9_.+-]', '-', short).upper() or 'HOST')


def check_pool_name(name):
    if not isinstance(name, str) or not POOL_NAME_RE.fullmatch(name):
        raise ValueError(f'Niedozwolona nazwa magazynu {name!r} (litera lub cyfra na początku, dalej litery, '
                         'cyfry oraz _ . + -; maks. 64 znaki).')
    if name in PROTECTED_POOLS:
        raise ValueError(f'{name} to nazwa chronionej puli — wybierz inną.')
    return name


def suggest_name(hostname, size_gib=None):
    """Np. HVM2-Local-1200 — unikalna per host. ValueError, gdy nazwa hosta nic nie mówi
    (pusta, localhost, host, ubuntu) — wtedy nazwę trzeba podać (--pool-name / pytanie w menu)."""
    short = (hostname or '').split('.')[0].strip().lower()
    if short in GENERIC_HOSTNAMES:
        raise ValueError(f'Nazwa hosta {hostname!r} nie pozwala zaproponować unikalnej nazwy magazynu — '
                         'podaj ją jawnie (--pool-name).')
    return check_pool_name(f'{host_label(hostname)}-Local' + (f'-{size_gib}' if size_gib else ''))


def try_suggest(hostname, size_gib=None):
    try:
        return suggest_name(hostname, size_gib)
    except ValueError:
        return None


def suggest_datastore_name(hostname, size_bytes=None):
    """Zgodność z 0.4: rozmiar w bajtach."""
    return suggest_name(hostname, round(size_bytes / GIB) if size_bytes else None)


# --- odczyt ------------------------------------------------------------------------------------

def available():
    return runner.run(virsh('uri'), check=False).returncode == 0


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


def pool_names():
    p = runner.run(virsh('pool-list', '--all', '--name'), check=False)
    if p.returncode:
        raise RuntimeError(f'Nie można pobrać listy pul libvirt: {(p.stderr or "").strip() or f"kod {p.returncode}"}')
    return [n.strip() for n in p.stdout.splitlines() if n.strip()]


def pool_xml(name):
    x = runner.run(virsh('pool-dumpxml', '--pool', name), check=False)
    return parse_pool_xml(x.stdout) if x.returncode == 0 else None


def pool_info(name):
    info = runner.run(virsh('pool-info', '--pool', name), check=False)
    return parse_kv(info.stdout) if info.returncode == 0 else {}


def list_pools():
    """Wszystkie pule: name, type, target, state, autostart, capacity, available, protected, broken."""
    out = []
    for name in pool_names():
        parsed = pool_xml(name)
        ptype, target = parsed or ('?', '')
        info = pool_info(name)
        out.append({'name': name, 'type': ptype, 'target': target, 'state': info.get('State', '?'),
                    'autostart': info.get('Autostart', '?'), 'capacity': info.get('Capacity', '?'),
                    'available': info.get('Available', '?'), 'protected': name in PROTECTED_POOLS,
                    'broken': parsed is None})
    return out


def same_dir(a, b):
    return bool(a) and bool(b) and runner.canon(a) == runner.canon(b)


def _under(path, parent):
    c, p = runner.canon(path), runner.canon(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def overlaps(a, b):
    return bool(a) and bool(b) and (_under(a, b) or _under(b, a))


def discover_pools(mount):
    """Istniejące pule typu dir, których katalog docelowy to dokładnie `mount` (tylko odczyt)."""
    found = []
    for name in pool_names():
        parsed = pool_xml(name)
        if parsed is None:
            ui.warn(f'nie można odczytać definicji puli {name} — pominięto.')
            continue
        ptype, path = parsed
        if ptype == 'dir' and same_dir(path, mount):
            found.append({'name': name, 'path': path, 'info': pool_info(name)})
    return found


def protected_overlap(mount, pools):
    """Chroniona pula, której katalog pokrywa się z `mount` (równy, nad nim lub pod nim), albo None."""
    return next((p for p in pools if p['protected'] and overlaps(mount, p['target'])), None)


def pool_decision(name, mount, pools):
    """Co zrobić z pulą magazynu: ('create', None) | ('existing', pula) | ('reuse', pula) | ('refuse', powód).

    Odmowa, gdy: nie da się odczytać definicji którejkolwiek puli; katalog pokrywa się z pulą chronioną;
    jakakolwiek inna pula (dowolnego typu) wskazuje na ten katalog, katalog nad nim lub pod nim — poza
    pulą typu dir wskazującą dokładnie na ten katalog (tę można użyć ponownie)."""
    broken = [p['name'] for p in pools if p.get('broken')]
    if broken:
        return 'refuse', (f'Nie można odczytać definicji puli: {", ".join(broken)} (virsh pool-dumpxml). '
                          'Sprawdź je ręcznie — program nie zakłada, że ich nie ma.')
    p = protected_overlap(mount, pools)
    if p:
        return 'refuse', (f'{mount} pokrywa się z katalogiem chronionej puli {p["name"]} ({p["target"]}) — '
                          'program nie zmienia chronionych pul ani ich katalogów.')
    if name in PROTECTED_POOLS:
        return 'refuse', f'{name} to pula chroniona — program jej nie zmienia.'
    reusable = [x for x in pools if x['type'] == 'dir' and same_dir(x['target'], mount)]
    clash = [x for x in pools if x not in reusable and overlaps(x['target'], mount)]
    if clash:
        x = clash[0]
        return 'refuse', (f'Pula {x["name"]} (typ {x["type"]}, katalog {x["target"]}) pokrywa się z {mount} — '
                          'odmowa, żeby nie zdublować ani nie zagnieździć pul.')
    by_name = next((x for x in pools if x['name'] == name), None)
    if by_name:
        if by_name in reusable:
            return 'existing', by_name
        return 'refuse', (f'Pula {name} już istnieje i wskazuje na {by_name["target"] or "?"} (typ {by_name["type"]}) '
                          '— nie zostanie zmieniona ani zdublowana. Wybierz inną nazwę magazynu.')
    if reusable:
        return 'reuse', reusable[0]
    return 'create', None


# --- maszyny wirtualne --------------------------------------------------------------------------

def domains(running_only=False):
    """Nazwy maszyn (domyślnie WSZYSTKICH, także zatrzymanych). [] gdy virsh nie jest zainstalowany,
    None gdy libvirt nie odpowiada (nie da się sprawdzić)."""
    p = runner.run(virsh('list', '--name', '--state-running' if running_only else '--all'), check=False)
    if p.returncode == 127:
        return []
    if p.returncode:
        return None
    return [n.strip() for n in p.stdout.splitlines() if n.strip()]


def domain_sources(dom):
    p = runner.run(virsh('domblklist', '--details', '--domain', dom), check=False)
    out = []
    for line in p.stdout.splitlines() if p.returncode == 0 else []:
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0] != 'Type' and not parts[0].startswith('-') and parts[3].strip() != '-':
            out.append(parts[3].strip())
    return out


def vm_sources():
    """[(VM, ścieżka dysku)] wszystkich zdefiniowanych maszyn albo None, gdy nie da się sprawdzić."""
    doms = domains()
    return None if doms is None else [(d, src) for d in doms for src in domain_sources(d)]


def vm_usage(mount=None, devices=(), sources=None):
    """Maszyny, których dyski leżą pod `mount` albo są jednym z `devices`; None = nie da się sprawdzić."""
    sources = vm_sources() if sources is None else sources
    if sources is None:
        return None
    return [(dom, src) for dom, src in sources
            if (mount and src.startswith('/') and _under(src, mount))
            or any(d and (src == d or lvm.same_device(src, d)) for d in devices)]
