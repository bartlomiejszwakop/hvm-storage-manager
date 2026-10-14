"""FakeHost — stanowy symulator hosta HVM dla testów (działa także na Windows).

Odpowiada na polecenia przechodzące przez hvm.runner.run() (podmienia runner._exec), więc
allowlista, dry-run i logowanie są testowane naprawdę. Stan: grupy i wolumeny LVM, węzły /dev
(słownik + prawdziwe katalogi w tymczasowym ROOT), montowania, /etc/fstab (prawdziwy plik
w ROOT), systemy plików, pule libvirt, maszyny wirtualne i ich dyski, nazwa hosta.
"""
import contextlib
import io
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid as uuidlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hvm import lvm, mount as mnt, runner, safety  # noqa: E402

GIB = 1024 ** 3
BOOT_UUID = '5d2b3c4e-1111-4222-8333-944455556666'
EFI_UUID = 'A1B2-C3D4'
ROOT_UUID = '0aa1bb2c-3333-4444-8555-666677778888'
BASE_FSTAB = ('# /etc/fstab: static file system information.\n'
              '/dev/disk/by-id/dm-uuid-LVM-root / ext4 defaults 0 1\n'
              f'UUID={BOOT_UUID} /boot ext4 defaults 0 1\n'
              f'UUID={EFI_UUID} /boot/efi vfat umask=0077 0 1\n'
              '/swap.img none swap sw 0 0\n')
VIRSH_POOL_MUT = ('pool-define-as', 'pool-start', 'pool-autostart', 'pool-refresh', 'pool-destroy', 'pool-undefine')
ORIG_NODE_INFO = lvm.node_info


def cp(args, rc=0, out='', err=''):
    return subprocess.CompletedProcess(list(args), rc, out, err)


def virsh_parts(a):
    """(podpolecenie, {opcja: wartość}, błąd) — ta sama ścisła składnia co safety.parse_virsh."""
    try:
        sub, values = safety.parse_virsh(list(a))
        return sub, values, None
    except safety.CommandRefused as exc:
        return safety.virsh_sub(list(a)), {}, str(exc)


def pool_arg(a):
    """Nazwa puli WYŁĄCZNIE z --pool/--name (forma --opcja=wartość nie jest nazwą — jest błędem)."""
    _, values, _ = virsh_parts(a)
    return values.get('--pool') or values.get('--name') or ''


