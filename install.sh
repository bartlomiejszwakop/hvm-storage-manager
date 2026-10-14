#!/usr/bin/env bash
# Instalator HVM Storage Manager.
#   curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
# Zmienne: HVM_REF=<gałąź|tag> (domyślnie main), HVM_UNINSTALL=1 (odinstalowanie).
set -euo pipefail

REPO="bartlomiejszwakop/hvm-storage-manager"
REF="${HVM_REF:-main}"
TARGET="/usr/local/sbin/hvm-storage-manager"
URL="https://raw.githubusercontent.com/${REPO}/${REF}/hvm_storage_manager.py"

die() { echo "BŁĄD: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "uruchom jako root (… | sudo bash)."

if [[ "${HVM_UNINSTALL:-}" == "1" ]]; then
    rm -f "$TARGET" "${TARGET}.prev"
    echo "Usunięto $TARGET. Log /var/log/hvm-storage-manager.log i kopie fstab pozostawiono."
    exit 0
fi

command -v python3 >/dev/null || die "brak python3."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "wymagany Python 3.8 lub nowszy."
command -v curl >/dev/null || die "brak curl."

missing=()
for c in lvcreate vgs lvs mkfs.ext4 blkid findmnt mount systemctl udevadm virsh; do
    command -v "$c" >/dev/null || missing+=("$c")
done
((${#missing[@]} == 0)) || echo "UWAGA: brak poleceń: ${missing[*]} (pakiety: lvm2, e2fsprogs, util-linux, systemd, libvirt-clients)." >&2

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
echo "Pobieranie ${URL}"
curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp" "$URL" || die "nie udało się pobrać programu."

# Sprawdzenie, czy pobrany plik to poprawny program, zanim trafi do systemu.
head -n 1 "$tmp" | grep -q '^#!/usr/bin/env python3' || die "pobrany plik nie wygląda na program."
python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$tmp" \
    || die "pobrany plik zawiera błędy składni."

old=""
[[ -x "$TARGET" ]] && old="$("$TARGET" --version 2>/dev/null | awk '{print $NF}')" || true
if [[ -f "$TARGET" ]]; then
    cp -p "$TARGET" "${TARGET}.prev"  # poprzednia wersja na wypadek potrzeby powrotu
fi
install -m 0755 -o root -g root "$tmp" "$TARGET"
new="$("$TARGET" --version | awk '{print $NF}')"
if [[ -n "$old" ]]; then
    echo "Zaktualizowano: ${old} → ${new} ($TARGET, poprzednia wersja: ${TARGET}.prev)"
else
    echo "Zainstalowano: ${new} → $TARGET"
fi
echo "Uruchom: sudo hvm-storage-manager   (podgląd bez zmian: sudo hvm-storage-manager --dry-run)"
