# 07 – Installation og drift af elmaaler (webinterface)

Forudsætning: G3S-serveren kører Linux med Docker (se docs/02), og Home Assistant
kører med **ZHA** (Zigbee Home Automation) på SLZB-MR1U og målerne parret og navngivet `elmaaler-001`,
`elmaaler-002` … (se administrationsmanualens kapitel 5). MQTT og Zigbee2MQTT bruges ikke.

Alle kommandoer skrives i en terminal på serveren (eller via `ssh`).

## 1. Tjek at målerne findes i Home Assistant

I Home Assistant: **Indstillinger → Enheder og tjenester → Entiteter**, søg på `elmaaler_001`.
Der skal være mindst:

| Entitet | Indhold |
|---|---|
| `sensor.elmaaler_001_summation_delivered` (eller `…_energy`) | tællerstand i kWh |
| `sensor.elmaaler_001_power` | effekt i W |
| `sensor.elmaaler_001_lqi` | Zigbee-signal (valgfri – er **deaktiveret** som standard i ZHA; aktivér den, hvis du vil se signal) |

Hedder de noget andet, sættes mønstrene `HA_ENERGY_ENTITY` osv. i `.env` (flere bud adskilles med komma).

**Vigtigt – offline-tid i ZHA:** ZHA melder som standard først en netforsynet måler *utilgængelig* efter
2 timer. Sæt det ned, så offline-varsler kommer i tide:
**Indstillinger → Enheder og tjenester → Zigbee Home Automation → Konfigurer →**
*Betragt netdrevne enheder som utilgængelige efter (sekunder)* = **900**.

## 2. Lav et adgangstoken i Home Assistant

1. Klik på dit brugernavn nederst til venstre i Home Assistant → fanen **Sikkerhed**.
2. Rul ned til **Langtids-adgangstokens** → **Opret token** → navn: `elmaaler`.
3. Kopiér den lange tekst (vises kun én gang). Den skal i `.env` som `HA_TOKEN`.

Tip: opret evt. en separat HA-bruger "elmaaler" (ikke administrator) og lav tokenet på den.

## 3. Hent programmet og udfyld .env

```bash
cd ~
git clone https://github.com/clausbrinck/Havne_elmaaler.git havne-elmaaler
cd havne-elmaaler
cp .env.example .env
nano .env
```

Udfyld mindst: `BASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `HA_URL`, `HA_TOKEN` og `SMTP_...`.
Gem med `Ctrl+O`, `Enter`, luk med `Ctrl+X`.

## 4. Start

```bash
mkdir -p data && sudo chown 1000:1000 data
docker compose up -d --build
docker compose logs -f elmaaler        # se at det kører – stop visning med Ctrl+C
```

Åbn `http://<serverens-ip>:8000` og log ind med `ADMIN_EMAIL`/`ADMIN_PASSWORD` – det er **systemadministrator**-kontoen.
**Skift koden** under *Konto* med det samme.

Under **System → Status** skal "Home Assistant" stå som OK, og "Målere fundet" skal passe.

## Brugerlag

| Rolle | Kan |
|---|---|
| **Bruger** (medlem) | se eget forbrug på *Min side*, vælge mails, få en forbrugsrapport sendt på mail |
| **Havnekontor** | alt ovenfor + oprette/fjerne brugere, tildele målere og fjerne dem igen (også samlet under *Brugere → Målere der ikke er i brug*), opgørelser og CSV |
| **Systemadministrator** | alt ovenfor + *System*: status/Zigbee, systemlog, måleropsætning (placering/aktiv), database og backup, indstillinger (grupper, priser, mails), debug |

Kontoen fra `ADMIN_EMAIL` bliver automatisk systemadministrator (også ved opdatering fra en ældre version).
Kun en systemadministrator kan give andre administrator-rollen.

## 5. Opsætning i webinterfacet

1. **System → Indstillinger** → tryk *Send testmail til mig*. Kommer den ikke, se **System → Systemlog**.
2. **Brugere → Importér fra CSV** – lav et regneark med kolonnerne
   `maaler;placering;email;navn;baad;telefon;start_dato`, gem som *CSV UTF-8*, og importér.
   Sæt flueben i *Send invitation*, så får medlemmerne en mail og vælger selv adgangskode.
