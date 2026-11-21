# Havne-elmåler – Aabenraa Sejl Club

Webinterface og opsamling for trådløse elmålere på havnens elstandere (65 standere × 6 udtag,
systemet understøtter 200 målere). Ejer: Claus Brinck (formand, ASC). Kommunikation på dansk;
Claus er automationsfolk, ikke Python-udvikler – giv trin-for-trin-instruktioner til alt der skal køres.

## Arkitektur

```
Shelly EM Mini Gen4 ~Zigbee~ SLZB-MR1U ── Zigbee2MQTT ── MQTT ── Home Assistant
                                                                   │ REST GET /api/states (hvert 30. s)
                                                 elmaaler (Docker) ─ SQLite (WAL) ─ backup hver nat
```

- Én Python-proces: Flask-app serveret af waitress + én baggrundstråd (`jobs.koer`).
- Afhængigheder bevidst minimale: flask, waitress, requests, tzdata, reportlab (kun PDF-rapporten). Ingen JS-biblioteker,
  ingen CDN (kører på lokalt net). Grafer: `gr.panel(...)` i `templates/_graf.html` tegnes som SVG af
  `static/app.js` ud fra `/api/forbrug` – Døgn/Uge/Måned/Vinter, bladr med ‹ › eller swipe, tryk på søjle = tooltip.
  Døgn er altid kl. 00–24. Medlemmer (eller `egen=1`) ser kun forbrug fra egne tildelingsperioder.
- Alt UI og alle tekster/variabelnavne er på dansk. Klublogo: `static/logo.png` (klippet fra et skærmbillede – udskift med original i høj opløsning, når den findes).

## Filer (elmaaler/app/)

| Fil | Indhold |
|---|---|
| `config.py` | alle indstillinger fra miljøvariabler (.env) |
| `db.py` | SQLite-skema, `db()` context manager, indstillinger i tabel `indstillinger`, `log()` |
| `collector.py` | henter fra Home Assistant (`HomeAssistant`) eller `Simulator`; skriver tabel `status` |
| `jobs.py` | hovedløkke: poll, gem aflæsning hvert 15. min, advarsler, offline-tjek, planlagte mails, månedslås, backup |
| `forbrug.py` | forbrugsberegning, forbrugsgrupper, månedsopgørelser |
| `perioder.py` | data til periode-grafen (`/api/forbrug`): døgn pr. time, uge/måned pr. dag, vinter pr. måned |
| `rapport.py` | CSV-udtræk og mailtekster |
| `web.py` | alle routes: Min side (bruger), `/kontor/...` (havnekontor), `/system/...` (admin) |
| `simulering.py`, `vedligehold.py` | debug-simuleret forbrug; DB-info, VACUUM, integritetstjek, nulstil fejlmålt forbrug |
| `bestyrelse.py` | PDF-månedsrapport til bestyrelsen (reportlab): `data(m)` + `pdf(d)`; sendes den 1. for nov–mar til indstillingen `bestyrelse_emails`; eksempler i `docs/eksempler/` |
| `backup.py` | natlig backup (SQLite backup-API + gzip); daglige gemmes `BACKUP_KEEP_DAYS`, den 1. i måneden 13 mdr.; krypteret kopi til OneDrive med rclone (`BACKUP_RCLONE_REMOTE=krypt:`, config i `/data/rclone/rclone.conf`) – altid `rclone copy`, aldrig `sync` |
| `mailer.py`, `sikkerhed.py`, `tid.py` | SMTP, kodeord/tokens/login-spærring, tidshjælpere |

## Vigtige designvalg

- **Forbrug = sum af delta_kwh.** Hver aflæsning i `aflaesninger` gemmer `delta_kwh` siden forrige.
  Periode `(fra, til]` = SUM(delta). Tåler tæller-nulstilling/udskiftning (fald → ny værdi tælles som delta,
  urealistiske spring > 16 A × 240 V × 1,25 ignoreres og logges).
- **Tid:** UTC epoch i DB, perioder beregnes i Europe/Copenhagen (`tid.py`).
- **Tildelinger:** `tildelinger(start_dato, slut_dato)`; slut er *eksklusiv* ("ophørt pr."). Ny tildeling
  på en måler afslutter automatisk den aktive pr. samme dato.
- **Både:** en bruger kan have flere både. Båden står på tildelingen (`tildelinger.baad`, kopieres til
  `maanedsopgoerelser.baad` ved låsning) – ikke på brugeren (`brugere.baad` er forældet, kun brugt ved migrering).
  Vinter-CSV har én linje pr. bruger og båd. `demodata.py` (`python -m app.demodata`) laver demo-brugere.
