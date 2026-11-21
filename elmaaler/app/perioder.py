"""Forbrug pr. periode til de interaktive grafer: døgn, uge, måned og vinter.

Et døgn går altid fra kl. 00:00 til 24:00 dansk tid. Uger starter mandag.
Vintersæsonen er 1. november – 31. marts.
"""
from bisect import bisect_left
from datetime import date, timedelta

from . import forbrug
from .tid import MAANEDER, UGEDAGE, dato_ts, i_dag, lokal, maaned_navn, maaned_str, nu

VISNINGER = ("dag", "uge", "maaned", "vinter")
UGEDAG_LANG = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


def _dk(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _periode(visning, d):
    """-> (start, slut, forrige, naeste) som datoer. slut er eksklusiv."""
    if visning == "dag":
        return d, d + timedelta(days=1), d - timedelta(days=1), d + timedelta(days=1)
    if visning == "uge":
        s = d - timedelta(days=d.weekday())
        return s, s + timedelta(days=7), s - timedelta(days=7), s + timedelta(days=7)
    if visning == "maaned":
        s = d.replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1)
        return s, e, (s - timedelta(days=1)).replace(day=1), e
    aar = d.year if d.month >= 11 else d.year - 1          # vinter
    return date(aar, 11, 1), date(aar + 1, 4, 1), date(aar - 1, 11, 1), date(aar + 1, 11, 1)


def _spande(visning, start, slut):
    """Søjlerne: liste af dict(l=etiket, t=tooltip-tekst, ned=(visning, dato)|None) og tidsgrænserne."""
    spande, grenser = [], []
    if visning == "dag":
        t, e = dato_ts(start), dato_ts(slut)
        while t < e:                       # pr. time; sommertid giver 23 eller 25 timer
            h = lokal(t).hour
            spande.append({"l": f"{h:02d}", "t": f"kl. {h:02d}:00–{h + 1:02d}:00", "ned": None})
            grenser.append(t)
            t += 3600
        grenser.append(e)
    elif visning in ("uge", "maaned"):
        d = start
        while d < slut:
            wd = d.weekday()
            spande.append({"l": f"{UGEDAGE[wd]} {d.day}" if visning == "uge" else str(d.day),
                           "t": f"{UGEDAG_LANG[wd]} {d:%d-%m-%Y}", "ned": ["dag", d.isoformat()]})
            grenser.append(dato_ts(d))
            d += timedelta(days=1)
        grenser.append(dato_ts(slut))
    else:
        d = start
        while d < slut:
            m = maaned_str(d)
            spande.append({"l": MAANEDER[d.month - 1][:3], "t": maaned_navn(m), "ned": ["maaned", d.isoformat()],
                           "maaned": m})
            grenser.append(dato_ts(d))
            d = (d + timedelta(days=32)).replace(day=1)
        grenser.append(dato_ts(slut))
    return spande, grenser


def _summer(c, maaler_id, grenser, tilladt):
    """kWh pr. spand. maaler_id None = alle målere. tilladt = liste af (fra_ts, til_ts|None) eller None."""
    fra, til = grenser[0], grenser[-1]
    if maaler_id is None:
        rows = c.execute("SELECT ts, SUM(delta_kwh) FROM alle_delta WHERE ts>? AND ts<=? GROUP BY ts", (fra, til))
    else:
        rows = c.execute("SELECT ts, delta_kwh FROM alle_delta WHERE maaler_id=? AND ts>? AND ts<=?",
                         (maaler_id, fra, til))
    v = [0.0] * (len(grenser) - 1)
    for r in rows:
        ts = r[0]
        if tilladt is not None and not any(a < ts <= (b or ts) for a, b in tilladt):
            continue
        v[bisect_left(grenser, ts) - 1] += r[1] or 0       # ts ligger i (grenser[i], grenser[i+1]]
    return v


def _foerste(c, maaler_id, tilladt):
    """Tidligste tidspunkt der kan bladres tilbage til."""
    if tilladt:
        return min(a for a, _ in tilladt)
    if maaler_id is None:
        r = c.execute("SELECT MIN(ts) FROM alle_delta").fetchone()
    else:
        r = c.execute("SELECT MIN(ts) FROM alle_delta WHERE maaler_id=?", (maaler_id,)).fetchone()
    return r[0] or nu()


def serie(c, maaler_id, visning, d, tilladt=None):
    """Data til én visning af periode-grafen (bruges af /api/forbrug)."""
    idag = i_dag()
    foerste = lokal(_foerste(c, maaler_id, tilladt)).date()
    d = max(min(d, idag), foerste)
    start, slut, forrige, naeste = _periode(visning, d)
    spande, grenser = _spande(visning, start, slut)
    vaerdier = _summer(c, maaler_id, grenser, tilladt)
    t_nu = nu()
    total = sum(vaerdier)
    ekstra = ""
    vinterpris = 0
    for i, (s, v) in enumerate(zip(spande, vaerdier)):
        s["v"] = round(v, 3)
        s["nu"] = grenser[i] <= t_nu < grenser[i + 1]
        if s.pop("maaned", None) and maaler_id is not None:
            k = forbrug.kategoriser(v, spande[i]["ned"][1][:7])
            if k["afregnes"] and grenser[i] <= t_nu:
                s["x"] = f"Gruppe {k['label']} · {_dk(k['pris'])} kr" + (" (foreløbig)" if s["nu"] else "")
                vinterpris += k["pris"]
        if grenser[i] > t_nu:
            s["ned"] = None                  # ingen detaljer for fremtiden

    if visning == "dag":
        titel = f"{UGEDAG_LANG[start.weekday()].capitalize()} {start:%d-%m-%Y}" + (" (i dag)" if start == idag else "")
    elif visning == "uge":
        titel = f"Uge {start.isocalendar()[1]} · {start:%d-%m} – {slut - timedelta(days=1):%d-%m-%Y}"
    elif visning == "maaned":
        m = maaned_str(start)
        titel = maaned_navn(m).capitalize()
        if maaler_id is not None:
            k = forbrug.kategoriser(total, m)
            if k["afregnes"]:
                ekstra = f"Forbrugsgruppe {k['label']} · {_dk(k['pris'])} kr" + (" (foreløbig)" if slut > idag else "")
    else:
        titel = f"Vinter {start.year}/{(start.year + 1) % 100:02d} (nov–mar)"
        if maaler_id is not None and vinterpris:
            ekstra = f"Pris for vinteren: {_dk(vinterpris)} kr" + (" (foreløbig)" if slut > idag else "")

    return {
        "visning": visning, "dato": start.isoformat(), "titel": titel,
        "total": round(total, 3), "ekstra": ekstra,
        "enhed": "kWh pr. time" if visning == "dag" else "kWh pr. måned" if visning == "vinter" else "kWh pr. dag",
        "forrige": forrige.isoformat() if start > foerste else None,
        "naeste": naeste.isoformat() if naeste <= idag else None,
        "er_nu": start <= idag < slut,
        "hver": 3 if visning == "dag" else 2 if len(spande) > 16 else 1,
        "punkter": spande,
    }
