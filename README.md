# HVM Storage Manager

Polski, prosty kreator konsolowy (v0.2.0) do tworzenia **nowych** wolumenów LVM/ext4 na Ubuntu HPE HVM: tworzy LV, formatuje go jako ext4, dopisuje wpis do `/etc/fstab` i montuje. Nie przebudowuje ani nie usuwa istniejących wolumenów.

> Nie jest oficjalnym produktem HPE.

## Wymagania

- Python 3.8+ (tylko biblioteka standardowa, jeden plik).
- `lvm2`, `e2fsprogs`, `util-linux` (`findmnt`, `blkid`, `lsblk`, `mount`), `systemd` (`systemctl`, `udevadm`), libvirt (`virsh` — tylko do podglądu w menu).
- Uprawnienia root (poza trybem `--dry-run`).

## Instalacja (jedna komenda)

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
```

Instalator sprawdza Pythona 3.8+ i wymagane polecenia, pobiera program, weryfikuje jego składnię i instaluje go jako `/usr/local/sbin/hvm-storage-manager`. Potem wystarczy:

```bash
sudo hvm-storage-manager
```

- Konkretna wersja (tag lub gałąź): `curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_REF=v0.2.0 bash`
- Odinstalowanie: `curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_UNINSTALL=1 bash`
- Ostrożniej (najpierw przejrzyj skrypt): `curl -fsSLO https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh && less install.sh && sudo bash install.sh`

Bez instalacji: `git clone https://github.com/bartlomiejszwakop/hvm-storage-manager.git` i `sudo python3 hvm_storage_manager.py`.

## Uruchomienie

Menu interaktywne (po instalacji `sudo hvm-storage-manager`, z repozytorium):

```bash
sudo python3 hvm_storage_manager.py
```

Opcje menu: `1` dyski i miejsce, `2` utwórz nowy datastore LVM, `3` pule libvirt, `4` sprawdzenie fstab i mountów, `5` maszyny VM, `0` wyjście. W kreatorze Enter przyjmuje wartość domyślną, `q` wraca do menu. Przed zmianami program pokazuje plan i wymaga wpisania frazy `TWORZ NOWY WOLUMEN`.

Podgląd bez zmian (polecenia zmieniające system są tylko wypisywane; nie wymaga root):

```bash
python3 hvm_storage_manager.py --dry-run
```

Tryb nieinteraktywny:

```bash
sudo python3 hvm_storage_manager.py create --vg ubuntu-vg --name vm-storage --size 1200 --mount /vm-storage [--yes]
```

- `--size` — liczba całkowita GiB (1–1000000); w VG musi zostać co najmniej 1 GiB rezerwy.
- `--yes` — pomija pytanie o frazę potwierdzającą oraz pytanie o rollback (wycofanie następuje automatycznie).
- `--dry-run` można podać przed lub po `create`.
- `--log-file PATH` — plik logu (domyślnie `/var/log/hvm-storage-manager.log`).
- `--version` — wypisuje wersję.

Kody wyjścia: `0` sukces, `1` błąd walidacji, nieudana operacja, anulowanie, brak root lub działająca inna instancja, `2` błędne argumenty, `130` przerwanie Ctrl+C.

## Jak działa tworzenie

Najpierw pełna walidacja (bez zmian w systemie): nazwy VG/LV zgodne z regułami LVM, rozmiar i wolne miejsce, punkt montowania z allowlisty, bez dowiązań symbolicznych w ścieżce, katalog pusty i jeszcze niezamontowany, brak wpisu w fstab, VG istnieje, LV jeszcze nie istnieje, obecny `/etc/fstab` przechodzi `findmnt --verify`.

Następnie kroki transakcji:

