# HVM Storage Manager

Polski, prosty kreator konsolowy (v0.4.0) do tworzenia **nowych** wolumenów LVM/ext4 na Ubuntu HPE HVM: tworzy LV, formatuje go jako ext4, dopisuje wpis do `/etc/fstab` i montuje. Umie też bezpiecznie **dokończyć** niepełną konfigurację istniejącego LV i odświeżyć pule libvirt, a audyt podpowiada unikalną nazwę datastore dla każdego hosta. Nie przebudowuje ani nie usuwa istniejących wolumenów.

> Nie jest oficjalnym produktem HPE.

## Wymagania

- Python 3.8+ (tylko biblioteka standardowa, jeden plik).
- `lvm2`, `e2fsprogs`, `util-linux` (`findmnt`, `blkid`, `lsblk`, `mount`), `systemd` (`systemctl`, `udevadm`), libvirt (`virsh` — podgląd oraz wykrywanie i odświeżanie pul).
- Uprawnienia root (poza trybem `--dry-run`).

## Instalacja (jedna komenda)

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
```

Instalator sprawdza Pythona 3.8+ i wymagane polecenia, pobiera program, weryfikuje jego składnię i instaluje go jako `/usr/local/sbin/hvm-storage-manager`. Potem wystarczy:

```bash
sudo hvm-storage-manager
```

- Konkretna wersja (tag lub gałąź): `curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_REF=v0.4.0 bash`
- Odinstalowanie: `curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_UNINSTALL=1 bash`
- Ostrożniej (najpierw przejrzyj skrypt): `curl -fsSLO https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh && less install.sh && sudo bash install.sh`

Bez instalacji: `git clone https://github.com/bartlomiejszwakop/hvm-storage-manager.git` i `sudo python3 hvm_storage_manager.py`.

## Aktualizacja

Aktualizacja to ta sama komenda co instalacja — pobiera najnowszą wersję z gałęzi `main` i podmienia program:

```bash
hvm-storage-manager --version          # obecna wersja
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
```

Instalator wypisze zmianę wersji (np. `Zaktualizowano: 0.3.0 → 0.4.0`) i zachowa poprzednią wersję jako `/usr/local/sbin/hvm-storage-manager.prev`.

- Aktualizacja do konkretnej wersji: `... | sudo HVM_REF=v0.4.0 bash` (lista wersji: zakładka *Tags* na GitHubie).
- Powrót do poprzedniej wersji: `sudo mv /usr/local/sbin/hvm-storage-manager.prev /usr/local/sbin/hvm-storage-manager`.
- Instalacja z `git clone`: `cd hvm-storage-manager && git pull`.
- Aktualizuj każdy host HVM osobno i nie w trakcie działającej operacji (program trzyma blokadę `/run/hvm-storage-manager.lock`).
- Log, kopie fstab i utworzone wolumeny nie są zmieniane przez aktualizację.

## Uruchomienie

Menu interaktywne (po instalacji `sudo hvm-storage-manager`, z repozytorium):

```bash
sudo python3 hvm_storage_manager.py
```

Opcje menu: `1` dyski i miejsce, `2` utwórz / dokończ datastore LVM, `3` pule libvirt, `4` sprawdzenie fstab i mountów, `5` maszyny VM, `6` wykryj i odśwież pule libvirt, `7` audyt lokalnego datastore na tym hoście, `0` wyjście. W kreatorze Enter przyjmuje wartość domyślną, `q` wraca do menu. Przed zmianami program pokazuje plan i wymaga wpisania frazy `TWORZ NOWY WOLUMEN`.

Podgląd bez zmian (polecenia zmieniające system są tylko wypisywane; nie wymaga root):

```bash
python3 hvm_storage_manager.py --dry-run
```

Tryb nieinteraktywny:

```bash
sudo hvm-storage-manager create --vg ubuntu-vg --name vm-storage --size 1200 --mount /vm-storage [--yes] [--fix-dev-conflict]
sudo hvm-storage-manager resume --vg ubuntu-vg --name vm-storage --mount /vm-storage [--yes] [--format-empty]
sudo hvm-storage-manager pools [--mount /vm-storage] [--refresh] [--yes]
sudo hvm-storage-manager audit [--mount /vm-storage]
```

