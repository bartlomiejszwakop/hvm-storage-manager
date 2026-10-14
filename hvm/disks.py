"""Dyski i miejsce: lsblk/pvs/vgs (JSON) i klasyfikacja — dysk systemowy (/, /boot, /boot/efi, swap,
także przez łańcuch PV → VG → LV), w użyciu, używany przez VM, pusty; grupy LVM z wolnym miejscem.

Wersja 1.0 tworzy magazyny wyłącznie w wolnym miejscu istniejących grup LVM. Puste dyski są
pokazywane tylko informacyjnie (bez pvcreate/vgextend)."""
import json

from hvm import lvm, runner, ui

LSBLK = ['lsblk', '-J', '-b', '-o', 'NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,PKNAME']
SYSTEM_MOUNTS = ('/', '/boot', '/boot/efi', '[SWAP]')


def lsblk():
    p = runner.run(LSBLK)
    return json.loads(p.stdout or '{}').get('blockdevices', [])


def _walk(node):
    yield node
    for child in node.get('children') or []:
        yield from _walk(child)


def _mounts(node):
    points = node.get('mountpoints')
    if points is None:
        points = [node.get('mountpoint')]
    return [m for m in points if m]


def classify(tree, pvs, vgs, vm_sources=()):
    disks = []
    for dev in tree:
        if dev.get('type') != 'disk':
            continue
        nodes = list(_walk(dev))
        paths = [n.get('path') for n in nodes if n.get('path')]
        mounts = sorted({m for n in nodes for m in _mounts(n)})
        pv_vgs = sorted({p['vg_name'] for p in pvs if p['pv_name'] in paths and p['vg_name']})
        vms = sorted({dom for dom, src in vm_sources if any(lvm.same_device(src, p) for p in paths)})
        system = [m for m in mounts if m in SYSTEM_MOUNTS]
        if system:
            status, reason = 'system', f'dysk systemowy ({", ".join(system)}) — zablokowany'
        elif vms:
            status, reason = 'vm', f'używany przez VM: {", ".join(vms)} — zablokowany'
        elif len(nodes) > 1 or mounts or dev.get('fstype') or pv_vgs:
            what = [f'LVM: {", ".join(pv_vgs)}'] if pv_vgs else []
            what += [f'zamontowany: {", ".join(mounts)}'] if mounts else []
            status, reason = 'in-use', 'w użyciu' + (f' ({"; ".join(what)})' if what else ' (partycje / sygnatury)')
        else:
            status, reason = 'free', 'pusty dysk — tylko informacja (wersja 1.0 nie dołącza nowych dysków do LVM)'
        disks.append({'name': dev.get('name', '?'), 'path': dev.get('path', ''), 'size': lvm._bytes(dev.get('size') or 0),
                      'status': status, 'reason': reason, 'mounts': mounts, 'vgs': pv_vgs, 'vms': vms})
    system_vgs = {vg for d in disks if d['status'] == 'system' for vg in d['vgs']}
    rows = []
    for v in vgs:
        max_gib = lvm.max_size_gib(v['vg_free'])
        rows.append({'name': v['vg_name'], 'size': v['vg_size'], 'free': v['vg_free'], 'max_gib': max_gib,
                     'reserve_gib': lvm.reserve_for(v['vg_free']), 'system': v['vg_name'] in system_vgs,
                     'candidate': max_gib >= 1})
    return {'disks': disks, 'vgs': rows, 'candidates': [r for r in rows if r['candidate']]}


def scan(vm_sources=()):
    return classify(lsblk(), lvm.list_pvs(), lvm.list_vgs(), vm_sources or ())


def print_scan(data):
    gib = lvm.GIB
    print('\nDYSKI')
    for d in data['disks']:
        state = 'unknown' if d['status'] == 'free' else 'error'
        print(ui.state_line(state, f'{d["name"]} ({d["size"] // gib} GiB): {d["reason"]}'))
    print('\nGRUPY LVM (miejsce na nowy magazyn)')
    if not data['vgs']:
        print('  (brak grup LVM)')
    for v in data['vgs']:
        line = (f'{v["name"]}: wolne {v["free"] // gib} GiB, do wykorzystania {v["max_gib"]} GiB '
                f'(rezerwa {v["reserve_gib"]} GiB)' + (' — na dysku systemowym, istniejące wolumeny nietknięte'
                                                      if v['system'] else ''))
        print(ui.state_line('ok' if v['candidate'] else 'error', line))
