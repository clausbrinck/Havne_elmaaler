"""Systemadministration: databaseoplysninger, integritetstjek, optimering og rettelse af fejlmålt forbrug."""
import os
import sqlite3

from . import backup, config, db, simulering

TABELLER = ["maalere", "status", "aflaesninger", "sim_forbrug", "brugere", "tildelinger",
            "maanedsopgoerelser", "haendelser", "varsler_sendt", "tokens", "indstillinger"]


def _kb(sti):
    try:
        return os.path.getsize(sti) // 1024
    except OSError:
        return 0


def info():
    with db.db() as c:
        antal = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABELLER}
        afl = c.execute("SELECT MIN(ts), MAX(ts) FROM aflaesninger").fetchone()
    return {
        "sti": config.DB_PATH,
        "db_kb": _kb(config.DB_PATH),
        "wal_kb": _kb(config.DB_PATH + "-wal"),
        "antal": antal,
        "foerste": afl[0], "seneste": afl[1],
        "sqlite": sqlite3.sqlite_version,
    }


def integritet():
    with db.db() as c:
        return [r[0] for r in c.execute("PRAGMA integrity_check").fetchall()]


def optimer(bruger=""):
    """VACUUM + ANALYZE: genbruger tom plads og opdaterer statistik. Returnerer sparet plads i kB."""
    foer = _kb(config.DB_PATH) + _kb(config.DB_PATH + "-wal")
    c = db.connect()
    c.isolation_level = None   # VACUUM må ikke køre i en transaktion
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.execute("ANALYZE")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    efter = _kb(config.DB_PATH) + _kb(config.DB_PATH + "-wal")
    db.log("system", f"Database optimeret (VACUUM/ANALYZE) af {bruger}: {foer} kB -> {efter} kB")
    return foer - efter


def fejlmaalt_forbrug(maaler_id, fra_ts, til_ts):
    """Forbrug (kun rigtige aflæsninger) for en måler i (fra_ts, til_ts] – til forhåndsvisning."""
    with db.db() as c:
        r = c.execute("SELECT COALESCE(SUM(delta_kwh),0), COUNT(*) FROM aflaesninger "
                      "WHERE maaler_id=? AND ts>? AND ts<=? AND delta_kwh<>0", (maaler_id, fra_ts, til_ts)).fetchone()
    return r[0], r[1]


def nulstil_forbrug(maaler_id, fra_ts, til_ts, aarsag, bruger=""):
    """Sæt forbruget (delta_kwh) til 0 for en måler i (fra_ts, til_ts] – fx et fejlmålt spring.
    Tællerstande bevares. Der tages backup først, og berørte låste måneder genberegnes."""
    if til_ts <= fra_ts:
        raise ValueError("Til-tidspunkt skal være efter fra-tidspunkt.")
    if not 1 <= maaler_id <= config.METER_COUNT:
        raise ValueError("Målernummeret findes ikke.")
    kwh, n = fejlmaalt_forbrug(maaler_id, fra_ts, til_ts)
    if not n:
        raise ValueError("Der er intet forbrug at nulstille i den periode.")
    backup.lav_backup(f"før nulstilling af forbrug på måler {maaler_id:03d} ({bruger})")
    with db.db() as c:
        c.execute("UPDATE aflaesninger SET delta_kwh=0 WHERE maaler_id=? AND ts>? AND ts<=?",
                  (maaler_id, fra_ts, til_ts))
        db.log("system", f"Forbrug nulstillet på måler {maaler_id:03d}: {kwh:.2f} kWh i {n} aflæsninger "
                         f"fjernet. Årsag: {aarsag or '–'} (af {bruger})", c)
    laast = simulering._genberegn_laaste(simulering._maaneder(fra_ts, til_ts))
    return kwh, n, laast
