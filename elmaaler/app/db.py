"""SQLite-database: skema, forbindelse og indstillinger."""
import json
import os
import sqlite3
from contextlib import contextmanager

from . import config

SKEMA = """
CREATE TABLE IF NOT EXISTS maalere (
    id          INTEGER PRIMARY KEY,          -- = målernummer (1..N)
    placering   TEXT NOT NULL DEFAULT '',     -- fx S12-U3
    aktiv       INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS status (           -- seneste værdier fra Home Assistant
    maaler_id      INTEGER PRIMARY KEY REFERENCES maalere(id),
    energi_kwh     REAL,
    effekt_w       REAL,
    linkquality    INTEGER,
    tilgaengelig   INTEGER NOT NULL DEFAULT 0,
    sidst_set      INTEGER,                   -- sidste gang måleren meldte sig
    offline_siden  INTEGER,
    offline_varslet INTEGER NOT NULL DEFAULT 0,
    opdateret      INTEGER
);

CREATE TABLE IF NOT EXISTS aflaesninger (     -- tællerstand hvert 15. minut
    maaler_id   INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    energi_kwh  REAL NOT NULL,
    delta_kwh   REAL NOT NULL,                -- forbrug siden forrige aflæsning
    PRIMARY KEY (maaler_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_afl_ts ON aflaesninger(ts);

CREATE TABLE IF NOT EXISTS brugere (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    navn          TEXT NOT NULL DEFAULT '',
    baad          TEXT NOT NULL DEFAULT '',     -- forældet: båd står nu på tildelingen
    telefon       TEXT NOT NULL DEFAULT '',
    pw_hash       TEXT,
    rolle         TEXT NOT NULL DEFAULT 'bruger',   -- bruger | kontor (havnekontor) | admin (system)
    aktiv         INTEGER NOT NULL DEFAULT 1,
    oprettet      INTEGER,
    sidste_login  INTEGER,
    mail_advarsel INTEGER NOT NULL DEFAULT 1,
    mail_dag      INTEGER NOT NULL DEFAULT 0,
    mail_uge      INTEGER NOT NULL DEFAULT 0,
    mail_maaned   INTEGER NOT NULL DEFAULT 1,
    mail_offline  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tildelinger (
    id          INTEGER PRIMARY KEY,
    maaler_id   INTEGER NOT NULL REFERENCES maalere(id),
    bruger_id   INTEGER NOT NULL REFERENCES brugere(id),
    start_dato  TEXT NOT NULL,                -- ÅÅÅÅ-MM-DD, fra kl. 00:00
    slut_dato   TEXT,                         -- ophørt pr. (kl. 00:00), NULL = aktiv
    start_ts    INTEGER NOT NULL,
    slut_ts     INTEGER,
    oprettet    INTEGER,
    baad        TEXT NOT NULL DEFAULT ''     -- båden på denne måler (en bruger kan have flere både)
);
CREATE INDEX IF NOT EXISTS ix_tild_maaler ON tildelinger(maaler_id);
CREATE INDEX IF NOT EXISTS ix_tild_bruger ON tildelinger(bruger_id);

CREATE TABLE IF NOT EXISTS maanedsopgoerelser (  -- låst opgørelse for afsluttede måneder
    maaned     TEXT NOT NULL,
    maaler_id  INTEGER NOT NULL,
    bruger_id  INTEGER,                         -- NULL = ikke tildelt
    fra_dato   TEXT NOT NULL,
    til_dato   TEXT NOT NULL,
    kwh        REAL NOT NULL,
    gruppe     INTEGER,
    pris       REAL,
    beregnet   INTEGER,
    baad       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (maaned, maaler_id, fra_dato)
);

CREATE TABLE IF NOT EXISTS varsler_sendt (
    noegle  TEXT PRIMARY KEY,
    ts      INTEGER
);

CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    bruger_id  INTEGER NOT NULL,
    formaal    TEXT NOT NULL,
    udloeber   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS haendelser (
    id    INTEGER PRIMARY KEY,
    ts    INTEGER NOT NULL,
    type  TEXT NOT NULL,
    tekst TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_haend_ts ON haendelser(ts);

CREATE TABLE IF NOT EXISTS indstillinger (
    noegle  TEXT PRIMARY KEY,
    vaerdi  TEXT NOT NULL
);

-- Simuleret forbrug fra debug-siden. Lægges OVEN I de rigtige aflæsninger og kan slettes igen;
-- tællerstande (energi_kwh) røres ikke.
CREATE TABLE IF NOT EXISTS sim_forbrug (
    maaler_id  INTEGER NOT NULL,
    ts         INTEGER NOT NULL,              -- 15-min-slot som i aflaesninger
    delta_kwh  REAL NOT NULL,
    PRIMARY KEY (maaler_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_sim_ts ON sim_forbrug(ts);

-- Alt forbrug (rigtigt + simuleret). Bruges af alle forbrugsberegninger.
CREATE VIEW IF NOT EXISTS alle_delta AS
    SELECT maaler_id, ts, delta_kwh FROM aflaesninger
    UNION ALL
    SELECT maaler_id, ts, delta_kwh FROM sim_forbrug;
"""

