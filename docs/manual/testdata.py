"""Testdata til manualens skærmbilleder. Kør kun mod en testdatabase (DATA_DIR) med SOURCE=sim."""
import os
import random
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "elmaaler"))
from app import db, demodata, forbrug, sikkerhed, config  # noqa: E402
from app.tid import nu, dato_ts  # noqa: E402

demodata.DEMO = [
    ("hans@eksempel.dk", "Hans Hansen", "Havørnen", 64, 93),
    ("hans@eksempel.dk", "Hans Hansen", "Marlin", 138, 212),
    ("grete@eksempel.dk", "Grete Jensen", "Vestenvind", 420, 612),
]
demodata.main()

FORNAVNE = ["Anne", "Bo", "Peter", "Karen", "Jens", "Lone", "Mads", "Ole", "Mette", "Søren", "Birgit", "Henrik",
            "Kirsten", "Lars", "Susanne", "Niels", "Hanne", "Torben", "Inge", "Claus", "Bente", "Erik", "Pia",
            "Morten", "Lene", "Jørgen", "Tove", "Kim", "Rikke", "Poul"]
EFTERNAVNE = ["Nielsen", "Jensen", "Hansen", "Pedersen", "Andersen", "Christensen", "Larsen", "Sørensen",
              "Rasmussen", "Jørgensen", "Petersen", "Madsen", "Kristensen", "Olsen", "Thomsen", "Lauridsen",
              "Holm", "Krog", "Bach", "Mikkelsen", "Schmidt", "Iversen", "Damgaard", "Lund"]
BAADE = ["Solsikken", "Måsen", "Nordlys", "Tern", "Bølgen", "Ellen", "Frida", "Skarven", "Ternen", "Vibeke",
         "Lykken", "Albatros", "Sønderhav", "Havfruen", "Kaja", "Smilla", "Sirius", "Vega", "Freja", "Thor",
         "Rokken", "Ålen", "Hvidvingen", "Lærken", "Svanen", "Marie", "Brisen", "Stormy", "Fiskeørnen", "Sula"]

rnd = random.Random(11)
with db.db() as c:
    for nr in range(1, config.METER_COUNT + 1):
        c.execute("UPDATE maalere SET placering=? WHERE id=?", (f"S{(nr - 1) // 6 + 1:02d}-U{(nr - 1) % 6 + 1}", nr))
    brugt = {r[0] for r in c.execute("SELECT maaler_id FROM tildelinger")}
    frie = [n for n in range(1, 171) if n not in brugt]
    rnd.shuffle(frie)
    kode = sikkerhed.hash_kode("testtest1")
    for i, nr in enumerate(frie[:128]):
        navn = f"{rnd.choice(FORNAVNE)} {rnd.choice(EFTERNAVNE)}"
        email = navn.lower().replace(" ", ".").replace("ø", "oe").replace("æ", "ae").replace("å", "aa") + f"{i}@eksempel.dk"
        c.execute("INSERT INTO brugere(email, navn, pw_hash, oprettet, mail_advarsel, mail_maaned) VALUES (?,?,?,?,0,0)",
                  (email, navn, kode, nu()))
        bid = c.execute("SELECT id FROM brugere WHERE email=?", (email,)).fetchone()[0]
        start = date(2026, 10, 1) if i < 118 else date(2026, 11, rnd.randint(3, 18))
        c.execute("INSERT INTO tildelinger(maaler_id, bruger_id, start_dato, start_ts, oprettet, baad) VALUES (?,?,?,?,?,?)",
                  (nr, bid, start.isoformat(), dato_ts(start), nu(), rnd.choice(BAADE)))
    # havnekontoret
    c.execute("INSERT OR IGNORE INTO brugere(email, navn, rolle, pw_hash, oprettet) VALUES (?,?,?,?,?)",
              ("kontor@aabenraa-sejlclub.dk", "Havnekontoret", "kontor", kode, nu()))
    c.execute("UPDATE brugere SET navn='Claus (systemadministrator)' WHERE email='admin@aabenraa-sejlclub.dk'")
    # Hans: tydelige e-mail-valg
    c.execute("UPDATE brugere SET mail_advarsel=1, mail_maaned=1, mail_offline=1, mail_dag=0, mail_uge=1 "
              "WHERE email='hans@eksempel.dk'")
forbrug.laas_maaned("2026-10")
with db.db() as c:
    for r in c.execute("SELECT t.maaler_id, t.baad, b.email FROM tildelinger t JOIN brugere b ON b.id=t.bruger_id "
                       "WHERE b.email IN ('hans@eksempel.dk','grete@eksempel.dk')"):
        print(r["email"], r["baad"], r["maaler_id"])
