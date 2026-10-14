#!/usr/bin/env bash
# Instalator / aktualizator HVM Storage Manager (bez git — pobiera archiwum z GitHuba przez HTTPS).
#
#   curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
#
# Zmienne:
#   HVM_REF=<gałąź|tag>   wersja do instalacji (domyślnie main)
#   HVM_ROLLBACK=1        powrót do poprzedniej wersji (zamiana z /opt/hvm-storage-manager.prev)
#   HVM_UNINSTALL=1       odinstalowanie programu (logi, raporty, kopie fstab, LV i pule zostają)
#
# Układ po instalacji:
#   /opt/hvm-storage-manager/          main.py, hvm_storage_manager.py, hvm/*.py, README.md (root:root, 0755/0644)
#   /opt/hvm-storage-manager.prev/     poprzednia wersja (do powrotu)
#   /usr/local/sbin/hvm-storage-manager       wrapper: exec /usr/bin/python3 -E -s .../main.py "$@"
#   /usr/local/sbin/hvm-storage-manager.prev  poprzedni plik programu/wrappera (≤ 0.4: cały program)
#
# Cały skrypt składa się z funkcji, a main() jest wywoływane w ostatniej linii — przy `curl | bash`
# niepełne pobranie skryptu nie wykona niczego.
set -euo pipefail

REPO="bartlomiejszwakop/hvm-storage-manager"
PREFIX="/opt/hvm-storage-manager"
TARGET="/usr/local/sbin/hvm-storage-manager"
PYTHON="/usr/bin/python3"
APP_LOCK="/run/hvm-storage-manager.lock"
MAX_TARBALL_BYTES=$((20 * 1024 * 1024))
OWN=()      # w main(): -o root -g root
WORK=""
STAGE_CREATED=0
SWAPPED=0       # 1 = trwa podmiana wersji (błąd → przywrócenie poprzedniego stanu w cleanup)
DONE_STEPS=" "  # wykonane kroki podmiany: old-moved, new-in-place, target-saved
HAD_PREFIX=0
HAD_TARGET=0

die() { echo "BŁĄD: $*" >&2; exit 1; }
note() { echo "$*"; }

cleanup() {
    local rc=$?
    if [[ "$SWAPPED" == "1" ]]; then
        SWAPPED=0
        restore_after_failure || true
        echo "Instalacja przerwana — przywrócono poprzedni stan." >&2
    fi
    if [[ -n "$WORK" && -d "$WORK" ]]; then
        rm -rf -- "$WORK"
    fi
    if [[ "$STAGE_CREATED" == "1" && -d "${PREFIX}.new" ]]; then
        rm -rf -- "${PREFIX}.new"
    fi
    return "$rc"
}

# --- walidacja (funkcje testowalne po `source install.sh`) ---------------------------------------