class FakeHost:
    def __init__(self, hostname='hvm2', vg='ubuntu-vg', vg_size=1800 * GIB, vg_free=1387 * GIB, libvirt=True,
                 ip='10.10.0.12', system_disk='sda', data_disk='sdb', protected_targets=None):
        self.hostname, self.ip, self.libvirt, self.vg = hostname, ip, libvirt, vg
        self.root = tempfile.mkdtemp(prefix='hvmfake-').replace('\\', '/')
        for d in ('etc', 'dev', 'var/lib/libvirt/images'):
            os.makedirs(f'{self.root}/{d}', exist_ok=True)
        self.write_fstab(BASE_FSTAB)
        with open(f'{self.root}/etc/os-release', 'w', encoding='utf-8', newline='\n') as f:
            f.write('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\n')
        self.vgs = {vg: {'size': vg_size, 'free': vg_free, 'pvs': [f'/dev/{system_disk}3']}}
        self.disks = [{'name': system_disk, 'size': vg_size + 3 * GIB,
                       'parts': [{'name': f'{system_disk}1', 'size': GIB, 'fstype': 'vfat'},
                                 {'name': f'{system_disk}2', 'size': 2 * GIB, 'fstype': 'ext4'},
                                 {'name': f'{system_disk}3', 'size': vg_size, 'fstype': 'LVM2_member', 'pv': vg}]}]
        if data_disk:
            self.disks.append({'name': data_disk, 'size': 2000 * GIB, 'parts': []})
        self.lvs, self.nodes, self.minor = {}, {}, 0
        self.mounts = [{'target': '/boot/efi', 'source': f'/dev/{system_disk}1', 'fstype': 'vfat', 'size': GIB,
                        'uuid': EFI_UUID},
                       {'target': '/boot', 'source': f'/dev/{system_disk}2', 'fstype': 'ext4', 'size': 2 * GIB,
                        'uuid': BOOT_UUID}]
        self._add_lv(vg, 'ubuntu-lv', 100, fs='ext4', uuid=ROOT_UUID)
        self.vgs[vg]['free'] = vg_free  # ubuntu-lv już jest wliczony w zajęte miejsce
        self.mounts.insert(0, {'target': '/', 'source': lvm.dev_paths(vg, 'ubuntu-lv')[1], 'fstype': 'ext4',
                               'size': 98 * GIB, 'uuid': ROOT_UUID})
        targets = protected_targets or {'local': '/var/lib/libvirt/images',
                                        'morpheus-images': '/var/morpheus/kvm/images',
                                        'morpheus-cloud-init': '/var/morpheus/kvm/cloud-init'}
        self.pools = {n: {'type': 'dir', 'target': t, 'state': 'running', 'autostart': 'yes'} for n, t in targets.items()}
        self.domains = {'morpheus-manager': {'running': True,
                                             'disks': ['/var/lib/libvirt/images/morpheus-manager.qcow2']}}
        self.calls, self.fail, self.interrupt, self.busy, self.extra_uuid_devices = [], set(), None, {}, {}
        self.defined, self.allow_lvremove, self.verify_calls, self._lv_fs_at_call = set(), False, 0, {}

    # --- przygotowanie stanu ---------------------------------------------------------------

    def write_fstab(self, text):
        with open(f'{self.root}/etc/fstab', 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write(text)

    def fstab(self):
        with open(f'{self.root}/etc/fstab', encoding='utf-8', errors='surrogateescape', newline='') as f:
            return f.read()

    def path(self, p):
        return self.root + p

    def _new_uuid(self):
        return str(uuidlib.uuid4())

    def _add_lv(self, vg, name, size_gib, fs=None, uuid=None, links=True):
        self.minor += 1
        lvpath, mapper = lvm.dev_paths(vg, name)
        dm = f'/dev/dm-{self.minor}'
        self.lvs[(vg, name)] = {'size': size_gib * GIB, 'fs': fs, 'uuid': uuid, 'minor': self.minor, 'active': True}
        self.nodes[dm] = ('block', f'253:{self.minor}')
        self.nodes[mapper] = ('link', f'../dm-{self.minor}')
        if links and not os.path.lexists(self.path(lvpath)):
            self.nodes[lvpath] = ('link', f'../dm-{self.minor}')
        self.vgs[vg]['free'] -= size_gib * GIB
        return self.lvs[(vg, name)]

    def add_storage(self, name='vm-storage', size=1200, mount='/vm-storage', fs='ext4', fstab=True, mounted=True,
                    pool='auto', pool_state='running', autostart='yes'):
        """Istniejący magazyn (np. z poprzedniego uruchomienia)."""
        uuid = self._new_uuid() if fs == 'ext4' else None
        self._add_lv(self.vg, name, size, fs=fs, uuid=uuid)
        os.makedirs(self.path(mount), exist_ok=True)
        if fstab and uuid:
            self.write_fstab(self.fstab() + mnt.fstab_line(uuid, mount))
        if mounted:
            self.mounts.append({'target': mount, 'source': lvm.dev_paths(self.vg, name)[1], 'fstype': 'ext4',
                                'size': int(size * GIB * 0.98), 'uuid': uuid})
        if pool:
            pool = f'{self.hostname.upper()}-Local-{size}' if pool == 'auto' else pool
            self.pools[pool] = {'type': 'dir', 'target': mount, 'state': pool_state, 'autostart': autostart}
        return uuid

    # --- instalacja ------------------------------------------------------------------------

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            for p in (mock.patch.object(runner, '_exec', self.exec), mock.patch.object(runner, 'ROOT', self.root),
                      mock.patch.object(runner, 'DRY_RUN', False), mock.patch.object(runner, 'SHOW_COMMANDS', False),
                      mock.patch.object(runner, 'is_root', return_value=True),
                      mock.patch.object(lvm, 'node_info', self.node_info),
                      mock.patch.object(lvm, 'block_majmin', self.block_majmin),
                      mock.patch.object(lvm, 'processes_using', self.processes_using),
                      mock.patch('socket.gethostname', return_value=self.hostname)):
                stack.enter_context(p)
            safety.ARMED.clear()
            yield self
            safety.ARMED.clear()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def node_info(self, path):
        return self.nodes[path] if path in self.nodes else ORIG_NODE_INFO(path)

    def block_majmin(self, path):
        for _ in range(5):
            node = self.nodes.get(path)
            if not node:
                return None
            if node[0] == 'block':
                return node[1]
            path = posixpath.normpath(posixpath.join(posixpath.dirname(path), node[1]))
        return None

    def processes_using(self, path):
        return sorted(pid for p, pids in self.busy.items() if p == path or p.startswith(path + '/') for pid in pids)

    # --- zapytania w testach -------------------------------------------------------------------

    def lv(self, name='vm-storage'):
        return self.lvs.get((self.vg, name))

    def mount_of(self, target):
        return next((m for m in reversed(self.mounts) if m['target'] == target), None)

    def names(self):
        return [c[0] for c in self.calls]

    def keys(self):
        return [self.key(c) for c in self.calls]

    def index(self, key):
        return self.keys().index(key)

    def mutating_calls(self):
        return [c for c in self.calls if safety.is_mutating(c)]

    def forbidden_calls(self):
        bad = []
        for a in self.calls:
            if a[0] == 'lvremove' and not self.allow_lvremove:
                bad.append(a)
            if a[0] == 'wipefs' and '-n' not in a:
                bad.append(a)
            if a[0] == 'mkfs.ext4' and self._lv_fs_at_call.get(id(a)):
                bad.append(a)  # formatowanie wolumenu, który miał już sygnaturę
            if a[0] == 'virsh':
                sub, _, error = virsh_parts(a)
                name = pool_arg(a)
                if error or 'pool-delete' in a or any(x.startswith('-') and '=' in x for x in a):
                    bad.append(a)
                elif sub in ('pool-destroy', 'pool-undefine') and name not in self.defined:
                    bad.append(a)
                elif sub in VIRSH_POOL_MUT and (name in safety.PROTECTED_POOLS
                                                or any(p in a for p in safety.PROTECTED_POOLS)):
                    bad.append(a)
        return bad

    # --- symulacja poleceń -----------------------------------------------------------------------

    @staticmethod
    def key(a):
        if a[0] == 'virsh':
            return f'virsh {safety.virsh_sub(list(a))}'
        return f'{a[0]} {a[1]}' if a[0] in ('udevadm', 'systemctl', 'dmsetup') and len(a) > 1 else a[0]

    def exec(self, args, mutating=False):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == 'mkfs.ext4':
            lv = self._lv_by_dev(args[-1])
            self._lv_fs_at_call[id(args)] = bool(lv and lv['fs'])
        key = self.key(args)
        if self.interrupt == key:
            self.interrupt = None
            raise KeyboardInterrupt
        if key in self.fail:
            return cp(args, 32 if args[0] == 'mount' else 5, '', 'symulowany błąd')
        handler = getattr(self, 'c_' + args[0].replace('.', '_'), None)
        if handler is None:
            return cp(args, 127, '', f'{args[0]}: not found')
        rc, out, err = handler(args)
        return cp(args, rc, out, err)

    def _lv_by_dev(self, dev):
        for (vg, name), lv in self.lvs.items():
            if dev in lvm.dev_paths(vg, name) or dev == f'/dev/dm-{lv["minor"]}':
                return lv
        return None

    def _open(self, vg, name):
        paths = lvm.dev_paths(vg, name)
        return (any(m['source'] in paths for m in self.mounts)
                or any(d['running'] and set(d['disks']) & set(paths) for d in self.domains.values()))

    def _report(self, key, rows):
        return 0, json.dumps({'report': [{key: rows}]}), ''

    def c_lsblk(self, a):
        def lvnode(vg, name, lv, parent):
            dev = lvm.dev_paths(vg, name)[1]
            mps = [m['target'] for m in self.mounts if m['source'] == dev] or [None]
            return {'name': lvm.dm_name(vg, name), 'path': dev, 'size': lv['size'], 'type': 'lvm', 'fstype': lv['fs'],
                    'mountpoints': mps, 'pkname': parent}
        out = []
        for d in self.disks:
            parts = []
            for p in d['parts']:
                mps = [m['target'] for m in self.mounts if m['source'] == f'/dev/{p["name"]}'] or [None]
                kids = [lvnode(vg, n, lv, p['name']) for (vg, n), lv in self.lvs.items() if vg == p.get('pv')]
                node = {'name': p['name'], 'path': f'/dev/{p["name"]}', 'size': p['size'], 'type': 'part',
                        'fstype': p['fstype'], 'mountpoints': mps, 'pkname': d['name']}
                if kids:
                    node['children'] = kids
                parts.append(node)
            node = {'name': d['name'], 'path': f'/dev/{d["name"]}', 'size': d['size'], 'type': 'disk', 'fstype': None,
                    'mountpoints': [None], 'pkname': None}
            if parts:
                node['children'] = parts
            out.append(node)
        return 0, json.dumps({'blockdevices': out}), ''

    def c_vgs(self, a):
        if '--reportformat' not in a:
            return 0, '  VG  #PV #LV\n' + ''.join(f'  {n}\n' for n in self.vgs), ''
        return self._report('vg', [{'vg_name': n, 'vg_size': str(v['size']), 'vg_free': str(v['free'])}
                                   for n, v in self.vgs.items()])

    def c_pvs(self, a):
        if '--reportformat' not in a:
            return 0, '  PV  VG\n', ''
        return self._report('pv', [{'pv_name': pv, 'vg_name': n, 'pv_size': str(v['size'])}
                                   for n, v in self.vgs.items() for pv in v['pvs']])

    def c_lvs(self, a):
        if '--reportformat' not in a:
            return 0, ''.join(f'  {n} {vg}\n' for (vg, n) in self.lvs), ''
        rows = []
        for (vg, name), lv in self.lvs.items():
            attr = list(lv.get('attr0', '-') + 'wi-------')
            attr[4] = 'a' if lv['active'] else '-'
            attr[5] = 'o' if lv['active'] and self._open(vg, name) else '-'
            rows.append({'lv_name': name, 'vg_name': vg, 'lv_attr': ''.join(attr), 'lv_size': str(lv['size']),
                         'lv_path': f'/dev/{vg}/{name}', 'lv_dm_path': lvm.dev_paths(vg, name)[1],
                         'lv_kernel_major': '253' if lv['active'] else '-1',
                         'lv_kernel_minor': str(lv['minor']) if lv['active'] else '-1'})
        return self._report('lv', rows)

    def c_lvcreate(self, a):
        size, name, vg = int(a[a.index('-L') + 1].rstrip('G')), a[a.index('-n') + 1], a[-1]
        if vg not in self.vgs or (vg, name) in self.lvs:
            return 5, '', f'Volume group "{vg}" problem'
        if size * GIB > self.vgs[vg]['free']:
            return 5, '', 'Insufficient free space'
        lvpath = lvm.dev_paths(vg, name)[0]
        if os.path.lexists(self.path(lvpath)):  # incydent HVM2: katalog w miejscu dowiązania
            if 'lvcreate_partial' in self.fail:
                self._add_lv(vg, name, size)
            return 5, '', f'{lvpath}: not found: device not cleared\n  Aborting. Failed to wipe start of new LV.'
        self._add_lv(vg, name, size)
        return (5, '', 'symulowany błąd częściowy') if 'lvcreate_partial' in self.fail else (0, '', '')

    def c_lvremove(self, a):
        target = a[-1].replace('/dev/', '')
        vg, name = target.split('/')[-2:]
        lv = self.lvs.pop((vg, name), None)
        if not lv:
            return 5, '', 'not found'
        for p in lvm.dev_paths(vg, name) + (f'/dev/dm-{lv["minor"]}',):
            self.nodes.pop(p, None)
        self.vgs[vg]['free'] += lv['size']
        return 0, '', ''

    def c_vgscan(self, a):
        for (vg, name), lv in self.lvs.items():
            lvpath = lvm.dev_paths(vg, name)[0]
            if lv['active'] and lvpath not in self.nodes and not os.path.lexists(self.path(lvpath)):
                self.nodes[lvpath] = ('link', f'../dm-{lv["minor"]}')
        return 0, '', ''

    def c_udevadm(self, a):
        return 0, ('E: DEVNAME=/dev/dm-x\n' if a[1] == 'info' else ''), ''

    def c_dmsetup(self, a):
        if a[1] == 'ls':
            return 0, ''.join(f'{lvm.dm_name(vg, n)}\t(253:{lv["minor"]})\n' for (vg, n), lv in self.lvs.items()), ''
        return 0, 'Name: x\n', ''

    def c_wipefs(self, a):
        lv = self._lv_by_dev(a[-1])
        if lv is None:
            return 1, '', f'{a[-1]}: No such device'
        return 0, (lv['fs'] + '\n') if lv['fs'] else '', ''

    def c_blkid(self, a):
        if '-t' in a:
            uuid = a[a.index('-t') + 1].split('=', 1)[1]
            devs = [lvm.dev_paths(vg, n)[1] for (vg, n), lv in self.lvs.items() if lv['uuid'] == uuid]
            devs += self.extra_uuid_devices.get(uuid, [])
            return (0, ''.join(d + '\n' for d in devs), '') if devs else (2, '', '')
        lv = self._lv_by_dev(a[-1])
        if not lv or not lv['fs']:
            return 2, '', ''
        if lv['fs'] == 'gpt':
            return 0, 'PTTYPE=gpt\n', ''
        uuid = 'garbage' if 'uuid' in self.fail else lv['uuid']
        return 0, f'TYPE={lv["fs"]}\nUUID={uuid}\n', ''

    def c_mkfs_ext4(self, a):
        lv = self._lv_by_dev(a[-1])
        if lv is None:
            return 1, '', 'no device'
        lv['fs'], lv['uuid'] = 'ext4', self._new_uuid()
        return 0, '', ''

    def c_findmnt(self, a):
        if '--verify' in a:
            self.verify_calls += 1
            if 'verify_after' in self.fail and self.verify_calls >= 2:
                return 1, '[E] unreachable source', ''
            known = {lv['uuid'] for lv in self.lvs.values() if lv['uuid']} | {BOOT_UUID, EFI_UUID}
            with open(self.path(a[-1]), encoding='utf-8', errors='surrogateescape') as f:
                for fields in mnt._fstab_fields(f.read()):
                    if fields[0].startswith('UUID=') and fields[0][5:].strip('"') not in known:
                        return 1, f'[E] unreachable source: {fields[0]}', ''
            return 0, '', ''
        if '-J' in a:
            return 0, json.dumps({'filesystems': self.mounts}), ''
        target = a[-1]
        m = self.mount_of(target)
        return (0, f'TARGET SOURCE\n{target} {m["source"]}\n', '') if m else (1, '', '')

    def c_mount(self, a):
        target = a[-1]
        if not os.path.isdir(self.path(target)):
            return 32, '', f'mount: {target}: mount point does not exist.'
        if 'mount_wrong' in self.fail:
            self.mounts.append({'target': target, 'source': '/dev/sdz1', 'fstype': 'ext4', 'size': GIB, 'uuid': ''})
            return 0, '', ''
        for fields in mnt._fstab_fields(self.fstab()):
            if posixpath.normpath(fields[1]) == target and fields[0].startswith('UUID='):
                uuid = fields[0][5:]
                for (vg, name), lv in self.lvs.items():
                    if lv['uuid'] == uuid and lv['fs'] == 'ext4':
                        self.mounts.append({'target': target, 'source': lvm.dev_paths(vg, name)[1], 'fstype': 'ext4',
                                            'size': int(lv['size'] * 0.98), 'uuid': uuid})
                        return 0, '', ''
        return 32, '', f'mount: {target}: can\'t find in /etc/fstab or device missing.'

    def c_umount(self, a):
        m = self.mount_of(a[-1])
        if not m:
            return 32, '', 'not mounted'
        self.mounts.remove(m)
        return 0, '', ''

    def c_systemctl(self, a):
        return 0, '', ''

    def c_df(self, a):
        return 0, 'Filesystem Size Used Avail Use% Mounted on\n', ''

    def c_ip(self, a):
        return 0, json.dumps([{'ifname': 'lo', 'addr_info': [{'local': '127.0.0.1', 'scope': 'host'}]},
                              {'ifname': 'eno1', 'addr_info': [{'local': self.ip, 'scope': 'global'}]}]), ''

    def c_hostname(self, a):
        return 0, f'{self.ip}\n', ''

    def c_virsh(self, a):
        sub, opts, error = virsh_parts(a)
        if error:
            return 1, '', f'error: {error}'
        if not self.libvirt:
            return 1, '', 'error: failed to connect to the hypervisor'
        name = opts.get('--pool') or opts.get('--name') or ''
        pool = self.pools.get(name)
        if sub == 'uri':
            return 0, 'qemu:///system\n', ''
        if sub == 'version':
            return 0, 'Running hypervisor: QEMU 8.2.2\n', ''
        if sub == 'list':
            return 0, ''.join(f'{n}\n' for n, d in self.domains.items() if d['running'] or '--all' in a), ''
        if sub == 'domblklist':
            d = self.domains.get(opts['--domain'])
            rows = ''.join(f' file   disk     vd{chr(97 + i)}      {src}\n' for i, src in enumerate(d['disks']))
            return 0, ' Type   Device   Target   Source\n' + '-' * 50 + '\n' + rows, ''
        if sub == 'pool-list':
            if '--name' in a:
                return 0, ''.join(f'{n}\n' for n in sorted(self.pools)), ''
            return 0, ' Name State Autostart\n' + ''.join(f' {n} {p["state"]} {p["autostart"]}\n'
                                                        for n, p in sorted(self.pools.items())), ''
        if sub == 'pool-define-as':
            if name in self.pools:
                return 1, '', f'error: pool {name} already exists'
            self.pools[name] = {'type': opts['--type'], 'target': opts['--target'], 'state': 'inactive',
                                'autostart': 'no'}
            self.defined.add(name)
            return 0, f'Pool {name} defined\n', ''
        if pool is None:
            return 1, '', f'error: failed to get pool \'{name}\''
        if sub == 'pool-dumpxml':
            if pool.get('broken'):
                return 1, '', 'error: internal error'
            return 0, (f"<pool type='{pool['type']}'><name>{name}</name><target><path>{pool['target']}</path>"
                       '</target></pool>'), ''
        if sub == 'pool-info':
            cap = '1.17 TiB' if pool['state'] == 'running' else '0.00 B'
            return 0, (f'Name:           {name}\nState:          {pool["state"]}\nPersistent:     yes\n'
                       f'Autostart:      {pool["autostart"]}\nCapacity:       {cap}\nAvailable:      {cap}\n'), ''
        if sub == 'pool-start':
            if not os.path.isdir(self.path(pool['target'])):
                return 1, '', 'error: cannot open directory'
            pool['state'] = 'running'
        elif sub == 'pool-autostart':
            pool['autostart'] = 'yes'
        elif sub == 'pool-refresh':
            return (0, '', '') if pool['state'] == 'running' else (1, '', 'error: pool is not active')
        elif sub == 'pool-destroy':
            pool['state'] = 'inactive'
        elif sub == 'pool-undefine':
            del self.pools[name]
        return 0, '', ''


class HostTestCase(unittest.TestCase):
    """Baza scenariuszy: po KAŻDYM teście sprawdza, że żaden host nie dostał zabronionego polecenia."""

    def setUp(self):
        self.hosts = []
        self.output = ''

    def host(self, **kw):
        h = FakeHost(**kw)
        self.hosts.append(h)
        self.addCleanup(h.cleanup)
        return h

    def tearDown(self):
        for h in self.hosts:
            self.assertEqual(h.forbidden_calls(), [], f'zabronione polecenia na {h.hostname}')

    def run_on(self, h, fn, answers=None):
        """Wywołuje fn() na hoście h; answers = odpowiedzi operatora (None = input() zabronione)."""
        with h.active(), contextlib.redirect_stdout(io.StringIO()) as out:
            if answers is None:
                with mock.patch('builtins.input', side_effect=AssertionError('input() nie powinno być wołane')):
                    result = fn()
            else:
                with mock.patch('builtins.input', side_effect=list(answers)) as inp:
                    result = fn()
                self.assertEqual(inp.call_count, len(answers), f'niewykorzystane odpowiedzi; wyjście:\n{out.getvalue()}')
        self.output = out.getvalue()
        return result