1. `lvcreate -L <N>G -n <nazwa> <vg>`.
2. `udevadm settle` i `blkid -p` — przerwanie, jeśli na nowym LV jest jakakolwiek sygnatura.
3. `mkfs.ext4 -F -m 1 -L <nazwa[:16]>` na nowym LV.
4. Utworzenie brakujących katalogów punktu montowania (0755).
5. Odczyt UUID i ponowne sprawdzenie, czy fstab nie zmienił się w międzyczasie.
6. Kopia `/etc/fstab` → `/etc/fstab.backup-hvm-RRRRMMDD-GGMMSS`.
7. Atomowe dopisanie `UUID=<uuid> <punkt> ext4 defaults,nofail 0 2`, `systemctl daemon-reload`.
8. `findmnt --verify`, potem `mount <punkt>` i sprawdzenie, że zamontowany jest nowy wolumen.

Przy błędzie (lub Ctrl+C) rollback cofa wykonane kroki w odwrotnej kolejności: `umount`, przywrócenie fstab z kopii + `daemon-reload`, usunięcie utworzonych (pustych) katalogów, `lvremove` nowego LV. W trybie interaktywnym program pyta `Czy wycofać zmiany? [T/n]`; przy odmowie lub nieudanym kroku wypisuje polecenia do ręcznego wykonania.

Po sukcesie: `df -h` nowego punktu i wskazówka dla Morpheus.

## Dozwolone punkty montowania

Lista `ALLOWED_MOUNT_PATTERNS` w `hvm_storage_manager.py` (wyrażenia regularne dopasowywane do całej znormalizowanej ścieżki):

| Wzorzec | Przykład |
|---|---|
| `^/vm[a-z0-9_-]*$` | `/vm-storage`, `/vm_data`, `/vm2` |
| `^/srv/[a-z0-9_-]+$` | `/srv/vm-storage` |
| `^/data/[a-z0-9_-]+$` | `/data/vm-storage` |
| `^/var/lib/libvirt/images/[a-z0-9_-]+$` | `/var/lib/libvirt/images/pula1` |

Inne ścieżki są odrzucane. Aby dopuścić nową lokalizację, dopisz wzorzec do listy (tylko małe litery, cyfry, `_` i `-`).

## Bezpieczeństwo

- Skrypt nie usuwa istniejących LV ani VM; odmawia pracy na istniejącym LV, niepustym lub już zamontowanym katalogu i ścieżce z dowiązaniem symbolicznym.
- Operacja tworzenia **modyfikuje dysk**, a `mkfs.ext4` formatuje nowy LV. Przed użyciem wykonaj backup i przetestuj w labie (najpierw `--dry-run`).
- W razie błędu działa automatyczny rollback, ale jeśli nie wszystko uda się wycofać, sprawdź stan ręcznie i nie restartuj hosta, dopóki `findmnt --verify` nie przejdzie bez błędów. Nie uruchamiaj kreatora ponownie w ciemno.
- Przed zmianą fstab program sprawdza go `findmnt --verify` i odmawia pracy, jeśli zawiera błędy. Kopia: `/etc/fstab.backup-hvm-RRRRMMDD-GGMMSS`.
- Blokada `/run/hvm-storage-manager.lock` uniemożliwia równoległe uruchomienie dwóch instancji.
- Wszystkie polecenia i kroki są logowane do `/var/log/hvm-storage-manager.log` (lub `--log-file`).
- Nigdy nie używaj tej aplikacji do przygotowania współdzielonego storage bez osobnego projektu i weryfikacji wsparcia HPE.
- Nie instaluj i nie uruchamiaj na produkcyjnym hypervisorze bez przeglądu kodu i zgody administratora.

## Testy

Testy jednostkowe (nie zmieniają systemu):

```bash
python -m unittest discover -s tests -v
```

Test integracyjny na urządzeniach loop: `tests/integration_loopdev.sh`. Uruchamiaj **wyłącznie na jednorazowej maszynie labowej**; wymaga ustawienia `HVM_TEST_I_UNDERSTAND=1`:

```bash
sudo HVM_TEST_I_UNDERSTAND=1 bash tests/integration_loopdev.sh
```

## Morpheus

Po utworzeniu wolumenu dodaj go w Morpheus jako **Directory Pool**: Infrastructure → Clusters → Storage → Datastores → ADD → Directory Pool, ścieżka = punkt montowania (np. `/vm-storage`).