# Nazwa gałęzi/tagu: tylko bezpieczne znaki, bez „..” i bez wiodącego „-” / „/”.
valid_ref() {
    local ref="$1"
    [[ -n "$ref" && ${#ref} -le 128 ]] || return 1
    [[ "$ref" =~ ^[A-Za-z0-9._][A-Za-z0-9._/+-]*$ ]] || return 1
    [[ "$ref" != *..* && "$ref" != */ ]] || return 1
}

# Sprawdza listę członków archiwum: bez ścieżek bezwzględnych, bez „..”, wyłącznie zwykłe pliki
# i katalogi (bez dowiązań, urządzeń itp.), wszystko w jednym katalogu głównym.
# Wypisuje nazwę katalogu głównego.
tar_top_dir() {
    local tarball="$1" names types name top="" first
    names="$(tar -tzf "$tarball")" || { echo "nie można odczytać archiwum" >&2; return 1; }
    types="$(tar -tvzf "$tarball")" || { echo "nie można odczytać archiwum" >&2; return 1; }
    [[ -n "$names" ]] || { echo "archiwum jest puste" >&2; return 1; }
    while IFS= read -r name; do
        [[ "$name" == "pax_global_header" ]] && continue
        if [[ -z "$name" || "$name" == /* || "$name" =~ (^|/)\.\.(/|$) ]]; then
            echo "niebezpieczna ścieżka w archiwum: ${name}" >&2
            return 1
        fi
        first="${name%%/*}"
        if [[ -z "$first" || "$first" == "." ]]; then
            echo "niepoprawny katalog główny w archiwum: ${name}" >&2
            return 1
        fi
        if [[ -z "$top" ]]; then
            top="$first"
        elif [[ "$first" != "$top" ]]; then
            echo "archiwum zawiera więcej niż jeden katalog główny (${top}, ${first})" >&2
            return 1
        fi
    done <<< "$names"
    while IFS= read -r name; do
        case "${name:0:1}" in
            -|d) ;;
            *) echo "archiwum zawiera element innego typu niż plik/katalog: ${name}" >&2; return 1 ;;
        esac
    done <<< "$types"
    [[ -n "$top" ]] || { echo "brak katalogu głównego w archiwum" >&2; return 1; }
    printf '%s\n' "$top"
}

# Rozpakowuje zweryfikowane archiwum do pustego katalogu $2; wypisuje ścieżkę katalogu źródeł.
extract_tarball() {
    local tarball="$1" dest="$2" top
    top="$(tar_top_dir "$tarball")" || return 1
    mkdir -p -- "$dest"
    tar -xzf "$tarball" -C "$dest" --no-same-owner --no-same-permissions || { echo "rozpakowanie nie powiodło się" >&2; return 1; }
    [[ -d "$dest/$top" && ! -L "$dest/$top" ]] || { echo "brak katalogu ${top} po rozpakowaniu" >&2; return 1; }
    printf '%s\n' "$dest/$top"
}

# Zwykły plik (nie dowiązanie).
regular_file() { [[ -f "$1" && ! -L "$1" ]]; }

# Sprawdza, czy katalog źródeł zawiera kompletny program o poprawnej składni Pythona.
validate_tree() {
    local src="$1" f
    local -a files=("$src/main.py" "$src/hvm_storage_manager.py")
    [[ -d "$src/hvm" && ! -L "$src/hvm" ]] || { echo "brak katalogu hvm/" >&2; return 1; }
    regular_file "$src/hvm/__init__.py" || { echo "brak pliku hvm/__init__.py" >&2; return 1; }
    for f in "$src"/hvm/*.py; do
        files+=("$f")
    done
    for f in "${files[@]}"; do
        regular_file "$f" || { echo "brak pliku lub nie jest zwykłym plikiem: ${f#"$src"/}" >&2; return 1; }
    done
    (cd / && "$PYTHON" -I -c 'import ast, sys; [ast.parse(open(f, encoding="utf-8").read(), f) for f in sys.argv[1:]]' \
        "${files[@]}" </dev/null) || { echo "pliki programu zawierają błędy składni" >&2; return 1; }
}

# Wersja z hvm/__init__.py (wyrażenie regularne — bez importowania kodu).
read_version() {
    local v
    v="$(sed -n "s/^__version__[[:space:]]*=[[:space:]]*['\"]\([0-9A-Za-z.+-]*\)['\"].*/\1/p" "$1/hvm/__init__.py" | head -n 1)"
    [[ -n "$v" ]] || { echo "nie można odczytać wersji z hvm/__init__.py" >&2; return 1; }
    printf '%s\n' "$v"
}

# Kopiuje wyłącznie pliki uruchomieniowe do $2 (katalogi 0755, pliki 0644).
stage_files() {
    local src="$1" dest="$2" f
    rm -rf -- "$dest"
    install -d -m 0755 "${OWN[@]}" "$dest" "$dest/hvm"
    for f in main.py hvm_storage_manager.py README.md LICENSE; do
        if [[ -e "$src/$f" || -L "$src/$f" ]]; then
            regular_file "$src/$f" || { echo "$f nie jest zwykłym plikiem" >&2; return 1; }
            install -m 0644 "${OWN[@]}" "$src/$f" "$dest/$f"
        fi
    done
    for f in "$src"/hvm/*.py; do
        regular_file "$f" || { echo "${f#"$src"/} nie jest zwykłym plikiem" >&2; return 1; }
        install -m 0644 "${OWN[@]}" "$f" "$dest/hvm/${f##*/}"
    done
}

# Wersja zainstalowanego programu (ostatnie pole `--version`) albo pusty napis.
installed_version() {
    local out
    [[ -x "$TARGET" ]] || return 0
    out="$("$TARGET" --version </dev/null 2>/dev/null)" || return 0
    printf '%s\n' "$out" | awk 'NF {v = $NF} END {print v}'
}

write_wrapper() {
    local tmp="${TARGET}.tmp.$$"
    cat > "$tmp" <<EOF
#!/bin/sh
# HVM Storage Manager — wrapper utworzony przez install.sh (program: ${PREFIX}).
exec ${PYTHON} -E -s ${PREFIX}/main.py "\$@"
EOF
    chmod 0755 "$tmp"
    if ((${#OWN[@]})); then chown root:root "$tmp"; fi
    mv -f -- "$tmp" "$TARGET"
}

# Zamiana dwóch ścieżek miejscami (pliki lub katalogi); brakująca strona jest dopuszczalna.
swap_paths() {
    local a="$1" b="$2" tmp="$1.swap.$$"
    if [[ -e "$a" && -e "$b" ]]; then
        mv -- "$a" "$tmp" && mv -- "$b" "$a" && mv -- "$tmp" "$b"
    elif [[ -e "$b" ]]; then
        mv -- "$b" "$a"
    elif [[ -e "$a" ]]; then
        mv -- "$a" "$b"
    fi
}

# Program nie może działać podczas instalacji (trzyma flock na $APP_LOCK).
check_not_running() {
    [[ -e "$APP_LOCK" ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0
    flock -n "$APP_LOCK" true </dev/null || die "HVM Storage Manager właśnie działa (blokada ${APP_LOCK}). Zakończ go i uruchom instalator ponownie."
}

check_python() {
    [[ -x "$PYTHON" ]] || die "brak ${PYTHON} (apt install python3)."
    "$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 8))' </dev/null || die "wymagany Python 3.8 lub nowszy."
}

warn_missing_commands() {
    local c
    local -a missing=()
    for c in lvcreate lvs vgs pvs mkfs.ext4 blkid wipefs lsblk findmnt mount systemctl udevadm dmsetup virsh ip tar; do
        command -v "$c" >/dev/null 2>&1 || missing+=("$c")
    done
    if ((${#missing[@]})); then
        echo "UWAGA: brak poleceń: ${missing[*]} (pakiety: lvm2, e2fsprogs, util-linux, systemd, dmsetup, libvirt-clients, iproute2, tar)." >&2
    fi
}

download() {
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-filesize "$MAX_TARBALL_BYTES" -o "$2" "$1"
}

# --- tryby ----------------------------------------------------------------------------------------

do_uninstall() {
    rm -rf -- "$PREFIX" "${PREFIX}.new" "${PREFIX}.prev"
    rm -f -- "$TARGET" "${TARGET}.prev"
    note "Usunięto ${PREFIX}{,.new,.prev}, ${TARGET} i ${TARGET}.prev."
    note "Bez zmian pozostały: log /var/log/hvm-storage-manager.log, raporty JSON, kopie /etc/fstab.backup-hvm-*,"
    note "wpisy w /etc/fstab, wolumeny LVM, punkty montowania i pule libvirt."
}

do_rollback() {
    local cur new
    [[ -d "${PREFIX}.prev" || -e "${TARGET}.prev" ]] || die "brak poprzedniej wersji (${PREFIX}.prev ani ${TARGET}.prev)."
    cur="$(installed_version)"
    if [[ -d "${PREFIX}.prev" ]]; then
        swap_paths "$PREFIX" "${PREFIX}.prev"
    fi
    if [[ -e "${TARGET}.prev" ]]; then
        swap_paths "$TARGET" "${TARGET}.prev"
    fi
    new="$(installed_version)"
    [[ -n "$new" ]] || die "po powrocie ${TARGET} --version nie działa — sprawdź ${PREFIX} i ${TARGET} ręcznie."
    note "Przywrócono poprzednią wersję: ${cur:-?} → ${new}"
    note "Ponowne HVM_ROLLBACK=1 wraca do wersji ${cur:-?}."
}

# Przywraca stan sprzed nieudanej instalacji (wywoływane z cleanup(); kroki wg $DONE_STEPS).
restore_after_failure() {
    if [[ "$DONE_STEPS" == *" new-in-place "* ]]; then
        rm -rf -- "$PREFIX"
    fi
    if [[ "$DONE_STEPS" == *" old-moved "* && ! -e "$PREFIX" ]]; then
        mv -- "${PREFIX}.prev" "$PREFIX"
    fi
    if [[ "$DONE_STEPS" == *" target-saved "* ]]; then
        cp -p -- "${TARGET}.prev" "$TARGET"
    elif [[ "$HAD_TARGET" == "0" ]]; then
        rm -f -- "$TARGET"
    fi
}

do_install() {
    local ref="$1" url tarball src version old new
    valid_ref "$ref" || die "niepoprawna wartość HVM_REF: ${ref}"
    command -v curl >/dev/null 2>&1 || die "brak curl."
    command -v tar >/dev/null 2>&1 || die "brak tar."
    check_python
    warn_missing_commands

    WORK="$(mktemp -d)"
    tarball="$WORK/src.tar.gz"
    url="https://codeload.github.com/${REPO}/tar.gz/${ref}"
    note "Pobieranie ${url}"
    download "$url" "$tarball" || die "nie udało się pobrać archiwum (sprawdź HVM_REF=${ref} i dostęp do github.com)."

    src="$(extract_tarball "$tarball" "$WORK/src")" || die "archiwum odrzucone."
    validate_tree "$src" || die "pobrany program jest niekompletny lub uszkodzony — nic nie zmieniono."
    version="$(read_version "$src")" || die "nie można ustalić wersji — nic nie zmieniono."

    STAGE_CREATED=1
    stage_files "$src" "${PREFIX}.new" || die "przygotowanie ${PREFIX}.new nie powiodło się — nic nie zmieniono."

    old="$(installed_version)"
    HAD_PREFIX=0
    HAD_TARGET=0
    if [[ -d "$PREFIX" ]]; then HAD_PREFIX=1; fi
    if [[ -e "$TARGET" ]]; then HAD_TARGET=1; fi

    # Podmiana: obecna wersja → .prev, nowa → na miejsce. Od tej chwili każdy błąd (także
    # przerwanie) przywraca poprzedni stan w cleanup().
    install -d -m 0755 "${OWN[@]}" "$(dirname "$TARGET")"
    if ((HAD_PREFIX)); then
        rm -rf -- "${PREFIX}.prev"
    fi
    SWAPPED=1
    if ((HAD_PREFIX)); then
        mv -- "$PREFIX" "${PREFIX}.prev"
        DONE_STEPS+="old-moved "
    fi
    mv -- "${PREFIX}.new" "$PREFIX"
    DONE_STEPS+="new-in-place "
    STAGE_CREATED=0
    if ((HAD_TARGET)); then
        cp -p -- "$TARGET" "${TARGET}.prev"
        DONE_STEPS+="target-saved "
    fi
    write_wrapper

    # Test dymny: nowy program musi się uruchomić i zgłosić oczekiwaną wersję.
    new="$(installed_version)"
    if [[ "$new" != "$version" ]]; then
        die "test po instalacji nie przeszedł (${TARGET} --version: '${new:-brak}', oczekiwano ${version})${old:+; wracam do wersji ${old}}."
    fi
    SWAPPED=0

    if [[ -n "$old" ]]; then
        note "Zaktualizowano: ${old} → ${new}"
    else
        note "Zainstalowano: ${new}"
    fi
    note "Program: ${PREFIX}  (polecenie: ${TARGET})"
    if [[ -d "${PREFIX}.prev" || -e "${TARGET}.prev" ]]; then
        note "Poprzednia wersja zachowana — powrót: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | sudo HVM_ROLLBACK=1 bash"
    fi
    note "Uruchom: sudo hvm-storage-manager   (podgląd bez zmian: sudo hvm-storage-manager --dry-run)"
}

main() {
    umask 022
    [[ "$(id -u)" -eq 0 ]] || die "uruchom jako root (… | sudo bash)."
    OWN=(-o root -g root)
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP
    check_not_running
    if [[ "${HVM_UNINSTALL:-}" == "1" ]]; then
        do_uninstall
    elif [[ "${HVM_ROLLBACK:-}" == "1" ]]; then
        do_rollback
    else
        do_install "${HVM_REF:-main}"
    fi
}

# `bash install.sh` i `curl … | bash` (BASH_SOURCE puste) uruchamiają main; `source install.sh` — tylko funkcje (testy).
if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
