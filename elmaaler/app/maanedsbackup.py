"""Månedsbackup: én CSV-fil pr. måned med alt, der skal til for at genskabe systemet, hvis alle databasebackups fejler.

Pr. måler og tildeling i måneden: bruger (email, navn, telefon, rolle), båd, tildelingens start/slut,
perioden i måneden, tællerstand ved start og slut og forbrug (kWh) med gruppe og pris. Målere med aflæsninger
men uden tildeling er også med (tællerstanden), og til sidst brugere uden måler.

Filen kan importeres direkte under Brugere → Importér fra CSV (kolonnerne maaler, placering, email, navn,
telefon, baad, start_dato, slut_dato, rolle), så brugere og tildelinger genskabes. Tællerstandene viser,
hvor målerne stod, så forbruget efter backuppen kan beregnes ud fra målerens egen tæller.
"""
import csv
import io
from datetime import date, timedelta

from . import db, forbrug
from .rapport import dk
from .tid import dato_ts, i_dag, lokal, maaned_grænser, maaned_str, nu

KOLONNER = ["maaned", "maaler", "placering", "email", "navn", "telefon", "rolle", "baad",
            "start_dato", "slut_dato", "fra", "til", "taeller_start_kwh", "taeller_slut_kwh",
            "kwh", "gruppe", "pris_kr", "status"]


def _taeller(c, maaler_id, ts, senest):
    """Tællerstand ved tidspunktet ts: sidste aflæsning til og med ts, ellers første aflæsning før `senest`."""
    r = c.execute("SELECT energi_kwh FROM aflaesninger WHERE maaler_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                  (maaler_id, ts)).fetchone()
    if r is None:
        r = c.execute("SELECT energi_kwh FROM aflaesninger WHERE maaler_id=? AND ts>? AND ts<=? ORDER BY ts LIMIT 1",
                      (maaler_id, ts, senest)).fetchone()
    return None if r is None else r[0]


def data(maaned):
    """Rækker til månedsbackuppen (liste af dicts med KOLONNER) og om måneden er låst."""
    rows, laast = forbrug.maanedsopgoerelse(maaned)
    fra_m, til_m, _, _ = maaned_grænser(maaned)
    slut_m = min(til_m, nu())
    ud, med = [], set()
    with db.db() as c:
        brugere = {b["id"]: b for b in c.execute("SELECT * FROM brugere")}
        for r in rows:
            a = dato_ts(date.fromisoformat(r["fra"]))
            b = min(dato_ts(date.fromisoformat(r["til"]) + timedelta(days=1)), slut_m)
            t = None
            if r["bruger_id"]:
                t = c.execute("SELECT * FROM tildelinger WHERE maaler_id=? AND bruger_id=? AND start_ts<=? "
                              "AND (slut_ts IS NULL OR slut_ts>?) ORDER BY start_ts DESC LIMIT 1",
                              (r["maaler_id"], r["bruger_id"], a, a)).fetchone()
            br = brugere.get(r["bruger_id"]) or {}
            ud.append(_linje(c, maaned, r["maaler_id"], r["placering"], br, t, r["baad"], a, b,
                             r["kwh"], r["gruppe"], r["pris"], laast))
            med.add(r["maaler_id"])
        # målere med aflæsninger i måneden, der ikke allerede er med (ikke tildelt og næsten intet forbrug)
        for m in c.execute("SELECT DISTINCT a.maaler_id, m.placering FROM aflaesninger a JOIN maalere m "
                           "ON m.id=a.maaler_id WHERE a.ts>? AND a.ts<=? ORDER BY a.maaler_id",
                           (fra_m, slut_m)).fetchall():
            if m["maaler_id"] not in med:
                kwh = round(forbrug.forbrug(c, m["maaler_id"], fra_m, slut_m, live=False), 2)
                ud.append(_linje(c, maaned, m["maaler_id"], m["placering"], {}, None, "", fra_m, slut_m,
                                 kwh, None, None, laast))
        ud.sort(key=lambda x: (x["maaler"], x["fra"]))
        # brugere uden måler i måneden (fx havnekontoret og nye medlemmer)
        har = {r["bruger_id"] for r in rows if r["bruger_id"]}
        for b in sorted(brugere.values(), key=lambda b: (b["navn"].lower(), b["email"])):
            if b["id"] not in har and b["aktiv"]:
                ud.append({**{k: "" for k in KOLONNER}, "maaned": maaned, "email": b["email"], "navn": b["navn"],
                           "telefon": b["telefon"], "rolle": b["rolle"], "status": "uden måler"})
    return ud, laast


def _linje(c, maaned, maaler_id, placering, br, t, baad, a, b, kwh, gruppe, pris, laast):
    t0, t1 = _taeller(c, maaler_id, a, b), _taeller(c, maaler_id, b, b)
    return {
        "maaned": maaned, "maaler": f"{maaler_id:03d}", "placering": placering or "",
        "email": br["email"] if br else "", "navn": br["navn"] if br else "",
        "telefon": br["telefon"] if br else "", "rolle": br["rolle"] if br else "", "baad": baad or "",
        "start_dato": t["start_dato"] if t else "", "slut_dato": (t["slut_dato"] or "") if t else "",
        "fra": _dato(a), "til": _dato(b - 1),
        "taeller_start_kwh": "" if t0 is None else dk(t0, 3),
        "taeller_slut_kwh": "" if t1 is None else dk(t1, 3),
        "kwh": dk(kwh, 2), "gruppe": gruppe or "", "pris_kr": "" if pris is None else dk(pris, 0),
        "status": "låst" if laast else "foreløbig",
    }


def _dato(ts):
    return lokal(ts).date().isoformat()


def csv_fil(maaned):
    rows, _ = data(maaned)
    buf = io.StringIO()
    buf.write("﻿")   # BOM så Excel læser æøå korrekt
    w = csv.DictWriter(buf, fieldnames=KOLONNER, delimiter=";")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def filnavn(maaned):
    return f"elmaaler-maanedsbackup-{maaned}.csv"


def mailtekst(maaned):
    rows, laast = data(maaned)
    maalere = {r["maaler"] for r in rows if r["maaler"]}
    tildelt = sum(1 for r in rows if r["maaler"] and r["email"])
    return (f"Månedsbackup for {maaned} er vedhæftet ({filnavn(maaned)}).\n\n"
            f"{len(maalere)} målere med aflæsninger, {tildelt} tildelinger, "
            f"{sum(1 for r in rows if not r['maaler'])} brugere uden måler. "
            f"Opgørelsen er {'låst' if laast else 'foreløbig'}.\n\n"
            "Filen indeholder pr. måler: bruger, båd, tildelingens start/slut, tællerstand ved start og slut af "
            "måneden og forbrug i kWh med gruppe og pris.\n\n"
            "GEM DENNE MAIL. Hvis systemet og alle backups går tabt, kan filen importeres under "
            "Brugere → Importér fra CSV i et nyt system. Så genskabes brugere og tildelinger, og tællerstandene "
            "viser, hvor målerne stod ved månedens slutning.\n")


def seneste_hele_maaned():
    d = i_dag().replace(day=1) - timedelta(days=1)
    return maaned_str(d)