- **Forbrugsgrupper** (fra klubmeddelelsen "Sådan afregnes vinterstrøm", nov–mar):
  0–20 kWh 0 kr · 21–100 120 kr · 101–300 400 kr · 301–700 1000 kr · 701–1500 2200 kr · over 1500 4500 kr.
  Månedsforbrug afrundes til hele kWh før placering. Opgøres pr. måler/tildeling (pr. båd), ikke pr. bruger.
  Kan ændres af systemadministratoren (System → Indstillinger).
- **Brugerlag (`brugere.rolle`):** `bruger` (medlem) < `kontor` (havnekontor: brugere, tildel/fjern målere,
  opgørelser) < `admin` (systemadministrator: alt + System-siderne: status, log, måleropsætning, database/backup,
  indstillinger, debug). Tjek med `er_kontor()`/`er_admin()` og `kontor_kraevet`/`admin_kraevet` i `web.py`.
  `ADMIN_EMAIL` gøres til admin ved start, hvis der ingen admin findes. Kun admin kan give admin-rollen, og
  havnekontoret kan ikke ændre en admins konto.
- **Fjern måler fra bruger** = afslut tildelingen pr. dato (`_fjern_tildeling`); historik bevares. Siden
  `/kontor/ubrugte` viser tildelte målere uden forbrug i N dage, så de kan fjernes samlet.
- **Forbrugsrapport på mail:** Min side → `/min-side/rapport` (`rapport.bruger_periode`), tekst + CSV pr. dag,
  maks 5 pr. bruger pr. time.
- **Månedslås:** den 1. kl. 00:20 fryses forrige måned i `maanedsopgoerelser`; kan genberegnes manuelt.
- **Offline:** bygger på HA-state `unavailable` → kræver Zigbee2MQTT `availability: enabled: true`.
- **Engangsjob/varsler** dedupliceres via tabel `varsler_sendt` (nøgler som `job:dag:2026-11-21`, `adv:...`).
- **Debug / simuleret forbrug** (`simulering.py`, side `/system/debug`): tabel `sim_forbrug` lægges oven i
  `aflaesninger` via viewet `alle_delta` – alle forbrugssummer skal læse fra `alle_delta`, mens tællerstande
  (`energi_kwh`) altid læses fra `aflaesninger`. Mens der findes simuleret forbrug, er mails til medlemmer på pause,
  og berørte låste måneder genberegnes ved tilføj/slet.
- **DB-låse:** åbn aldrig en ny forbindelse (fx `db.log()` eller `mailer.send()` der fejler og logger) mens en
  skrive-transaktion er åben – send `c` med til `db.log(type, tekst, c)` eller send mail efter commit.
- Sikkerhed: PBKDF2-hash, CSRF-token på alle POST, login-spærring 10 forsøg/15 min, session ugyldig ved kodeskift.

## Kør og test lokalt

```bash
cd elmaaler
pip install -r requirements.txt
# PowerShell: $env:DATA_DIR="./testdata"; $env:SOURCE="sim"; ...
DATA_DIR=./testdata SOURCE=sim SIM_BACKFILL_DAYS=45 ADMIN_EMAIL=a@b.dk ADMIN_PASSWORD=testtest1 PORT=8000 python -m app.main
```

`SOURCE=sim` giver 200 falske målere med historik. Log ind på http://localhost:8000.

## Drift

- `docker-compose.yml` + `.env.example` (almindelig Docker) – se `docs/07-elmaaler-installation.md`.
- Koden ligger i Docker-imaget: efter `git pull` skal imaget altid bygges om (`docker compose up -d --build`).
- `dockge/compose.yaml` + `dockge/.env.example` (indholdet sættes i Dockges .env-felt) – Dockge-stack: læg i `/opt/stacks/elmaaler/` med koden i
  `/opt/stacks/elmaaler/elmaaler/`. Data i navngivet volume `elmaaler-data`.
- GitHub: `clausbrinck/Havne_elmaaler` (var tomt ved overførslen – første push mangler).

## Status (21-11-2026)

Bygget og testet med simulator (alle sider, mails, advarsler, offline, import, kodeskift/nulstil, backup)
og mod en falsk Home Assistant. Endnu ikke kørt mod rigtig Home Assistant eller rigtige Shelly-målere.

Mulige næste skridt: pilot med 6 målere · HTTPS/adgang udefra (Cloudflare Tunnel) · udtynding af gamle
aflæsninger efter 2 år · relæ-målere (Shelly 1PM) hvis udtag skal kunne slukkes.
