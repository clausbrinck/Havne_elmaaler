# 07 – Installation og drift af elmaaler (webinterface)

Forudsætning: G3S-serveren kører Linux med Docker (se docs/02), og Home Assistant
kører med Zigbee2MQTT og målerne parret som `elmaaler-001`, `elmaaler-002` …

Alle kommandoer skrives i en terminal på serveren (eller via `ssh`).

## 1. Tjek at målerne findes i Home Assistant

I Home Assistant: **Indstillinger → Enheder og tjenester → Entiteter**, søg på `elmaaler_001`.
Der skal være mindst:

| Entitet | Indhold |
|---|---|
| `sensor.elmaaler_001_energy` | tællerstand i kWh |
| `sensor.elmaaler_001_power` | effekt i W |
| `sensor.elmaaler_001_linkquality` | Zigbee-signal (valgfri – er **deaktiveret** som standard; aktivér den, hvis du vil se signal) |

Hedder de noget andet, rettes mønstrene `HA_ENERGY_ENTITY` osv. i `.env`.

**Vigtigt – Zigbee2MQTT "availability":** for at målere vises som *offline*, når de mister
forbindelsen, skal availability være slået til i Zigbee2MQTT. I `configuration.yaml`:

```yaml
availability:
  enabled: true
  active:
    timeout: 10      # minutter – netforsynede enheder (routere)
```

Ellers vil Home Assistant blive ved med at vise den sidste værdi, og offline-varsler kommer ikke.

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

Åbn `http://<serverens-ip>:8000` og log ind med `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
**Skift koden** under *Konto* med det samme.

Under **Netværk & status** skal "Home Assistant" stå som OK, og "Målere fundet" skal passe.

## 5. Opsætning i webinterfacet

1. **Indstillinger** → tryk *Send testmail til mig*. Kommer den ikke, se *Hændelseslog* under Netværk.
2. **Brugere → Importér fra CSV** – lav et regneark med kolonnerne
   `maaler;placering;email;navn;baad;telefon;start_dato`, gem som *CSV UTF-8*, og importér.
   Sæt flueben i *Send invitation*, så får medlemmerne en mail og vælger selv adgangskode.
3. Opret Flemming og andre på havnekontoret med rollen **Havnekontor**.
4. Under **Indstillinger** vælger hver kontor-bruger sine mails, og evt. kassereren tilføjes som ekstra modtager.

## Hvornår sker hvad

| Hvornår | Hvad |
|---|---|
| hvert 30. sekund | henter tællerstand og effekt fra Home Assistant |
| hvert 15. minut | gemmer aflæsning; tjekker om medlemmer nærmer sig næste gruppe (advarer ved 90 %) |
| hvert minut | tjekker målere uden forbindelse i mere end 60 min → mail til medlem og kontor |
| kl. 03:00 | backup af databasen |
| kl. 06:00 | døgnopgørelser; mandag ugeopgørelser; den 1. månedsopgørelser (kontoret får CSV vedhæftet) |
| den 1. kl. 00:20 | forrige måned låses |

Tidspunkter og grænser ændres under Indstillinger (backup-tid i `.env`).

Uden forbindelse til Home Assistant tæller målerne videre selv. Når forbindelsen er tilbage,
kommer forbruget med i næste aflæsning. Kontoret får mail, hvis Home Assistant har været væk i mere end en time.

## Backup og gendannelse

Backups ligger i `data/backup/` som `elmaaler-ÅÅÅÅMMDD-TTMMSS.db.gz` og gemmes 30 dage.
**Læg også en kopi uden for serveren** – fx USB-disk eller NAS:

1. Mount disken, fx på `/mnt/usb-backup`.
2. I `docker-compose.yml`: fjern `#` foran linjen `- /mnt/usb-backup:/backup-usb`.
3. I `.env`: `BACKUP_EXTRA_DIR=/backup-usb`, og kør `docker compose up -d`.

Backups kan også hentes fra **Indstillinger** i webinterfacet.

Gendannelse:

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

Når siden kører på HTTPS: sæt `BASE_URL` til den offentlige adresse, fx
`BASE_URL=https://el.aabenraa-sejlclub.dk`, i `.env` og kør `docker compose up -d`
(i Dockge: ret `.env`-feltet og tryk **Deploy**).

- `BASE_URL` bruges i alle links i mails og i nulstil-/invitationslinks. Står den til `localhost`,
  virker linkene kun på serveren selv. Havnekontoret får en rød advarsel øverst på siden, hvis
  `BASE_URL` ikke passer med den adresse, siden bliver brugt på.
- Når `BASE_URL` starter med `https://`, bliver login-cookien automatisk "secure" (sendes kun over HTTPS).
  Log så også ind via domænet hjemme på havnen – login via `http://192.168.x.x:8000` virker ikke længere.
  Vil man fortsat kunne logge ind via lokal IP, sæt `COOKIE_SECURE=false` (mindre sikkert).
- `PROXY_HOPS=1` (standard) betyder, at programmet stoler på én proxy foran sig (Cloudflare Tunnel,
  Caddy, Nginx) og bruger dens `X-Forwarded-For/-Proto/-Host`. Så ser login-spærringen brugerens rigtige
  IP i stedet for tunnelens. Kører siden uden proxy, kan den sættes til `0`.

Sikkerhed i programmet: adgangskoder gemmes som PBKDF2-hash, login spærres 15 min efter
10 forkerte forsøg, alle formularer har CSRF-beskyttelse, og nulstil-links udløber efter 24 timer
(invitationer efter 14 dage).

## Fejlsøgning

| Problem | Løsning |
|---|---|
| "Ingen forbindelse til Home Assistant: 401" | Forkert `HA_TOKEN` |
| "… Connection refused" | Forkert `HA_URL` – brug serverens IP, ikke `localhost`, inde fra Docker |
| Målere fundet = 0 | Entitetsnavnene passer ikke – tjek mønstrene i `.env` mod Home Assistant |
| Måler aldrig offline | Availability er ikke slået til i Zigbee2MQTT (se trin 1) |
| Ingen mails | Tjek `SMTP_...`, send testmail, se Hændelseslog |
| `Permission denied: /data` | `sudo chown -R 1000:1000 data` |
