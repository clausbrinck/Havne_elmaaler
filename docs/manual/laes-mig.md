# Manualen – sådan bygges den

Resultatet er tre manualer og to quickguides i `elmaaler/app/hjaelp/` (Manual-medlem/-havnekontor/-administration.pdf og
Quickguide-medlem/-havnekontor.pdf). Kilden er `manual-*.html` og `quickguide-*.html` (tekst, fælles stil i
`manual.css`/`quickguide.css`) og `billeder/` (skærmbilleder). PDF'erne vises på websiden under **Hjælp**
(efter rolle) – efter en ny byg skal serveren opdateres (`docker compose up -d --build`).
Teksten rettes i html-filerne; billederne tages automatisk fra et testsystem med falske målere.

Kræver Python 3.12 og Microsoft Edge på pc'en. Kør i PowerShell fra mappen `docs\manual`.

## 1. Start et testsystem med testdata (kun første gang / når data skal laves om)

```powershell
$env:DATA_DIR = "$env:TEMP\elmaaler-manualdata"; $env:SOURCE = "sim"; $env:SIM_BACKFILL_DAYS = "60"
$env:ADMIN_EMAIL = "admin@aabenraa-sejlclub.dk"; $env:ADMIN_PASSWORD = "testtest1"
python testdata.py
```

`testdata.py` laver Hans Hansen (Havørnen + Marlin), Grete Jensen, 128 andre medlemmer, standerplaceringer
og brugeren `kontor@aabenraa-sejlclub.dk`. Alle har kodeordet `testtest1`. Den skriver, hvilke målere Hans' både har.

## 2. Start testsystemet

Samme vindue som ovenfor (miljøvariablerne skal være sat), med Hans' målernumre fra trin 1:

```powershell
$env:SMTP_HOST = "127.0.0.1"; $env:SMTP_PORT = "2525"; $env:SMTP_SECURITY = "none"
$env:TVING_OFFLINE = "159"; $env:TVING_W = "199:227"
python testsystem.py
```

`TVING_OFFLINE` = Marlins måler (vises offline), `TVING_W` = Havørnens måler og watt. Mails sendes ikke ud,
men gemmes i `%TEMP%\elmaaler-manual-mails`.

## 3. Tag skærmbilleder og byg PDF'en (i et nyt PowerShell-vindue)

```powershell
python skaermbilleder.py          # alle billeder – eller fx: python skaermbilleder.py minside-graf
python byg.py
```

Hvert billede og dets nummererede markeringer står i listen `BILLEDER` i `skaermbilleder.py`.
Numrene i billederne skal passe med de nummererede punkter under billedet i html-filerne.