# Forbrugsgrupper jf. "Sådan afregnes vinterstrøm" (1. nov – 31. mar).
# "til" er øvre grænse i hele kWh (inkl.); sidste gruppe har ingen øvre grænse.
STANDARD_INDSTILLINGER = {
    "kategorier": [
        {"til": 20, "pris": 0},
        {"til": 100, "pris": 120},
        {"til": 300, "pris": 400},
        {"til": 700, "pris": 1000},
        {"til": 1500, "pris": 2200},
        {"til": None, "pris": 4500},
    ],
    "afregningsmaaneder": [11, 12, 1, 2, 3],
    "advarsel_procent": 90,          # advar når forbruget når X % af gruppens grænse
    "offline_minutter": 60,          # varsel når måler har været væk så længe
    "mail_tidspunkt": "06:00",       # daglige/ugentlige/månedlige opgørelser
    "ekstra_kontor_emails": "",      # fx kasserer – modtager kontor-mails
    "ekstra_mail_offline": True,
    "ekstra_mail_dag": False,
    "ekstra_mail_uge": False,
    "ekstra_mail_maaned": True,
    "bestyrelse_emails": "bestyrelse@aabenraa-sejlclub.dk",   # PDF-månedsrapport (kun afregningsmåneder)
}


def connect():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
    return c


@contextmanager
def db():
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SKEMA)
        for nr in range(1, config.METER_COUNT + 1):
            c.execute("INSERT OR IGNORE INTO maalere(id) VALUES (?)", (nr,))
            c.execute("INSERT OR IGNORE INTO status(maaler_id) VALUES (?)", (nr,))
        for k, v in STANDARD_INDSTILLINGER.items():
            c.execute("INSERT OR IGNORE INTO indstillinger VALUES (?, ?)", (k, json.dumps(v)))
        _migrer(c)


def _migrer(c):
    """Opdatér ældre databaser. Båd flyttet fra bruger til tildeling (en bruger kan have flere både)."""
    def kolonner(tabel):
        return {r[1] for r in c.execute(f"PRAGMA table_info({tabel})")}
    if "baad" not in kolonner("tildelinger"):
        c.execute("ALTER TABLE tildelinger ADD COLUMN baad TEXT NOT NULL DEFAULT ''")
        c.execute("UPDATE tildelinger SET baad=(SELECT b.baad FROM brugere b WHERE b.id=tildelinger.bruger_id)")
    if "baad" not in kolonner("maanedsopgoerelser"):
        c.execute("ALTER TABLE maanedsopgoerelser ADD COLUMN baad TEXT NOT NULL DEFAULT ''")
        c.execute("UPDATE maanedsopgoerelser SET baad=COALESCE("
                  "(SELECT b.baad FROM brugere b WHERE b.id=maanedsopgoerelser.bruger_id), '')")


def hent(noegle):
    with db() as c:
        r = c.execute("SELECT vaerdi FROM indstillinger WHERE noegle=?", (noegle,)).fetchone()
    if r is None:
        return STANDARD_INDSTILLINGER.get(noegle)
    return json.loads(r["vaerdi"])


def gem(noegle, vaerdi):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO indstillinger VALUES (?, ?)", (noegle, json.dumps(vaerdi)))


def log(type_, tekst, c=None):
    """Skriv i hændelsesloggen. Har kalderen en åben skrive-transaktion, så send den med som c."""
    from .tid import nu
    if c is not None:
        c.execute("INSERT INTO haendelser(ts, type, tekst) VALUES (?,?,?)", (nu(), type_, tekst))
        return
    with db() as c2:
        c2.execute("INSERT INTO haendelser(ts, type, tekst) VALUES (?,?,?)", (nu(), type_, tekst))
        c2.execute("DELETE FROM haendelser WHERE ts < ?", (nu() - 365 * 86400,))
