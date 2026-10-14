#!/usr/bin/env bash
# Test integracyjny hvm_storage_manager.py na urządzeniu loop.
#
# TYLKO dla jednorazowej maszyny laboratoryjnej (NIE produkcja!). Uruchamiać jako root:
#   sudo HVM_TEST_I_UNDERSTAND=1 bash tests/integration_loopdev.sh
#
# Co robi: tworzy rzadki plik 6G, loop, PV i VG hvmtest-vg-<PID>, uruchamia narzędzie
# w trybie --dry-run (nic nie może się zmienić), potem naprawdę (LV + ext4 + mount + fstab),
# a następnie wywołania, które muszą zostać odrzucone. Pułapka EXIT sprząta wszystko
# i przywraca /etc/fstab z kopii wykonanej na starcie.
set -euo pipefail

if [[ "${HVM_TEST_I_UNDERSTAND:-}" != "1" ]]; then
    echo "Odmowa: ten test modyfikuje LVM i /etc/fstab. Uruchamiaj tylko na jednorazowej VM:" >&2
    echo "  sudo HVM_TEST_I_UNDERSTAND=1 bash $0" >&2
    exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
    echo "Odmowa: wymagany root." >&2
    exit 2
fi
for cmd in python3 losetup truncate pvcreate vgcreate vgremove pvremove lvs lvremove blkid findmnt mountpoint sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "Brak polecenia: $cmd" >&2; exit 2; }
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL="$REPO_DIR/hvm_storage_manager.py"
[[ -f "$TOOL" ]] || { echo "Nie znaleziono $TOOL" >&2; exit 2; }

ID=$$
VG="hvmtest-vg-$ID"
LV="t1"
LVPATH="/dev/$VG/$LV"
MNT="/srv/hvmtest$ID"
MNT_BUSY="/srv/hvmtest${ID}busy"
MNT_OTHER="/srv/hvmtest${ID}other"
WORK=""
LOOP=""
FSTAB_BACKUP=""
LOGF=""
FAILURES=0

