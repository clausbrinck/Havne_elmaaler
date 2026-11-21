"""CSV-udtræk og tekster til emails."""
import csv
import io
from datetime import timedelta

from . import forbrug
from .tid import maaned_navn, fmt_dato, maaned_str, naeste_maaned


def dk(x, dec=1):
    if x is None:
        return "–"
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def opgoerelse_csv(maaned):
    rows, laast = forbrug.maanedsopgoerelse(maaned)
    buf = io.StringIO()
    buf.write("﻿")   # BOM så Excel læser æøå korrekt
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Måned", "Måler", "Placering", "Navn", "Email", "Båd", "Fra", "Til",
                "kWh", "Gruppe", "Gruppe (kWh)", "Pris (kr)", "Afregnes", "Status"])
    for r in rows:
        w.writerow([maaned, f"{r['maaler_id']:03d}", r["placering"], r["navn"], r["email"], r["baad"],
                    r["fra"], r["til"], dk(r["kwh"], 2), r["gruppe"], r["label"],
                    "" if r["pris"] is None else dk(r["pris"], 0),
                    "ja" if r["afregnes"] else "nej", "låst" if laast else "foreløbig"])
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------- brugermails

def _maaler_linje(t):
    navn = f"Måler {t['maaler_id']:03d}" + (f" ({t['placering']})" if t["placering"] else "")
    baad = t["baad"] if "baad" in t.keys() else ""
    return f"{baad} – {navn}" if baad else navn


def bruger_dag(c, bruger, tild, dato):
    maaned = maaned_str(dato)
    linjer = [f"Hej {bruger['navn'] or ''}".strip() + ",", "",
              f"Dit strømforbrug {fmt_dato(dato.isoformat())}:", ""]
    for t in tild:
        serie = forbrug.dagsserie(c, [t["maaler_id"]], dato, dato + timedelta(days=1))
        st = forbrug.bruger_maaned_status(c, t, maaned)
        linjer.append(f"{_maaler_linje(t)}: {dk(sum(serie.values()))} kWh")
        linjer.append(f"   Hidtil i {maaned_navn(maaned)}: {dk(st['kwh'])} kWh – gruppe {st['label']}"
                      + (f" ({dk(st['pris'], 0)} kr)" if st["afregnes"] else ""))
    return "\n".join(linjer)


def bruger_uge(c, bruger, tild, mandag):
    linjer = [f"Hej {bruger['navn'] or ''}".strip() + ",", "",
              f"Dit strømforbrug uge {mandag.isocalendar()[1]} "
              f"({fmt_dato(mandag.isoformat())} – {fmt_dato((mandag + timedelta(days=6)).isoformat())}):", ""]
    for t in tild:
        serie = forbrug.dagsserie(c, [t["maaler_id"]], mandag, mandag + timedelta(days=7))
        linjer.append(f"{_maaler_linje(t)}: i alt {dk(sum(serie.values()))} kWh")
        for d, v in serie.items():
            linjer.append(f"   {['man','tir','ons','tor','fre','lør','søn'][d.weekday()]} {d:%d-%m}: {dk(v)} kWh")
        linjer.append("")
    return "\n".join(linjer)


def bruger_maaned(bruger, rows, maaned):
    linjer = [f"Hej {bruger['navn'] or ''}".strip() + ",", "",
              f"Opgørelse af dit strømforbrug for {maaned_navn(maaned)}:", ""]
    for r in rows:
        linjer.append(f"Måler {r['maaler_id']:03d}" + (f" ({r['placering']})" if r["placering"] else "")
                      + f": {dk(r['kwh'])} kWh ({fmt_dato(r['fra'])} – {fmt_dato(r['til'])})")
        if r["afregnes"]:
            linjer.append(f"   Forbrugsgruppe {r['label']}: {dk(r['pris'], 0)} kr")
        else:
            linjer.append("   Måneden ligger uden for vinterafregningen.")
    if any(r["afregnes"] for r in rows):
        linjer += ["", "Det samlede beløb for vinterperioden opkræves ved standerhejsning."]
    return "\n".join(linjer)


