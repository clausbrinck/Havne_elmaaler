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
- Afhængigheder bevidst minimale: flask, waitress, requests, tzdata. Ingen JS-biblioteker,
  ingen CDN (kører på lokalt net). Grafer er SVG i Jinja-makroer (`templates/_graf.html`).
- Alt UI og alle tekster/variabelnavne er på dansk.

## Filer (elmaaler/app/)

| Fil | Indhold |
|---|---|
| `config.py` | alle indstillinger fra miljøvariabler (.env) |
| `db.py` | SQLite-skema, `db()` context manager, indstillinger i tabel `indstillinger`, `log()` |
| `collector.py` | henter fra Home Assistant (`HomeAssistant`) eller `Simulator`; skriver tabel `status` |
| `jobs.py` | hovedløkke: poll, gem aflæsning hvert 15. min, advarsler, offline-tjek, planlagte mails, månedslås, backup |
| `forbrug.py` | forbrugsberegning, forbrugsgrupper, månedsopgørelser |
| `rapport.py` | CSV-udtræk og mailtekster |
| `web.py` | alle routes (bruger + havnekontor) |
| `mailer.py`, `backup.py`, `sikkerhed.py`, `tid.py` | SMTP, backup, kodeord/tokens/login-spærring, tidshjælpere |

## Vigtige designvalg

- **Forbrug = sum af delta_kwh.** Hver aflæsning i `aflaesninger` gemmer `delta_kwh` siden forrige.
  Periode `(fra, til]` = SUM(delta). Tåler tæller-nulstilling/udskiftning (fald → ny værdi tælles som delta,
  urealistiske spring > 16 A × 240 V × 1,25 ignoreres og logges).
- **Tid:** UTC epoch i DB, perioder beregnes i Europe/Copenhagen (`tid.py`).
- **Tildelinger:** `tildelinger(start_dato, slut_dato)`; slut er *eksklusiv* ("ophørt pr."). Ny tildeling
  på en måler afslutter automatisk den aktive pr. samme dato.
- **Forbrugsgrupper** (fra klubmeddelelsen "Sådan afregnes vinterstrøm", nov–mar):
  0–20 kWh 0 kr · 21–100 120 kr · 101–300 400 kr · 301–700 1000 kr · 701–1500 2200 kr · over 1500 4500 kr.
  Månedsforbrug afrundes til hele kWh før placering. Opgøres pr. måler/tildeling (pr. båd), ikke pr. bruger.
  Kan ændres af havnekontoret (Indstillinger).
- **Månedslås:** den 1. kl. 00:20 fryses forrige måned i `maanedsopgoerelser`; kan genberegnes manuelt.
- **Offline:** bygger på HA-state `unavailable` → kræver Zigbee2MQTT `availability: enabled: true`.
- **Engangsjob/varsler** dedupliceres via tabel `varsler_sendt` (nøgler som `job:dag:2026-11-21`, `adv:...`).
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
- `dockge/compose.yaml` + `dockge/.env.example` (indholdet sættes i Dockges .env-felt) – Dockge-stack: læg i `/opt/stacks/elmaaler/` med koden i
  `/opt/stacks/elmaaler/elmaaler/`. Data i navngivet volume `elmaaler-data`.
- GitHub: `clausbrinck/Havne_elmaaler` (var tomt ved overførslen – første push mangler).

## Status (21-11-2026)

Bygget og testet med simulator (alle sider, mails, advarsler, offline, import, kodeskift/nulstil, backup)
og mod en falsk Home Assistant. Endnu ikke kørt mod rigtig Home Assistant eller rigtige Shelly-målere.

Mulige næste skridt: pilot med 6 målere · HTTPS/adgang udefra (Cloudflare Tunnel) · udtynding af gamle
aflæsninger efter 2 år · relæ-målere (Shelly 1PM) hvis udtag skal kunne slukkes.
