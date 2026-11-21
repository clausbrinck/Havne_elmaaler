"""Simuleret forbrug (debug-siden på havnekontoret).

Simuleret forbrug gemmes i tabel `sim_forbrug` og lægges oven i de rigtige aflæsninger via
viewet `alle_delta`. Tællerstande røres ikke, så alt kan slettes igen uden spor.
Så længe der findes simuleret forbrug, sendes der ikke mails til medlemmer (se jobs.py).
"""
import random
from datetime import date, timedelta

from . import config, db, forbrug
from .tid import nu, dato_ts, lokal, maaned_str

MAKS_KW = 3.84 * 1.25   # samme grænse som opsamlingen: 16 A × 240 V + margen


def aktiv(c=None) -> bool:
    if c is not None:
        return c.execute("SELECT 1 FROM sim_forbrug LIMIT 1").fetchone() is not None
    with db.db() as c2:
        return c2.execute("SELECT 1 FROM sim_forbrug LIMIT 1").fetchone() is not None


def parse_maalere(tekst: str) -> list:
    """'12, 14, 20-25' -> [12, 14, 20, 21, 22, 23, 24, 25]"""
    ud = set()
    for del_ in tekst.replace(";", ",").replace(" ", ",").split(","):
        if not del_:
            continue
        if "-" in del_:
            a, b = (int(x) for x in del_.split("-", 1))
            ud.update(range(min(a, b), max(a, b) + 1))
        else:
            ud.add(int(del_))
    if not ud:
        raise ValueError("Angiv mindst én måler.")
    forkerte = [n for n in ud if not 1 <= n <= config.METER_COUNT]
    if forkerte:
        raise ValueError(f"Målernummer findes ikke: {', '.join(map(str, sorted(forkerte)))}")
    return sorted(ud)


def _vaegt(ts, profil, rnd):
    if profil == "jaevn":
        return 1.0
    t = lokal(ts).hour
    basis = 0.5 if t < 6 else 1.4 if 17 <= t < 22 else 1.0
    return basis * rnd.uniform(0.6, 1.4)


def _maaneder(fra_ts, til_ts):
    """Måneder som perioden (fra_ts, til_ts] berører."""
    ud, d = [], lokal(fra_ts).date().replace(day=1)
    slut = lokal(til_ts - 1).date()
    while d <= slut:
        ud.append(maaned_str(d))
        d = (d + timedelta(days=32)).replace(day=1)
    return ud


def _genberegn_laaste(maaneder):
    """Genberegn låste måneder, så de passer med det simulerede forbrug. Kaldes EFTER commit."""
    gjort = []
    for m in sorted(set(maaneder)):
        if forbrug.er_laast(m):
            forbrug.laas_maaned(m)
            gjort.append(m)
    return gjort


def tilfoej(maalere, fra: date, til: date, kwh: float, profil="dag", bruger=""):
    """Læg kwh (pr. måler) ud over perioden fra..til (begge dage inkl.) i 15-min-slots."""
    if til < fra:
        raise ValueError("Til-dato skal være samme dag som eller efter fra-dato.")
    if kwh <= 0:
        raise ValueError("kWh skal være større end 0.")
    step = config.STORE_MINUTES * 60
    a = dato_ts(fra)
    b = min(dato_ts(til + timedelta(days=1)), nu() // step * step)
    slots = list(range(a + step, b + 1, step))
    if not slots:
        raise ValueError("Perioden ligger i fremtiden – vælg datoer til og med i dag.")
    timer = len(slots) * step / 3600
    if kwh / timer > MAKS_KW:
        raise ValueError(f"{kwh:g} kWh på {timer:.0f} timer svarer til {kwh / timer:.1f} kW i snit – "
                         f"mere end en stander kan levere ({MAKS_KW:.1f} kW). Vælg en længere periode.")
    rnd = random.Random()
    with db.db() as c:
        for nr in maalere:
            vaegt = [_vaegt(ts, profil, rnd) for ts in slots]
            total = sum(vaegt)
            c.executemany(
                "INSERT INTO sim_forbrug(maaler_id, ts, delta_kwh) VALUES (?,?,?) "
                "ON CONFLICT(maaler_id, ts) DO UPDATE SET delta_kwh = delta_kwh + excluded.delta_kwh",
                [(nr, ts, kwh * v / total) for ts, v in zip(slots, vaegt)])
        db.log("debug", f"Simuleret forbrug: {kwh:g} kWh pr. måler på {len(maalere)} måler(e) "
                        f"({', '.join(f'{n:03d}' for n in maalere[:20])}{' …' if len(maalere) > 20 else ''}) "
                        f"{fra} – {til} (af {bruger})", c)
    return _genberegn_laaste(_maaneder(a, b))


def slet(maaler_id=None, bruger=""):
    """Slet simuleret forbrug på én måler (eller alle). Returnerer genberegnede låste måneder."""
    with db.db() as c:
        hvor, args = ("WHERE maaler_id=?", (maaler_id,)) if maaler_id else ("", ())
        r = c.execute(f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM sim_forbrug {hvor}", args).fetchone()
        if not r[2]:
            return []
        c.execute(f"DELETE FROM sim_forbrug {hvor}", args)
        db.log("debug", f"Simuleret forbrug slettet på {'måler %03d' % maaler_id if maaler_id else 'alle målere'} "
                        f"(af {bruger})", c)
    return _genberegn_laaste(_maaneder(r[0] - 1, r[1]))


def oversigt():
    with db.db() as c:
        rows = c.execute("SELECT s.maaler_id, COUNT(*) n, SUM(s.delta_kwh) kwh, MIN(s.ts) fra, MAX(s.ts) til, "
                         "m.placering FROM sim_forbrug s JOIN maalere m ON m.id=s.maaler_id "
                         "GROUP BY s.maaler_id ORDER BY s.maaler_id").fetchall()
    return [{**dict(r), "fra_dato": lokal(r["fra"] - 1).date(), "til_dato": lokal(r["til"] - 1).date()} for r in rows]