cleanup() {
    local rc=$?
    set +e
    echo "=== SPRZĄTANIE ==="
    for m in "$MNT" "$MNT_OTHER" "$MNT_BUSY"; do
        if mountpoint -q "$m" 2>/dev/null; then
            umount "$m" || umount -l "$m"
        fi
    done
    if [[ -n "$FSTAB_BACKUP" && -f "$FSTAB_BACKUP" ]]; then
        if ! cmp -s "$FSTAB_BACKUP" /etc/fstab; then
            echo "Przywracanie /etc/fstab z $FSTAB_BACKUP"
            cp --preserve=mode,ownership "$FSTAB_BACKUP" /etc/fstab
        fi
        systemctl daemon-reload 2>/dev/null
    fi
    # kopie fstab utworzone przez narzędzie w czasie testu (shutil.copy2 zachowuje mtime,
    # więc porównujemy listy nazw, a nie czasy)
    if [[ -n "$WORK" && -f "$WORK/backups.before" ]]; then
        new_backups | while IFS= read -r f; do echo "Usuwanie $f"; rm -f -- "$f"; done
    fi
    if vgs "$VG" >/dev/null 2>&1; then
        lvremove -y "$VG" >/dev/null
        vgremove -y "$VG" >/dev/null
    fi
    if [[ -n "$LOOP" ]]; then
        pvremove -y "$LOOP" >/dev/null 2>&1
        if command -v lvmdevices >/dev/null 2>&1; then
            lvmdevices --deldev "$LOOP" >/dev/null 2>&1
        fi
        losetup -d "$LOOP"
    fi
    rm -f "$MNT_BUSY/keep.txt"
    for m in "$MNT" "$MNT_OTHER" "$MNT_BUSY"; do
        [[ -d "$m" ]] && rmdir "$m"
    done
    if [[ -n "$LOGF" && -f "$LOGF" && $rc -ne 0 ]]; then
        echo "--- log narzędzia ($LOGF) ---"
        cat "$LOGF"
    fi
    [[ -n "$WORK" ]] && rm -rf "$WORK"
    if [[ $rc -eq 0 ]]; then echo "WYNIK: OK"; else echo "WYNIK: BŁĄD (kod $rc)" >&2; fi
    exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT TERM

fail() { echo "FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }
pass() { echo "ok:   $*"; }

lv_exists() { lvs --noheadings -o lv_name "$VG/$1" >/dev/null 2>&1; }
fstab_sum() { sha256sum /etc/fstab | cut -d' ' -f1; }
list_backups() { find /etc -maxdepth 1 -name 'fstab.backup-hvm-*' -print | LC_ALL=C sort; }
new_backups() { list_backups | LC_ALL=C comm -13 "$WORK/backups.before" -; }
backup_count() { new_backups | wc -l; }

tool() { python3 "$TOOL" --log-file "$LOGF" "$@"; }

# Oczekuje odmowy (kod != 0) i braku jakichkolwiek zmian.
expect_refusal() {
    local desc="$1"; shift
    local before_sum before_backups rc=0
    before_sum="$(fstab_sum)"
    before_backups="$(backup_count)"
    tool "$@" || rc=$?
    if [[ $rc -eq 0 ]]; then fail "$desc: narzędzie NIE odmówiło"; else pass "$desc: odmowa (kod $rc)"; fi
    [[ "$(fstab_sum)" == "$before_sum" ]] && pass "$desc: fstab bez zmian" || fail "$desc: fstab zmieniony"
    [[ "$(backup_count)" == "$before_backups" ]] || fail "$desc: powstała kopia fstab (czyli zaczęto zmiany)"
}

# --- przygotowanie -----------------------------------------------------------------------
WORK="$(mktemp -d /tmp/hvmtest.XXXXXX)"
LOGF="$WORK/hvm.log"
FSTAB_BACKUP="$WORK/fstab.orig"
cp -p /etc/fstab "$FSTAB_BACKUP"
list_backups > "$WORK/backups.before"

for m in "$MNT" "$MNT_BUSY" "$MNT_OTHER"; do
    if [[ -e "$m" ]]; then echo "$m już istnieje — przerwano." >&2; exit 2; fi
done
if vgs "$VG" >/dev/null 2>&1; then echo "VG $VG już istnieje — przerwano." >&2; exit 2; fi

truncate -s 6G "$WORK/disk.img"
LOOP="$(losetup --find --show "$WORK/disk.img")"
echo "Urządzenie loop: $LOOP"
pvcreate -q "$LOOP"
vgcreate -q "$VG" "$LOOP"
FSTAB_SUM0="$(fstab_sum)"

# --- 1. dry-run: nic nie może się zmienić ---------------------------------------------------
echo "=== 1. DRY-RUN ==="
tool --dry-run create --vg "$VG" --name "$LV" --size 2 --mount "$MNT" --yes \
    || fail "dry-run zakończony błędem"
lv_exists "$LV" && fail "dry-run utworzył LV" || pass "dry-run: brak LV"
[[ -e "$MNT" ]] && fail "dry-run utworzył $MNT" || pass "dry-run: brak katalogu"
[[ "$(fstab_sum)" == "$FSTAB_SUM0" ]] && pass "dry-run: fstab bez zmian" || fail "dry-run zmienił fstab"
[[ "$(backup_count)" -eq 0 ]] && pass "dry-run: brak kopii fstab" || fail "dry-run utworzył kopię fstab"

# --- 2. prawdziwe utworzenie ---------------------------------------------------------------
echo "=== 2. UTWORZENIE ==="
tool create --vg "$VG" --name "$LV" --size 2 --mount "$MNT" --yes || fail "utworzenie zakończone błędem"
lv_exists "$LV" && pass "LV $LVPATH istnieje" || fail "brak LV $LVPATH"
mountpoint -q "$MNT" && pass "$MNT zamontowany" || fail "$MNT nie jest zamontowany"
UUID="$(blkid -p -s UUID -o value "$LVPATH" 2>/dev/null || true)"
[[ -n "$UUID" ]] && pass "UUID: $UUID" || fail "brak UUID na $LVPATH"
[[ "$(blkid -p -s TYPE -o value "$LVPATH" 2>/dev/null || true)" == "ext4" ]] && pass "ext4" || fail "to nie ext4"
if [[ -n "$UUID" ]] && grep -qxF "UUID=$UUID $MNT ext4 defaults,nofail 0 2" /etc/fstab; then
    pass "wpis w fstab"
else
    fail "brak oczekiwanego wpisu w fstab"
fi
[[ "$(grep -c -F " $MNT " /etc/fstab || true)" -eq 1 ]] && pass "dokładnie jeden wpis $MNT" || fail "liczba wpisów $MNT != 1"
[[ "$(findmnt -n -o UUID --mountpoint "$MNT" || true)" == "$UUID" ]] && pass "findmnt UUID zgodny" || fail "findmnt UUID niezgodny"
findmnt --verify --tab-file /etc/fstab >/dev/null && pass "findmnt --verify" || fail "findmnt --verify zgłasza błędy"
[[ "$(backup_count)" -eq 1 ]] && pass "utworzono kopię fstab" || fail "oczekiwano jednej kopii fstab"
# kopia narzędzia musi być identyczna z oryginałem
NEWBK="$(new_backups | head -n1)"
[[ -n "$NEWBK" ]] && cmp -s "$NEWBK" "$FSTAB_BACKUP" && pass "kopia fstab = oryginał" || fail "kopia fstab różni się od oryginału"
LV_COUNT="$(lvs --noheadings -o lv_name "$VG" | wc -l)"

# --- 3. wywołania, które muszą zostać odrzucone -----------------------------------------------
echo "=== 3. ODMOWY ==="
expect_refusal "istniejąca nazwa LV" create --vg "$VG" --name "$LV" --size 1 --mount "$MNT_OTHER" --yes
mkdir "$MNT_BUSY"
echo "nie ruszać" > "$MNT_BUSY/keep.txt"
expect_refusal "niepusty katalog" create --vg "$VG" --name t2 --size 1 --mount "$MNT_BUSY" --yes
[[ "$(cat "$MNT_BUSY/keep.txt")" == "nie ruszać" ]] && pass "plik w niepustym katalogu nietknięty" || fail "plik zmieniony"
expect_refusal "punkt montowania już użyty" create --vg "$VG" --name t3 --size 1 --mount "$MNT" --yes
expect_refusal "za mało miejsca" create --vg "$VG" --name t4 --size 100 --mount "$MNT_OTHER" --yes
expect_refusal "lokalizacja spoza allowlisty" create --vg "$VG" --name t5 --size 1 --mount /etc/hvmtest$ID --yes
expect_refusal "zastrzeżona nazwa" create --vg "$VG" --name x_rimage --size 1 --mount "$MNT_OTHER" --yes
expect_refusal "nieistniejąca VG" create --vg "nope-vg-$ID" --name t6 --size 1 --mount "$MNT_OTHER" --yes
[[ "$(lvs --noheadings -o lv_name "$VG" | wc -l)" -eq "$LV_COUNT" ]] && pass "żadnych nowych LV" || fail "powstały nowe LV"
[[ -e "$MNT_OTHER" ]] && fail "$MNT_OTHER został utworzony" || pass "$MNT_OTHER nie istnieje"
[[ -e "/etc/hvmtest$ID" ]] && fail "/etc/hvmtest$ID został utworzony" || pass "/etc nietknięte"
mountpoint -q "$MNT" && pass "$MNT nadal zamontowany" || fail "$MNT odmontowany przez odmowę"

echo
if [[ $FAILURES -ne 0 ]]; then
    echo "Niepowodzenia: $FAILURES" >&2
    exit 1
fi
echo "Wszystkie sprawdzenia przeszły."