3. Opret Flemming og andre på havnekontoret med rollen **Havnekontor**.
4. Under **Konto** vælger hver kontor-bruger sine mails. Kassereren kan tilføjes som ekstra modtager under **System → Indstillinger**.

## Hvornår sker hvad

| Hvornår | Hvad |
|---|---|
| hvert 30. sekund | henter tællerstand og effekt fra Home Assistant |
| hvert 15. minut | gemmer aflæsning; tjekker om medlemmer nærmer sig næste gruppe (advarer ved 90 %) |
| hvert minut | tjekker målere uden forbindelse i mere end 60 min → mail til medlem og kontor |
| kl. 03:00 | backup af databasen |
| kl. 06:00 | døgnopgørelser; mandag ugeopgørelser; den 1. månedsopgørelser (kontoret får CSV vedhæftet) |
| den 1. kl. 00:20 | forrige måned låses |

Tidspunkter og grænser ændres under System → Indstillinger (backup-tid i `.env`).

Uden forbindelse til Home Assistant tæller målerne videre selv. Når forbindelsen er tilbage,
kommer forbruget med i næste aflæsning. Kontoret får mail, hvis Home Assistant har været væk i mere end en time.

## Backup og gendannelse

Backups ligger i `data/backup/` som `elmaaler-ÅÅÅÅMMDD-TTMMSS.db.gz`. Daglige backups gemmes 30 dage
(`BACKUP_KEEP_DAYS`), og backuppen fra den 1. i hver måned gemmes 13 måneder, så en afregning kan
genskabes efter standerhejsning. **Læg også en kopi uden for serveren** – helst krypteret i OneDrive (nedenfor)
eller på USB-disk/NAS:

1. Mount disken, fx på `/mnt/usb-backup`.
2. I `docker-compose.yml`: fjern `#` foran linjen `- /mnt/usb-backup:/backup-usb`.
3. I `.env`: `BACKUP_EXTRA_DIR=/backup-usb`, og kør `docker compose up -d`.

Backups kan også laves og hentes under **System → Database & backup** i webinterfacet.

### Backup i OneDrive (krypteret)

Hver nat efter backuppen kopieres den med programmet **rclone** til en mappe i OneDrive. Filerne
krypteres *før* de forlader serveren, så Microsoft (eller andre med adgang til OneDrive-kontoen) kun ser
uforståelige filnavne og data. Kopierne i OneDrive slettes aldrig af en tom eller hacket server – kun
backups ældre end 30 dage (daglige) / 13 måneder (den 1. i måneden) ryddes op.

Brug en OneDrive-konto, der tilhører **klubben** (ikke en privat konto), så den overlever bestyrelsesskift.

**1. Opdatér serveren**, så rclone er i Docker-imaget:

```bash
cd /opt/stacks/elmaaler && docker compose up -d --build
```

**2. Installér rclone på din egen pc** (bruges kun til at logge ind i OneDrive med browseren). I PowerShell:

```powershell
winget install Rclone.Rclone
```

Luk og åbn PowerShell igen bagefter.

**3. Opret forbindelsen til OneDrive** – på serveren:

```bash
docker exec -it elmaaler rclone config
```

Svar sådan (tryk Enter = standardsvar):