- `--size` — liczba całkowita GiB (1–1000000); w VG musi zostać co najmniej 1 GiB rezerwy.
- `--yes` — pomija pytanie o frazę potwierdzającą oraz pytanie o rollback (wycofanie następuje automatycznie).
- `--fix-dev-conflict` — w trybie `--yes` pozwala usunąć **pusty** katalog blokujący ścieżkę `/dev/<vg>/<lv>` (interaktywnie program pyta o to osobno).
- `--format-empty` — w `resume --yes` pozwala sformatować LV, na którym nie ma żadnej sygnatury (interaktywnie trzeba wpisać `FORMATUJ <nazwa>`).
- `--refresh` — w `pools` odświeża dopasowane pule (`virsh pool-refresh`) po potwierdzeniu.
- `--dry-run` można podać przed lub po podkomendzie.
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

Po rollbacku program sprawdza stan systemu (LV, ścieżka w `/dev`, montowanie, fstab, katalogi) i dopiero wtedy pisze `Wszystkie zmiany wycofane — stan zweryfikowany.`. Jeśli coś zostało, wypisuje co i jak to usunąć ręcznie.

Po sukcesie: `df -h` nowego punktu i wskazówka dla Morpheus. Wpis ma opcję `nofail` — system uruchomi się bez datastore, więc przed startem VM sprawdź `findmnt -M <punkt>`.

### Konflikt w `/dev/<vg>/<lv>`

Przed `lvcreate` program sprawdza, czy `/dev/<vg>/<lv>` i `/dev/mapper/...` są wolne. Pusty katalog w tym miejscu (przyczyna błędu `device not cleared`) może usunąć po osobnym potwierdzeniu; niepusty katalog, plik, stare dowiązanie lub urządzenie bez LV w LVM — odmowa z diagnozą.

### Dokończenie (`resume`)

Gdy LV już istnieje (np. utworzony ręcznie albo po przerwanej operacji), `resume` wykonuje tylko brakujące kroki i pokazuje plan:

- LV bez sygnatur → `mkfs.ext4` po wpisaniu `FORMATUJ <nazwa>`; ext4 → formatowanie pominięte; inny system plików, tablica partycji, LUKS, PV → odmowa.
- Już zamontowany ten sam wolumen → montowanie pominięte; inne urządzenie pod punktem → odmowa.
- Poprawny wpis w fstab → bez zmian; inny lub zdublowany wpis → odmowa (przegląd ręczny); brak → atomowe dopisanie z kopią.

Ponowne uruchomienie nic nie zmienia (`Nic do zrobienia`). Rollback w `resume` nigdy nie usuwa LV.

### Pule libvirt (`pools`, opcja 6)

Sprawdza, że punkt jest osobnym systemem plików (a nie katalogiem na dysku systemowym), wyszukuje pule `dir` wskazujące dokładnie na ten katalog i pokazuje ich stan; odświeża je tylko po potwierdzeniu. Nie tworzy pul i nie korzysta z API Morpheus. Oba hosty mogą mieć pule o tej samej nazwie, ale na **innych lokalnych dyskach**.

### Audyt i nazwy datastore (`audit`, opcja 7)

Gdy dwa hosty mają lokalny datastore pod tą samą ścieżką i z tą samą nazwą, ich nazwy się nakładają. `audit` (tylko odczyt) pokazuje na danym hoście montowanie, miejsce, wolumeny LVM i pule libvirt, a następnie proponuje unikalną nazwę datastore: `<HOST>-Local-<rozmiar GiB>`, np. `HVM1-Local-1200` na `hvm1`/`node1` i `HVM2-Local-1200` na `hvm2`/`node2`. Inne hosty dostają swoją nazwę wielkimi literami (`HOST_ALIASES` w kodzie). Rozmiar jest odczytywany z urządzenia zamontowanego pod punktem.

Pule, których nazwa nie zawiera nazwy hosta, są oznaczone, bo na drugim hoście mogą mieć identyczną nazwę. Uruchom `audit` na każdym hoście i porównaj wyniki.

Ważne: nazwa puli libvirt i nazwa Datastore w Morpheus to dwie różne rzeczy — `virsh pool-define` ani zmiana nazwy puli nie tworzy datastore per host. Nie usuwaj obecnej puli klastrowej (może zawierać pliki VM). Przed migracją potwierdź w HPE, czy Twoja wersja Morpheus 9.x obsługuje datastore przypisany do konkretnego węzła.

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

- Skrypt nie usuwa istniejących LV ani VM; `create` odmawia pracy na istniejącym LV, niepustym lub już zamontowanym katalogu i ścieżce z dowiązaniem symbolicznym. Istniejący LV obsługuje wyłącznie `resume`, który nigdy nie formatuje wolumenu z jakąkolwiek sygnaturą.
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
