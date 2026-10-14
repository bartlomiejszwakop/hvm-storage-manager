# HVM Storage Manager

Konsolowy kreator (po polsku) do przygotowania **lokalnego magazynu maszyn wirtualnych** na hostach
**HPE Morpheus VM Essentials (HVM)** z Ubuntu 24.04. W wolnym miejscu istniejącej grupy LVM tworzy
wolumen, formatuje go jako ext4, dopisuje do `/etc/fstab`, montuje i rejestruje jako pulę `dir`
w lokalnym libvirt. Umie też bezpiecznie dokończyć lub naprawić niepełną konfigurację.

Wersja 1.0.0. Tylko biblioteka standardowa Pythona 3.8+.

> Projekt niezależny — **nie jest oficjalnym produktem HPE** i nie jest przez HPE wspierany.

## Szybki start

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
sudo hvm-storage-manager
```

1. Wybierz **1. Przygotuj nowy magazyn dla VM**.
2. Wybierz rozmiar (np. `1200 GiB — zalecane`) i zaakceptuj lub zmień nazwę magazynu (`HVM2-Local-1200`).
3. Przeczytaj podsumowanie, wybierz **[1] Wykonaj** i wpisz frazę `FORMATUJ vm-storage`.
4. Zarejestruj magazyn w panelu Morpheus (patrz [Trzy stany magazynu](#trzy-stany-magazynu)).

Najpierw możesz sprawdzić wszystko bez zmian: `sudo hvm-storage-manager --dry-run` albo `hvm-storage-manager --diagnose`.

## Wymagania

- Ubuntu 24.04 (host HVM), uprawnienia root (poza `--dry-run` i `--diagnose`).
- `/usr/bin/python3` w wersji 3.8 lub nowszej.
- Polecenia: `lvm2` (`lvcreate`, `lvs`, `vgs`, `pvs`, `vgscan`), `e2fsprogs` (`mkfs.ext4`), `util-linux`
  (`blkid`, `wipefs`, `lsblk`, `findmnt`, `mount`), `systemd` (`systemctl`, `udevadm`), `dmsetup`,
  `iproute2` (`ip`), `libvirt-clients` (`virsh`). Instalator ostrzega o brakujących.
- Wolne miejsce w istniejącej grupie LVM. Wersja 1.0 **nie** dołącza nowych dysków do LVM
  (puste dyski są pokazywane tylko informacyjnie).

## Instalacja

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
```

Instalator (nie wymaga `git`):

1. sprawdza root, Pythona 3.8+, `curl`, `tar` i ostrzega o brakujących poleceniach systemowych;
2. pobiera archiwum repozytorium wyłącznie przez HTTPS (`codeload.github.com`, TLS 1.2+) do katalogu tymczasowego;
3. odrzuca archiwum ze ścieżkami bezwzględnymi, `..`, dowiązaniami lub więcej niż jednym katalogiem głównym;
4. sprawdza, że są `main.py`, `hvm_storage_manager.py`, `hvm/__init__.py`, i że wszystkie pliki `.py` mają poprawną składnię; wersję czyta z `hvm/__init__.py` (bez uruchamiania kodu);
5. kopiuje tylko pliki uruchomieniowe do `/opt/hvm-storage-manager.new` (root:root, katalogi 0755, pliki 0644), a potem podmienia katalog;
6. zapisuje wrapper `/usr/local/sbin/hvm-storage-manager`;
7. uruchamia `hvm-storage-manager --version` jako test — gdy się nie powiedzie, **automatycznie przywraca poprzednią wersję** i kończy się błędem.

Układ po instalacji:

| Ścieżka | Zawartość |
|---|---|
| `/opt/hvm-storage-manager/` | `main.py`, `hvm_storage_manager.py`, `hvm/*.py`, `README.md` |
| `/opt/hvm-storage-manager.prev/` | poprzednia wersja (po aktualizacji) |
| `/usr/local/sbin/hvm-storage-manager` | wrapper: `exec /usr/bin/python3 -E -s /opt/hvm-storage-manager/main.py "$@"` |
| `/usr/local/sbin/hvm-storage-manager.prev` | poprzedni plik polecenia (przy aktualizacji z 0.4 — cały stary program) |