| Spørgsmål | Svar |
|---|---|
| `n/s/q>` | `n` (ny) |
| `name>` | `onedrive` |
| `Storage>` | skriv `onedrive` |
| `client_id>` og `client_secret>` | Enter (tomt) |
| `region>` | `1` (Microsoft Cloud Global) |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate?` | `n` |
| `config_token>` | se nedenfor |

rclone viser nu en kommando, fx `rclone authorize "onedrive"`. Kør den **i PowerShell på din pc**. Browseren
åbner – log ind med klubbens Microsoft-konto og godkend. PowerShell viser derefter en lang tekst
mellem `--->` og `<---End paste`. Kopiér hele teksten (det der står mellem pilene) og sæt den ind ved `config_token>` på serveren.

| Spørgsmål | Svar |
|---|---|
| `config_type>` | `1` (OneDrive Personal or Business) |
| `config_driveid>` | `1` (den første/eneste) |
| `Is that okay?` / `Keep this "onedrive" remote?` | `y` |

**4. Opret den krypterede mappe** – stadig i `rclone config`:

| Spørgsmål | Svar |
|---|---|
| `e/n/d/r/c/s/q>` | `n` (ny) |
| `name>` | `krypt` |
| `Storage>` | skriv `crypt` |
| `remote>` | `onedrive:Elmaaler-backup` |
| `filename_encryption>` | `1` (standard) |
| `directory_name_encryption>` | `1` (true) |
| `y/g>` (adgangskode) | `g` (lav en stærk), vælg `128` bits, og `y` for at bruge den |
| `y/g/n>` (salt / password2) | `g`, `128`, `y` |
| `Edit advanced config?` | `n` |
| `Keep this "krypt" remote?` | `y` |
| `e/n/d/r/c/s/q>` | `q` (afslut) |

> ⚠ **Skriv de to adgangskoder ned, som rclone viser i trin 4**, og gem dem i klubbens
> password manager eller i et kuvert i pengeskabet. **Uden dem kan backupperne i OneDrive ikke læses** –
> heller ikke af os. Går serveren tabt, skal de bruges til at gendanne.

**5. Slå kopieringen til**: i Dockge (`.env`-feltet) eller `.env`:

```
BACKUP_RCLONE_REMOTE=krypt:
```

Gem og genstart stacken (`docker compose up -d`).

**6. Afprøv**: Log ind som systemadministrator → **System → Database & backup** → *Kopiér til skyen nu*.
Genindlæs siden efter et minut – der skal stå "sidst kopieret …" i en grå boks. I OneDrive ligger nu
mappen `Elmaaler-backup` med uforståelige filnavne. Det er korrekt (de er krypterede).

Fejler kopieringen, skrives det i System → Log, og havnekontoret får en mail. Siden viser en rød boks.

**Gendan fra OneDrive** (fx på en ny server eller din pc med rclone og de samme to adgangskoder sat op som i trin 3–4):

```bash
rclone ls krypt:                                  # vis backupperne
rclone copy krypt:elmaaler-20261201-030000.db.gz .  # hent én
```

Pak den ud og læg den på plads som beskrevet under *Gendannelse* nedenfor.

### Gendannelse

```bash
cd ~/havne-elmaaler
docker compose stop elmaaler
cp data/elmaaler.db data/elmaaler-gammel.db
gunzip -c data/backup/elmaaler-20261120-030000.db.gz > data/elmaaler.db
rm -f data/elmaaler.db-wal data/elmaaler.db-shm
docker compose start elmaaler
```

## Opdatering

```bash
cd ~/havne-elmaaler
git pull
docker compose up -d --build
```

## Adgang udefra (medlemmer hjemmefra)

Webinterfacet skal kunne nås fra internettet, hvis medlemmerne skal bruge det hjemmefra.
**Åbn ikke port 8000 direkte i routeren.** Brug HTTPS, fx:

- **Cloudflare Tunnel** (ingen port åbnes i routeren, gratis): installér `cloudflared` på serveren
  og peg et underdomæne, fx `el.aabenraa-sejlclub.dk`, på `http://localhost:8000`.
- **Caddy** som reverse proxy med automatisk Let's Encrypt-certifikat (kræver port 443 åben).

Når siden kører på HTTPS: sæt `COOKIE_SECURE=true` og `BASE_URL=https://el.aabenraa-sejlclub.dk` i `.env`
og kør `docker compose up -d`.

Sikkerhed i programmet: adgangskoder gemmes som PBKDF2-hash, login spærres 15 min efter
10 forkerte forsøg, alle formularer har CSRF-beskyttelse, og nulstil-links udløber efter 24 timer
(invitationer efter 14 dage).

## Fejlsøgning

| Problem | Løsning |
|---|---|
| "Ingen forbindelse til Home Assistant: 401" | Forkert `HA_TOKEN` |
| "… Connection refused" | Forkert `HA_URL` – brug serverens IP, ikke `localhost`, inde fra Docker |
| Målere fundet = 0 | Entitetsnavnene passer ikke – tjek mønstrene i `.env` mod Home Assistant |
| Måler sent eller aldrig offline | Sæt ZHA's tid for utilgængelige netdrevne enheder ned (se trin 1) |
| Ingen mails | Tjek `SMTP_...`, send testmail, se Hændelseslog |
| `Permission denied: /data` | `sudo chown -R 1000:1000 data` |
