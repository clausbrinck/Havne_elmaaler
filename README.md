# Havne-elmåler – Aabenraa Sejl Club

Trådløs måling af strømforbrug på havnens elstandere. Hvert udtag har en
Shelly EM Mini Gen4 (Zigbee) i et nummereret hus ("Elmåler NR. 001" …).
Målerne kommer ind i **Home Assistant** via Zigbee2MQTT. Programmet `elmaaler`
henter tællerstand (kWh) og aktuel effekt (W) fra Home Assistant, gemmer dem i
en lokal database og har et webinterface for medlemmer og havnekontor.

```
 Shelly EM Mini ~~Zigbee~~ SLZB-MR1U ── Zigbee2MQTT ── MQTT ── Home Assistant
                                                                    │ REST API (hvert 30. s)
                                                                    ▼
                                         elmaaler (Docker) ─ SQLite ─ backup hver nat
                                           ├ webinterface (bruger + havnekontor)
                                           └ emails (advarsler, opgørelser, offline)
```

## Hvad kan det

**Op til 200 målere** (kan hæves med `METER_COUNT`).

**Bruger-login (medlemmer)**
- Aktuel effekt (opdateres hvert 15. sekund), forbrug i dag, denne måned og total siden tildeling
- Hvilken forbrugsgruppe måneden ligger i, hvor tæt man er på næste gruppe, og prognose for måneden
- Graf over forbrug pr. dag og tidligere måneders gruppe og pris
- Email til/fra: advarsel ved næste forbrugsgruppe · opgørelse pr. døgn / uge / måned ·
  besked når forbindelsen til måleren har været væk i mere end en time
- Skift email og adgangskode · glemt kode via email

**Havnekontor-login**
- Overblik over alle målere: bruger, tildelt fra, status, effekt, månedens kWh og gruppe, signal
- Samlet effekt nu og sidste døgn, samlet forbrug pr. dag
- Brugere: opret, invitér på mail, tildel/afslut måler med dato, CSV-import af mange på én gang
- Opgørelse pr. måned: hvem er i hvilken gruppe (indeværende måned til nu og afsluttede måneder),
  fordeling og beløb, CSV til Excel, samlet vinterperiode pr. bruger til opkrævning ved standerhejsning
- Netværk & status: Home Assistant/Zigbee2MQTT-forbindelse, signal (linkquality) pr. måler,
  offline-målere, brugerlogins, hændelseslog
- Indstillinger: hvilke kontor-mails (manglende forbindelse til måler nr. …, samlet forbrug døgn/uge/måned),
  ekstra modtagere, forbrugsgrupper og priser, backup

## Forbrugsgrupper

Fra klubmeddelelsen "Sådan afregnes vinterstrøm" – 1. november til 31. marts:

| Forbrug pr. måned | Pris pr. måned |
|---|---|
| 0–20 kWh | 0 kr |
| 21–100 kWh | 120 kr |
| 101–300 kWh | 400 kr |
| 301–700 kWh | 1.000 kr |
| 701–1.500 kWh | 2.200 kr |
| over 1.500 kWh | 4.500 kr |

Regler i programmet:
- Månedens forbrug afrundes til hele kWh før placering (20,4 → 20 → gratis; 20,5 → 21 → 120 kr).
- Hver måler/båd opgøres for sig. Skifter en måler bruger midt i måneden, opgøres hver bruger for sin del.
- Den 1. i hver måned kl. 00:20 **låses** forrige måned, så senere ændringer af grupper ikke ændrer historikken.
  Havnekontoret kan genberegne en låst måned.
- Grupper, priser og hvilke måneder der afregnes kan ændres under Indstillinger.

## Hurtig start

Se trin-for-trin i **[docs/07-elmaaler-installation.md](docs/07-elmaaler-installation.md)**.

```bash
cd ~/havne-elmaaler
cp .env.example .env && nano .env      # HA_URL, HA_TOKEN, SMTP, ADMIN_...
mkdir -p data && sudo chown 1000:1000 data
docker compose up -d --build
```

Åbn `http://<serverens-ip>:8000` og log ind med `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

Prøv det uden målere: sæt `SOURCE=sim` og `SIM_BACKFILL_DAYS=45` i `.env` – så laves 200 falske målere
med 45 dages historik.

## Mapper

```
├── docker-compose.yml
├── .env.example
├── docs/07-elmaaler-installation.md   installation, Home Assistant, mail, backup, adgang udefra
├── elmaaler/
│   ├── Dockerfile, requirements.txt
│   └── app/
│       ├── main.py        start
│       ├── collector.py   henter data fra Home Assistant (eller simulator)
│       ├── jobs.py        aflæsning hvert 15. min, varsler, opgørelser, backup
│       ├── forbrug.py     beregning af forbrug og forbrugsgrupper
│       ├── web.py         webinterface
│       ├── rapport.py     CSV og mailtekster
│       ├── backup.py, mailer.py, sikkerhed.py, db.py, config.py, tid.py
│       └── templates/, static/
└── data/                  database, backups (oprettes automatisk – ikke i git)
```
