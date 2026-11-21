"""Opret 3 demo-brugere med simulerede elmålinger for oktober og november.

Hver båd har sin egen måler og opgøres for sig (både oktober og november):

    Demo-bruger 1, Lille Måge:  0–20 kWh       (1. gruppe, 0 kr)
    Demo-bruger 2, Havørn:      101–300 kWh    (3. gruppe, 400 kr)
    Demo-bruger 2, Jollen Pip:  0–20 kWh       (1. gruppe, 0 kr)   <- samme bruger, båd nr. 2
    Demo-bruger 3, Nordlys:     701–1.500 kWh  (5. gruppe, 2.200 kr)

Kør med appen STOPPET og med samme DATA_DIR/SOURCE som appen:

    python -m app.demodata

Scriptet kan køres igen – det overskriver blot demodata. Brugernes kodeord er DEMO_KODE nedenfor.
Der vælges målere som simulatoren ikke selv giver forbrug ("tom"-profil), så tallene står fast.
"""
import random
from datetime import date

from . import collector, config, db, forbrug, sikkerhed
from .tid import nu, dato_ts, lokal, maaned_grænser

DEMO_KODE = "testtest1"
START = date(2026, 10, 1)

# én linje pr. båd: (email, navn, båd, kWh i oktober, kWh i november indtil nu)
DEMO = [
    ("demo1@example.com", "Demo-bruger 1 (0–20 kWh)", "Lille Måge", 8, 11),
    ("demo2@example.com", "Demo-bruger 2 (to både)", "Havørn", 185, 140),
    ("demo2@example.com", "Demo-bruger 2 (to både)", "Jollen Pip", 12, 9),
    ("demo3@example.com", "Demo-bruger 3 (701–1.500 kWh)", "Nordlys", 1100, 790),
]


def _vaegt(ts, rnd):
    """Relativ belastning i et 15-min-interval: mere om dagen/aftenen end om natten."""
    t = lokal(ts).hour
    basis = 0.6 if t < 6 else 1.3 if 17 <= t < 22 else 1.0
    return basis * rnd.uniform(0.5, 1.5)


def _vaelg_maalere(c, sim):
    """Målere med simulator-profilen 'tom' og uden tildelinger fra andre end demo-brugerne."""
    demo_ids = [r[0] for r in c.execute(
        f"SELECT id FROM brugere WHERE email IN ({','.join('?' * len(DEMO))})", [d[0] for d in DEMO])]
    fri = []
    for nr in range(config.METER_COUNT, 0, -1):
        if sim.m[nr]["profil"] != "tom":
            continue
        andre = c.execute(
            f"SELECT 1 FROM tildelinger WHERE maaler_id=? AND bruger_id NOT IN ({','.join('?' * len(demo_ids)) or '-1'})",
            (nr, *demo_ids)).fetchone()
        if not andre:
            fri.append(nr)
        if len(fri) == len(DEMO):
            return fri
    raise SystemExit(f"Fandt ikke {len(DEMO)} ledige målere med 'tom'-profil.")


def main():
    db.init()
    if config.SOURCE != "sim":
        raise SystemExit("Demodata er kun til testmiljøet (SOURCE=sim).")
    sim = collector.Simulator()
    if config.SIM_BACKFILL_DAYS:
        sim.backfill(config.SIM_BACKFILL_DAYS)   # gør intet hvis der allerede er historik

    step = config.STORE_MINUTES * 60
    fra = dato_ts(START)
    slut = nu() // step * step
    okt_fra, okt_til, _, _ = maaned_grænser("2026-10")
    nov_fra, nov_til, _, _ = maaned_grænser("2026-11")
    rnd = random.Random(7)

    with db.db() as c:
        maalere = _vaelg_maalere(c, sim)
        # brugere (opret eller opdatér) – fjern deres gamle demo-tildelinger
        bruger_id = {}
        for email, navn, *_ in DEMO:
            if email in bruger_id:
                continue
            c.execute("INSERT OR IGNORE INTO brugere(email, oprettet) VALUES (?, ?)", (email, nu()))
            c.execute("UPDATE brugere SET navn=?, rolle='bruger', aktiv=1, pw_hash=?, "
                      "mail_advarsel=0, mail_dag=0, mail_uge=0, mail_maaned=0, mail_offline=0 WHERE email=?",
                      (navn, sikkerhed.hash_kode(DEMO_KODE), email))
            bruger_id[email] = c.execute("SELECT id FROM brugere WHERE email=?", (email,)).fetchone()[0]
            c.execute("DELETE FROM tildelinger WHERE bruger_id=?", (bruger_id[email],))

        for (email, navn, baad, okt, nov), nr in zip(DEMO, maalere):
            # tildeling fra 1. oktober – båden står på tildelingen
            c.execute("DELETE FROM tildelinger WHERE maaler_id=?", (nr,))
            c.execute("INSERT INTO tildelinger(maaler_id, bruger_id, start_dato, start_ts, oprettet, baad) "
                      "VALUES (?,?,?,?,?,?)", (nr, bruger_id[email], START.isoformat(), fra, nu(), baad))

            # aflæsninger: erstat alt fra 1. oktober
            forr = (c.execute("SELECT energi_kwh FROM aflaesninger WHERE maaler_id=? AND ts<? "
                              "ORDER BY ts DESC LIMIT 1", (nr, fra)).fetchone()
                    or c.execute("SELECT energi_kwh FROM aflaesninger WHERE maaler_id=? "
                                 "ORDER BY ts LIMIT 1", (nr,)).fetchone())
            c.execute("DELETE FROM aflaesninger WHERE maaler_id=? AND ts>=?", (nr, fra))
            slots = list(range(fra, slut + 1, step))
            vaegt = {ts: _vaegt(ts, rnd) for ts in slots}
            delta = {ts: 0.0 for ts in slots}
            for a, b, maal in ((okt_fra, okt_til, okt), (nov_fra, nov_til, nov)):
                i_periode = [ts for ts in slots if a < ts <= b]
                total = sum(vaegt[ts] for ts in i_periode)
                for ts in i_periode:
                    delta[ts] = maal * vaegt[ts] / total
            e = forr[0] if forr else round(sim.m[nr]["energi"], 1)
            rows = []
            for ts in slots:
                e += delta[ts]
                rows.append((nr, ts, round(e, 4), round(delta[ts], 4)))
            c.executemany("INSERT INTO aflaesninger VALUES (?,?,?,?)", rows)
            c.execute("UPDATE status SET energi_kwh=?, effekt_w=0, tilgaengelig=1, sidst_set=?, "
                      "offline_siden=NULL, offline_varslet=0, opdateret=? WHERE maaler_id=?",
                      (round(e, 4), slut, nu(), nr))
            db.log("bruger", f"Demodata: {email}, {baad} på måler {nr:03d} (okt {okt} kWh, nov {nov} kWh)", c)

    forbrug.laas_maaned("2026-10")   # frys oktober med de nye tal

    print(f"Demo-brugere oprettet (kodeord: {DEMO_KODE}):")
    for (email, navn, baad, okt, nov), nr in zip(DEMO, maalere):
        ko, kn = forbrug.kategoriser(okt, "2026-10"), forbrug.kategoriser(nov, "2026-11")
        print(f"  {email:20} {baad:11} måler {nr:03d}  okt {okt:>5} kWh -> gruppe {ko['gruppe']} ({ko['label']})"
              f"   nov {nov:>5} kWh -> gruppe {kn['gruppe']} ({kn['label']}, {kn['pris']:.0f} kr)")


if __name__ == "__main__":
    main()
