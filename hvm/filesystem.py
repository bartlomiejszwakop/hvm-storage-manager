"""System plików: wykrywanie sygnatur (wyłącznie odczyt: wipefs -n, blkid -p), mkfs.ext4, UUID."""
import re

from hvm import runner, safety

UUID_RE = re.compile(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def blkid_export(device):
    """(kod blkid -p, słownik KEY=VALUE). Kod 2 = brak sygnatur."""
    b = runner.run(['blkid', '-p', '-o', 'export', device], check=False)
    info = dict(line.split('=', 1) for line in b.stdout.splitlines() if '=' in line) if b.returncode == 0 else {}
    return b.returncode, info


def detect_signature(device):
    """('empty' | 'ext4' | 'other', opis) — wyłącznie odczyt."""
    w = runner.run(['wipefs', '-n', '--noheadings', '--output', 'TYPE', device], check=False)
    if w.returncode:
        raise RuntimeError(f'wipefs -n nie odczytał sygnatur {device}: {w.stderr.strip() or f"kod {w.returncode}"}')
    types = {t.strip() for t in w.stdout.splitlines() if t.strip()}
    rc, info = blkid_export(device)
    if rc not in (0, 2):
        return 'other', f'blkid -p kod {rc} (niejednoznaczne lub uszkodzone sygnatury)'
    found = types | {v for k, v in info.items() if k in ('TYPE', 'PTTYPE') and v}
    if not found and rc == 2:
        return 'empty', 'brak sygnatur (pusty wolumen)'
    if info.get('TYPE') == 'ext4' and 'PTTYPE' not in info and types <= {'ext4'}:
        return 'ext4', 'ext4' + (f' (UUID {info["UUID"]})' if info.get('UUID') else '')
    return 'other', ', '.join(sorted(found)) or 'nieznana sygnatura'


def read_uuid(device):
    uuid = blkid_export(device)[1].get('UUID', '').strip()
    if not UUID_RE.fullmatch(uuid):
        raise RuntimeError(f'Nie udało się odczytać UUID systemu plików na {device} ({uuid!r}).')
    return uuid


def mkfs_args(device, label):
    return ['mkfs.ext4', '-F', '-m', '1', '-L', label[:16], device]


def mkfs_ext4(device, label):
    """Formatuje WYŁĄCZNIE wolumen bez żadnej sygnatury: świeża kontrola tuż przed mkfs, potem
    jednorazowe uzbrojenie dokładnie tego polecenia (bez tego run() odmówi)."""
    sig, desc = detect_signature(device)
    if sig != 'empty':
        raise RuntimeError(f'Na {device} wykryto: {desc} — formatowanie przerwane.')
    args = mkfs_args(device, label)
    safety.arm(args)
    runner.run(args)


def devices_with_uuid(uuid):
    """Urządzenia z systemem plików o danym UUID (bez pamięci podręcznej blkid). >1 = migawka/klon."""
    p = runner.run(['blkid', '-c', '/dev/null', '-o', 'device', '-t', f'UUID={uuid}'], check=False)
    return sorted({line.strip() for line in p.stdout.splitlines() if line.strip()}) if p.returncode == 0 else []