Konkretna wersja (tag lub gałąź): `... | sudo HVM_REF=v1.0.0 bash`.

Ostrożniej — najpierw przejrzyj skrypt:

```bash
curl -fsSLO https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh
less install.sh
sudo bash install.sh
```

Bez instalacji (z kopii repozytorium): `sudo python3 main.py` (lub dla zgodności `sudo python3 hvm_storage_manager.py`).

## Aktualizacja

Ta sama komenda co instalacja:

```bash
hvm-storage-manager --version
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo bash
```

Instalator wypisze `Zaktualizowano: 0.4.0 → 1.0.0` (lub `Zainstalowano: 1.0.0` przy pierwszej instalacji).
Dotychczasowa wersja trafia do `/opt/hvm-storage-manager.prev` i `/usr/local/sbin/hvm-storage-manager.prev`.

- Instalator odmawia pracy, gdy program właśnie działa (blokada `/run/hvm-storage-manager.lock`).
- Aktualizuj każdy host HVM osobno.
- Aktualizacja nie zmienia logów, raportów, kopii fstab, wolumenów ani pul.
- Aktualizacja z 0.4 (jeden plik w `/usr/local/sbin`) jest obsługiwana — stary plik zostaje jako `.prev`.

## Powrót do poprzedniej wersji

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_ROLLBACK=1 bash
```

Zamienia miejscami `/opt/hvm-storage-manager` ↔ `/opt/hvm-storage-manager.prev` oraz wrapper ↔ `.prev`
(każdą parę tylko wtedy, gdy kopia `.prev` istnieje). Ponowne `HVM_ROLLBACK=1` wraca do nowszej wersji.
Przechowywana jest tylko jedna poprzednia wersja. Po aktualizacji z 0.4 powrót przywraca stary jednoplikowy program.

## Odinstalowanie

```bash
curl -fsSL https://raw.githubusercontent.com/bartlomiejszwakop/hvm-storage-manager/main/install.sh | sudo HVM_UNINSTALL=1 bash
```

Usuwa `/opt/hvm-storage-manager{,.new,.prev}`, `/usr/local/sbin/hvm-storage-manager` i `.prev`.
**Pozostawia bez zmian**: log `/var/log/hvm-storage-manager.log`, raporty JSON, kopie `/etc/fstab.backup-hvm-*`,
wpisy w `/etc/fstab`, wolumeny LVM, punkty montowania i pule libvirt.

## Menu

```text
╔═════════════════════════════╗
║ HVM STORAGE MANAGER 1.0     ║
║ Zarządzanie dyskami HPE HVM ║
╚═════════════════════════════╝
Serwer: hvm2
Adres IP: 10.10.0.12
System: Ubuntu 24.04.1 LTS

STATUS MAGAZYNÓW
✓ Dysk systemowy: sda 1803 GiB
✓ Magazyn VM: 1200 GiB (ubuntu-vg/vm-storage)
✓ Zamontowany: /vm-storage
✓ Dostępny w libvirt: tak, pula HVM2-Local-1200 aktywna