def bruger_periode(c, bruger, fra_dato, til_dato):
    """Forbrugsrapport bestilt på Min side for perioden fra_dato..til_dato (begge inkl.).
    Returnerer (mailtekst, CSV-bytes) – CSV er None hvis brugeren ingen måler havde i perioden."""
    fra, til = forbrug.dato_ts(fra_dato), forbrug.dato_ts(til_dato + timedelta(days=1))
    tild = c.execute("SELECT t.*, m.placering FROM tildelinger t JOIN maalere m ON m.id=t.maaler_id "
                     "WHERE t.bruger_id=? AND t.start_ts<? AND (t.slut_ts IS NULL OR t.slut_ts>?) "
                     "ORDER BY t.maaler_id, t.start_ts", (bruger["id"], til, fra)).fetchall()
    if not tild:
        return "", None
    buf = io.StringIO()
    buf.write("﻿")   # BOM så Excel læser æøå korrekt
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Dato", "Måler", "Placering", "Båd", "kWh"])
    linjer = [f"Hej {bruger['navn'] or ''}".strip() + ",", "",
              f"Her er dit strømforbrug {fmt_dato(fra_dato.isoformat())} – {fmt_dato(til_dato.isoformat())}.", ""]
    total = 0.0
    for t in tild:
        a, b = max(fra, t["start_ts"]), min(til, t["slut_ts"] or til)
        d0, d1 = forbrug.lokal(a).date(), forbrug.lokal(b).date()
        serie = forbrug.dagsserie(c, [t["maaler_id"]], d0, d1)
        kwh = sum(serie.values())
        total += kwh
        linjer.append(f"{_maaler_linje(t)}: {dk(kwh)} kWh"
                      + (f" ({fmt_dato(d0.isoformat())} – {fmt_dato((d1 - timedelta(days=1)).isoformat())})"
                         if (a, b) != (fra, til) else ""))
        # forbrugsgruppe for hver måned perioden rører (hele måneden på tildelingen)
        m = maaned_str(d0)
        while m <= maaned_str(d1 - timedelta(days=1)):
            laast = c.execute("SELECT kwh, gruppe, pris FROM maanedsopgoerelser WHERE maaned=? AND maaler_id=? "
                              "AND bruger_id=? AND fra_dato>=? ORDER BY fra_dato LIMIT 1",
                              (m, t["maaler_id"], bruger["id"], t["start_dato"])).fetchone()
            if laast:
                k = forbrug.kategoriser(laast["kwh"], m)
                mk, pris, note = laast["kwh"], laast["pris"], ""
            else:
                k = forbrug.bruger_maaned_status(c, t, m)
                mk, pris, note = k["kwh"], k["pris"], " (foreløbig)"
            linjer.append(f"   {maaned_navn(m).capitalize()}: {dk(mk)} kWh – gruppe {k['label']}"
                          + (f", {dk(pris, 0)} kr" if k["afregnes"] and pris is not None else ", ikke afregnet")
                          + note)
            m = naeste_maaned(m)
        if len(serie) <= 62:
            for d, v in serie.items():
                linjer.append(f"      {['man','tir','ons','tor','fre','lør','søn'][d.weekday()]} {d:%d-%m}: {dk(v)} kWh")
        linjer.append("")
        for d, v in serie.items():
            w.writerow([d.strftime("%d-%m-%Y"), f"{t['maaler_id']:03d}", t["placering"], t["baad"], dk(v, 3)])
    if len(tild) > 1:
        linjer.append(f"I alt: {dk(total)} kWh")
        linjer.append("")
    linjer.append("Forbruget pr. dag er også vedhæftet som CSV-fil (kan åbnes i Excel).")
    return "\n".join(linjer), buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------- kontormails

def kontor_periode(c, fra_dato, til_dato, titel):
    serie = forbrug.dagsserie(c, None, fra_dato, til_dato)
    total = sum(serie.values())
    st = c.execute("SELECT SUM(tilgaengelig=1) online, SUM(tilgaengelig=0 AND sidst_set IS NOT NULL) offline, "
                   "SUM(COALESCE(effekt_w,0)) effekt FROM status s JOIN maalere m ON m.id=s.maaler_id "
                   "WHERE m.aktiv=1").fetchone()
    fra, til = (forbrug.dato_ts(fra_dato), forbrug.dato_ts(til_dato))
    top = c.execute("SELECT maaler_id, SUM(delta_kwh) k FROM alle_delta WHERE ts>? AND ts<=? "
                    "GROUP BY maaler_id ORDER BY k DESC LIMIT 10", (fra, til)).fetchall()
    linjer = [titel, "", f"Samlet forbrug alle målere: {dk(total)} kWh"]
    if len(serie) > 1:
        linjer.append("")
        for d, v in serie.items():
            linjer.append(f"   {d:%d-%m}: {dk(v)} kWh")
    linjer += ["", f"Målere online: {st['online'] or 0}   offline: {st['offline'] or 0}",
               f"Samlet effekt lige nu: {dk((st['effekt'] or 0) / 1000, 2)} kW", "",
               "Største forbrug i perioden:"]
    for r in top:
        tl = forbrug.aktive_tildelinger(c, maaler_id=r["maaler_id"])
        hvem = tl[0]["navn"] if tl else "ikke tildelt"
        linjer.append(f"   Måler {r['maaler_id']:03d}: {dk(r['k'])} kWh – {hvem}")
    return "\n".join(linjer)


def kontor_maaned(rows, maaned):
    fordeling = forbrug.gruppe_fordeling(rows)
    total = sum(r["kwh"] for r in rows)
    ikke = [r for r in rows if r["bruger_id"] is None]
    linjer = [f"Månedsopgørelse {maaned_navn(maaned)}", "",
              f"Samlet forbrug: {dk(total)} kWh fordelt på {len(rows)} rækker.", ""]
    if forbrug.afregnes(maaned):
        linjer.append("Forbrugsgrupper:")
        for nr, g in fordeling.items():
            linjer.append(f"   {g['label']:>18}: {g['antal']:>3} stk  {dk(g['kwh'], 0):>8} kWh  {dk(g['beloeb'], 0):>8} kr")
        linjer.append(f"   {'I alt':>18}: {sum(g['antal'] for g in fordeling.values()):>3} stk  "
                      f"{'':>8}      {dk(sum(g['beloeb'] for g in fordeling.values()), 0):>8} kr")
    else:
        linjer.append("Måneden ligger uden for vinterafregningen – ingen opkrævning.")
    if ikke:
        linjer += ["", f"OBS: {len(ikke)} målere har forbrug uden tildelt bruger "
                       f"({dk(sum(r['kwh'] for r in ikke))} kWh)."]
    linjer += ["", "Fuld opgørelse er vedhæftet som CSV (kan åbnes i Excel)."]
    return "\n".join(linjer)