1. Przygotuj nowy magazyn dla VM
2. Napraw lub dokończ konfigurację
3. Pokaż dostępne dyski i miejsce
4. Sprawdź magazyny Morpheus
5. Sprawdź stan serwera
6. Zaawansowane
0. Wyjście
```

Symbole: `✓` w porządku, `✗` problem / zablokowane, `?` nieznane lub niezweryfikowane, `○` pominięto, `↺` wycofano
(gdy terminal nie obsługuje UTF-8: `+ x ? o <`). W pytaniach Enter przyjmuje wartość domyślną, `q` wraca do menu.

| Opcja | Co robi |
|---|---|
| **1. Przygotuj nowy magazyn dla VM** | Kreator nowego magazynu `ubuntu-vg/vm-storage` → `/vm-storage` z pulą libvirt. Jeśli wolumen już istnieje, przechodzi do naprawy (nigdy nie formatuje go ponownie). |
| **2. Napraw lub dokończ konfigurację** | Diagnozuje istniejący magazyn (przypadki A–D) i wykonuje tylko brakujące kroki. |
| **3. Pokaż dostępne dyski i miejsce** | Dyski (systemowy / w użyciu / używany przez VM / pusty) i grupy LVM z miejscem do wykorzystania po odjęciu rezerwy. Tylko odczyt. |
| **4. Sprawdź magazyny Morpheus** | Trzy stany każdego magazynu na tym hoście; opcjonalnie `virsh pool-refresh` aktywnych pul (pytanie `[t/N]`). |
| **5. Sprawdź stan serwera** | Pełny raport: host, dyski, magazyny, pule libvirt (chronione oznaczone), `findmnt --verify`. Tylko odczyt. |
| **6. Zaawansowane** | Przełączniki „pokazuj polecenia” i dry-run; `lsblk`/`pvs`/`vgs`/`lvs`; `virsh pool-list`/`pool-info`; sprawdzenie `/etc/fstab`; diagnostyka device-mapper i udev; eksport raportu JSON; lokalizacja logu; narzędzia 0.4 (`create`, `resume`, `pools`, `audit`). |

## Co robi „Przygotuj nowy magazyn”

1. **Wykrycie** — nazwa hosta, dyski (dysk systemowy i dyski VM są zablokowane), grupy LVM z wolnym miejscem.
   Grupa: `ubuntu-vg`, a gdy jej nie ma — jedyna grupa z miejscem (przy kilku program pyta).
2. **Rozmiar** (zawsze w GiB = 1024³ B). W grupie zostaje rezerwa **max(1 GiB, 1% wolnego miejsca)**, np.:

   ```text
   Dostępne miejsce: 1387 GiB (grupa ubuntu-vg; po odjęciu rezerwy LVM 14 GiB: 1373 GiB)
   Ile miejsca przeznaczyć na maszyny?
   [1] 1000 GiB
   [2] 1200 GiB — zalecane
   [3] Maksymalna dostępna przestrzeń (1373 GiB)
   [4] Wpisz własną wartość
   [0] Wróć
   ```

   Zalecane jest 1200 GiB; gdy się nie mieści — okrągła wartość poniżej maksimum. Opcje, które się nie mieszczą, nie są pokazywane.
3. **Nazwa magazynu** = nazwa puli libvirt, domyślnie `<HOST>-Local-<GiB>`, np. `HVM2-Local-1200`
   (`hvm1`/`node1` → `HVM1`, `hvm2`/`node2` → `HVM2`, inne hosty — krótka nazwa WIELKIMI literami, znaki spoza
   `A-Z 0-9 _ . + -` zamieniane na `-`). Dzięki temu pule na dwóch hostach nie mają tej samej nazwy.
   Gdy nazwa hosta nic nie mówi (`localhost`, `host`, `ubuntu`, pusta), program nie proponuje nazwy — trzeba ją
   podać (`--pool-name` albo pytanie w menu). Nazwa: litera/cyfra na początku, dalej `A-Z a-z 0-9 _ . + -`, maks. 64 znaki.
4. **Walidacja bez zmian w systemie** — nazwy LVM, wolne miejsce, punkt montowania z allowlisty, brak dowiązań
   w ścieżce, katalog pusty i niezamontowany, brak wpisu w fstab, `findmnt --verify` bez błędów, brak kolizji
   z chronionymi pulami, libvirt odpowiada, nazwa puli wolna (albo ta sama pula już wskazuje na ten katalog —
   wtedy zostanie użyta bez duplikatu).
5. **Podsumowanie** i wybór `[1] Wykonaj / [2] Pokaż szczegóły techniczne / [0] Anuluj`, potem fraza `FORMATUJ vm-storage`.
6. **Wykonanie** — faza 1 (wolumen Linux) i faza 2 (pula libvirt):

   | Krok | Polecenie / działanie |
   |---|---|
   | Utworzenie LV | `lvcreate --yes -Wy -Zy -L <N>G -n vm-storage ubuntu-vg` — LVM czyści sygnatury i zeruje początek **wyłącznie nowego LV** (jego własnych, właśnie przydzielonych ekstentów) |
   | Urządzenie | `udevadm settle`; gdy brak `/dev/mapper/...` — `vgscan --mknodes` |
   | Kontrola pustości | `wipefs -n`, `blkid -p` — przerwanie, jeśli jest jakakolwiek sygnatura |
   | Formatowanie | tuż przed: ponowna kontrola sygnatur, `lv_attr` (zwykły LV, nieotwarty), brak `holders`, zgodność maj:min z LVM, żadna maszyna (także zatrzymana) nie używa wolumenu; potem `mkfs.ext4 -F -m 1 -L vm-storage /dev/mapper/ubuntu--vg-vm--storage` |
   | Katalog | `mkdir /vm-storage` (0755) |
   | fstab | kopia `/etc/fstab.backup-hvm-RRRRMMDD-GGMMSS`, atomowe dopisanie `UUID=<uuid> /vm-storage ext4 defaults,nofail 0 2`, `systemctl daemon-reload`, `findmnt --verify` |
   | Montowanie | `mount /vm-storage` + sprawdzenie, że zamontowany jest nowy wolumen |
   | Kontrola puli | osobny punkt montowania, pojemność ≥ 90% LV, `virsh uri` |
   | Pula | `virsh -c qemu:///system pool-define-as --name <nazwa> --type dir --target /vm-storage`, potem `pool-start`, `pool-autostart`, `pool-refresh` (każde z `--pool <nazwa>`) |

7. **Raport kroków** (`✓ wykonano / ✗ błąd / ○ pominięto / ↺ wycofano`), `df -h` i przypomnienie o rejestracji w Morpheus.

Menu zawsze używa nazw `vm-storage` i `/vm-storage`; inne wartości — przez `prepare --name/--mount/--vg` (patrz [CLI](#cli-dla-administratorów)).

## Naprawa (opcja 2 / `repair`)

Program lokalizuje magazyn (LV zamontowany w `/vm-storage`, jedyny LV `vm-storage` albo wartości domyślne),
pokazuje trzy stany, plan kroków i to, co zostanie pominięte. Po `[1] Wykonaj` robi **tylko brakujące kroki**.

| Przypadek | Objaw | Co robi program |
|---|---|---|
| **A** | `/dev/<vg>/<lv>` jest **katalogiem** zamiast dowiązania. Typowy objaw przy tworzeniu: `lvcreate` kończy się błędem `/dev/ubuntu-vg/vm-storage: not found: device not cleared` (incydent na HVM2). | Usuwa (`rmdir`) wyłącznie **pusty**, niezamontowany katalog, którego nie używa żaden proces — po osobnym pytaniu `[t/N]` (z `--yes` tylko z `--fix-dev-conflict`). Niepusty katalog, plik, stare dowiązanie lub węzeł urządzenia bez LV → odmowa z diagnozą. Gdy LV już istnieje, po usunięciu katalogu odtwarza dowiązanie (`vgscan --mknodes`). |
| **B** | LV istnieje, brak dowiązania `/dev/<vg>/<lv>`, `/dev/mapper/...` zgodny z metadanymi LVM. | Używa `/dev/mapper/...`, próbuje `vgscan --mknodes` (niepowodzenie nie przerywa naprawy). Niespójność device-mapper z LVM → odmowa. |
| **C** | LV istnieje, ale konfiguracja jest niepełna: brak ext4, brak wpisu w fstab, niezamontowany, brak puli / pula nieaktywna / bez autostartu. | Dokańcza brakujące kroki. Pusty LV (bez żadnych sygnatur) formatuje tylko po frazie `FORMATUJ <lv>` (z `--yes` tylko z `--format-empty <vg>/<lv>` wskazującym dokładnie ten LV). Obcy system plików, tablica partycji, LUKS, PV, inne urządzenie pod punktem montowania (sprawdzane po maj:min, nie tylko po UUID), inny/zdublowany wpis w fstab (także `/dev/...` wskazujący to samo urządzenie pod inną ścieżką), ten sam UUID na kilku urządzeniach (migawka/klon), LV zamontowany gdzie indziej lub otwarty, migawka/pula zamiast zwykłego LV, urządzenia zbudowane na LV (`holders`), pusty LV będący dyskiem dowolnej maszyny (także zatrzymanej) albo brak możliwości sprawdzenia tego (libvirt nie odpowiada) → odmowa. |
| **D** | Wszystko gotowe. | `✓ Magazyn jest już poprawnie skonfigurowany.` / `Nie trzeba wykonywać dodatkowych operacji.` — nic nie zmienia. |

Nieaktywny LV (`lv_attr`) → odmowa z podpowiedzią `lvchange -ay <vg>/<lv>`. Ponowne uruchomienie po udanej naprawie daje przypadek D.

## Trzy stany magazynu

Każdy magazyn (opcje 2, 4, 5 i `--diagnose`) jest opisany trzema niezależnymi stanami:

```text
HVM2-Local-1200
✓ Wolumen Linux gotowy
✓ Pula libvirt aktywna
? Rejestracja w panelu Morpheus niezweryfikowana
```

| Stan | Znaczenie |
|---|---|
| **Wolumen Linux** | LV istnieje i jest aktywny, ext4, zamontowany w punkcie montowania, poprawny wpis w fstab. |
| **Pula libvirt** | Pula `dir` wskazująca na katalog magazynu jest uruchomiona i ma autostart. |
| **Morpheus** | Zawsze `?` — program **nie łączy się z Morpheus API**, więc nie potwierdza, że magazyn jest widoczny w panelu. |

**Pula libvirt ≠ Datastore w Morpheus.** Pula to zasób lokalnego hosta; datastore w panelu to osobna rejestracja.
Po przygotowaniu magazynu dodaj go w Morpheus jako **Directory Pool**:
*Infrastructure → Clusters → Storage → Datastores → ADD → Directory Pool*, ścieżka = punkt montowania (`/vm-storage`).

Każdy host HVM ma własny, **lokalny** dysk — magazyny pod tą samą ścieżką na dwóch hostach to nie jest
współdzielony datastore. Przygotuj magazyn osobno na każdym hoście (każdy dostanie własną nazwę, np.
`HVM1-Local-1200`, `HVM2-Local-1200`). Nie usuwaj istniejących pul ani ich plików — mogą zawierać dyski VM.

## Bezpieczeństwo

Polityka wersji 1.0:

- **Diagnoza → plan → potwierdzenie → wykonanie.** Przed jakąkolwiek zmianą program pokazuje, co zrobi, i czeka na zgodę.
- **Nigdy nie formatuje istniejących danych.** Formatowany jest wyłącznie wolumen bez żadnej sygnatury
  (sprawdzane `wipefs -n` i `blkid -p` bezpośrednio przed `mkfs.ext4`), zawsze po frazie `FORMATUJ <lv>`
  (w `create` z 0.4 — po frazie `TWORZ NOWY WOLUMEN`). Istniejący LV nigdy nie jest tworzony ponownie.
- **Brak automatycznego usuwania**: program nie wywołuje automatycznie `lvremove`, nie uruchamia `wipefs`
  w trybie zapisu (tylko `-n`) i nigdy nie wywołuje `virsh pool-delete`.
- **Chronione pule** `local`, `morpheus-images`, `morpheus-cloud-init` — tylko odczyt. Punkt montowania w katalogu
  `/var/lib/libvirt/images` lub `/var/morpheus`, nad nim albo równy mu jest zawsze odrzucany (niezależnie od tego,
  czy libvirt odpowiada); dodatkowo — gdy libvirt odpowiada — także każdy inny katalog chronionej puli.
- **Pule**: odmowa, gdy jakakolwiek istniejąca pula (dowolnego typu) wskazuje na katalog magazynu, katalog nad nim
  lub pod nim (poza pulą `dir` dokładnie na tym katalogu — ta zostaje użyta ponownie), oraz gdy definicji którejś
  puli nie da się odczytać.
- **Allowlista poleceń**: program uruchamia (bez powłoki, z `PATH=/usr/sbin:/usr/bin:/sbin:/bin`, `LC_ALL=C`)
  wyłącznie: `lsblk pvs vgs lvs blkid findmnt df ip wipefs lvcreate lvremove mkfs.ext4 mount umount`,
  `hostname -I`, `vgscan --mknodes`, `dmsetup ls|info`, `udevadm settle|info`, `systemctl daemon-reload`
  i `virsh -c qemu:///system` z wybranymi podpoleceniami (`uri version list domblklist pool-list pool-info
  pool-dumpxml pool-define-as pool-start pool-autostart pool-refresh pool-destroy pool-undefine`) — tylko ze znanymi
  opcjami, nazwy wyłącznie jako wartości `--pool/--name/--domain` (forma `--opcja=wartość`, inne `-c`/`--connect`
  i argumenty zaczynające się od `-` są odrzucane). `lvremove`, `mkfs.ext4`, `pool-destroy` i `pool-undefine`
  wymagają dodatkowo jednorazowej, jawnej zgody na dokładnie to polecenie (`mkfs.ext4` — dopiero po świeżej
  kontroli pustości wolumenu).
- **Ctrl+C**: polecenia zmieniające system działają w osobnej sesji i zawsze są doczekiwane do końca (nie są
  zabijane w połowie); przerwanie jest obsługiwane po ich zakończeniu. Wycofanie i raport kroków są odporne na
  kolejne Ctrl+C.
- **Wycofanie** po błędzie obejmuje tylko niedestrukcyjne kroki z bieżącego uruchomienia: odmontowanie,
  przywrócenie `/etc/fstab` z kopii + `daemon-reload`, usunięcie utworzonych (pustych) katalogów, zatrzymanie
  i usunięcie definicji puli zdefiniowanej w tym uruchomieniu (pliki zostają). Błąd w fazie puli wycofuje
  tylko pulę — wolumen Linux zostaje gotowy.
  - Utworzony LV **nie jest usuwany automatycznie**. Interaktywnie (nigdy z `--yes`) program proponuje
    `lvremove` po wpisaniu `USUN <lv>`; Enter zostawia wolumen do dokończenia opcją 2.
  - **Po `mkfs.ext4` pełne wycofanie nie jest możliwe** (formatowania nie da się cofnąć; wolumen był pusty) —
    program to wyraźnie zgłasza.
  - Po nieudanej operacji program porównuje stan z wyjściowym (`=== WERYFIKACJA STANU ===`) i wypisuje
    polecenia do ręcznego sprawdzenia. Nie restartuj hosta, dopóki `findmnt --verify` zgłasza błędy.
- **Blokada** `/run/hvm-storage-manager.lock` — jedna instancja naraz.
- **Log** wszystkich poleceń i kroków: `/var/log/hvm-storage-manager.log` (zmiana: `--log-file`; plik otwierany
  bez podążania za dowiązaniem, uprawnienia 0640). Raporty JSON: `/var/log/hvm-storage-manager-report-<data>.json`.
- **`--dry-run` i `--diagnose` bez root niczego nie zapisują**: bez logu, bez pliku blokady, eksport raportu
  wypisuje JSON na ekran. Wyłączenie trybu podglądu w menu *Zaawansowane* dopiero wtedy włącza log i blokadę.
- **Kopie fstab**: `/etc/fstab.backup-hvm-RRRRMMDD-GGMMSS` przed każdą zmianą; zapis atomowy;
  przy błędach w obecnym fstab (`findmnt --verify`) program odmawia dopisania wpisu.
- **`nofail`**: wpis ma opcję `nofail`, więc host uruchomi się także **bez** magazynu. Po restarcie, przed
  startem VM, sprawdź `findmnt -M /vm-storage` (albo nagłówek menu).
- Nie używaj programu do storage współdzielonego. Przed użyciem na produkcji przetestuj na labie (`--dry-run`) i wykonaj backup.

## CLI dla administratorów

Bez podkomendy program uruchamia menu. `--dry-run` można podać przed lub po podkomendzie.

```bash
hvm-storage-manager --version
hvm-storage-manager --diagnose [--json]           # raport stanu (tylko odczyt, bez root — mniej danych)
sudo hvm-storage-manager --dry-run [podkomenda]   # polecenia zmieniające system są tylko wypisywane
sudo hvm-storage-manager --log-file PATH ...      # inny plik logu

sudo hvm-storage-manager prepare [--size GiB|max|recommended] [--vg VG] [--name LV] [--mount PATH]
                                 [--pool-name NAZWA] [--yes] [--fix-dev-conflict]
sudo hvm-storage-manager repair  [--vg VG] [--name LV] [--mount PATH] [--pool-name NAZWA]
                                 [--yes] [--format-empty VG/LV] [--fix-dev-conflict]
```

| Opcja | Znaczenie |
|---|---|
| `prepare --size` | `recommended` (domyślnie), `max` albo liczba GiB (1–1000000, w granicach wolnego miejsca minus rezerwa) |
| `prepare --vg` | domyślnie `ubuntu-vg` lub jedyna grupa z wolnym miejscem |
| `--name`, `--mount` | domyślnie `vm-storage`, `/vm-storage` (punkt montowania z allowlisty) |
| `--pool-name` | domyślnie `<HOST>-Local-<GiB>`; w `repair` — istniejąca pula katalogu, jeśli jest |
| `--yes` | bez pytań i frazy; w `prepare` formatuje wyłącznie NOWY LV; nigdy nie usuwa LV |
| `--format-empty VG/LV` | `repair --yes`: pozwala sformatować istniejący LV bez żadnych sygnatur — wartość musi wskazywać dokładnie naprawiany LV (np. `ubuntu-vg/vm-storage`) |
| `--fix-dev-conflict` | z `--yes`: pozwala usunąć (`rmdir`) pusty, nieużywany katalog w miejscu `/dev/<vg>/<lv>` |

`prepare` na istniejącym LV automatycznie przechodzi do `repair` (bez formatowania).

Polecenia z wersji 0.4 (te same flagi; bez puli libvirt w `create`/`resume`):

```bash
sudo hvm-storage-manager create --vg VG --name LV --size GiB --mount PATH [--yes] [--fix-dev-conflict]
sudo hvm-storage-manager resume --vg VG --name LV --mount PATH [--yes] [--format-empty VG/LV]
sudo hvm-storage-manager pools  [--mount PATH] [--refresh] [--yes]
sudo hvm-storage-manager audit  [--mount PATH]
```

- `create` — nowy LV + ext4 + fstab + mount; potwierdzenie frazą `TWORZ NOWY WOLUMEN` (`--yes` ją pomija).
- `resume` — dokończenie konfiguracji istniejącego LV (pytanie `[t/N]`; pusty LV — fraza `FORMATUJ <lv>`). Gdy nic nie brakuje: `Nic do zrobienia`.
- `pools` — pokazuje pule `dir` wskazujące dokładnie na punkt montowania; z `--refresh` odświeża je po potwierdzeniu. Nie tworzy pul.
- `audit` — tylko odczyt: montowanie, miejsce, LV, pule i proponowana nazwa `<HOST>-Local-<GiB>`; oznacza pule bez nazwy hosta w nazwie.

Kody wyjścia: `0` sukces (także zakończony dry-run), `1` błąd, odmowa, anulowanie, brak root lub działająca
inna instancja, `2` błędne argumenty, `130` przerwanie Ctrl+C.

## Dozwolone punkty montowania

Lista `ALLOWED_MOUNT_PATTERNS` w `hvm/mount.py` (dopasowanie do całej znormalizowanej ścieżki):

| Wzorzec | Przykład |
|---|---|
| `^/vm[a-z0-9_-]*$` | `/vm-storage`, `/vm_data`, `/vm2` |
| `^/srv/[a-z0-9_-]+$` | `/srv/vm-storage` |
| `^/data/[a-z0-9_-]+$` | `/data/vm-storage` |

Ścieżka musi być bezwzględna, bez spacji, `..` i dowiązań symbolicznych. Inne lokalizacje — dopisz wzorzec w kodzie.
Niezależnie od listy odrzucane są ścieżki w `/var/lib/libvirt/images` i `/var/morpheus`, nad nimi lub równe im
(`PROTECTED_TARGETS` — katalogi pul `local` / Morpheus).

## Testy

Testy jednostkowe i scenariusze na symulatorze hosta (nie zmieniają systemu, działają też bez root):

```bash
python3 -m unittest discover -s tests -v
```

Test integracyjny na urządzeniach loop (tworzy VG testowe, zmienia `/etc/fstab`, testową pulę libvirt) —
**wyłącznie na jednorazowej maszynie labowej**:

```bash
sudo HVM_TEST_I_UNDERSTAND=1 bash tests/integration_loopdev.sh
```

## Zmiany w 1.0

- Nowy układ: pakiet `hvm/` (CLI w `hvm/cli.py`, także `python3 -m hvm`); `main.py` i `hvm_storage_manager.py`
  to tylko nakładki uruchamiające `hvm.cli:main`.
- Nowa instalacja: program w `/opt/hvm-storage-manager`, wrapper w `/usr/local/sbin`, pobieranie archiwum
  zamiast jednego pliku, automatyczny powrót przy nieudanym teście, `HVM_ROLLBACK=1`.
- Nowe menu (1–6) z nagłówkiem stanu; dawne opcje 0.4 są w **6. Zaawansowane**.
- Kreator tworzy też **pulę libvirt** `<HOST>-Local-<GiB>`; nowe polecenia `prepare`, `repair`, `--diagnose [--json]`.
- Naprawa przypadków A–D, w tym `not found: device not cleared` (pusty katalog w `/dev/<vg>/<lv>`).
- Rozmiary w GiB z menu wyboru; rezerwa w grupie LVM to max(1 GiB, 1% wolnego miejsca) (w 0.4 — 1 GiB).
- Nowe magazyny z menu/`prepare` potwierdza się frazą `FORMATUJ <lv>`.
- Allowlista poleceń, chronione pule, raport kroków i weryfikacja stanu po błędzie.
- **Zmiany zachowania względem 0.4:**
  - nieudane `create` **nie usuwa już automatycznie** utworzonego LV (interaktywnie można go usunąć frazą `USUN <lv>`);
  - zniknęło pytanie `Czy wycofać zmiany? [T/n]` — niedestrukcyjne kroki są wycofywane zawsze, automatycznie;
  - wzorzec `/var/lib/libvirt/images/<x>` usunięto z allowlisty — ścieżki w `/var/lib/libvirt/images` i
    `/var/morpheus` są zawsze odrzucane;
  - `resume --yes` formatuje pusty LV tylko z `--format-empty <vg>/<lv>` (w 0.4: sama flaga `--format-empty`);
  - `lvcreate` działa z `--yes -Wy -Zy` (czyszczenie sygnatur wyłącznie na nowym LV).
