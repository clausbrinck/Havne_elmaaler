"""Forbrugsberegninger og forbrugsgrupper (kategorier).

Princip: hver aflæsning gemmer delta_kwh = forbrug siden forrige aflæsning.
Forbrug i en periode (fra, til] = summen af delta_kwh med ts i perioden.
Det tåler måler-nulstilling/udskiftning og gør dag/uge/måned-summer enkle.
"""
import math
import re
from collections import defaultdict
from datetime import timedelta

from . import db
from .tid import nu, lokal, maaned_grænser, dato_ts


# ---------------------------------------------------------------- kategorier

def gruppe_label(kat, i):
    fra = 0 if i == 0 else kat[i - 1]["til"] + 1
    til = kat[i]["til"]
    if til is None:
        return f"over {fra - 1:,} kWh".replace(",", ".")
    return f"{fra:,}–{til:,} kWh".replace(",", ".")


def kategorier():
    kat = db.hent("kategorier")
    return [dict(k, nr=i + 1, label=gruppe_label(kat, i)) for i, k in enumerate(kat)]


def afregnes(maaned: str) -> bool:
    return int(maaned[5:7]) in db.hent("afregningsmaaneder")


def kategoriser(kwh: float, maaned: str) -> dict:
    """Placér et månedsforbrug i forbrugsgruppe. Afrundes til hele kWh."""
    kat = kategorier()
    afr = math.floor(max(kwh, 0) + 0.5)
    valgt = kat[-1]
    for k in kat:
        if k["til"] is None or afr <= k["til"]:
            valgt = k
            break
    a = afregnes(maaned)
    return {
        "gruppe": valgt["nr"],
        "label": valgt["label"],
        "graense": valgt["til"],
        "pris": valgt["pris"] if a else None,
        "afregnes": a,
        "afrundet": afr,
        "naeste": next((k for k in kat if k["nr"] == valgt["nr"] + 1), None),
    }


# ---------------------------------------------------------------- summer

def sum_delta(c, maaler_id, fra, til):
    r = c.execute("SELECT COALESCE(SUM(delta_kwh),0) FROM alle_delta "
                  "WHERE maaler_id=? AND ts>? AND ts<=?", (maaler_id, fra, til)).fetchone()
    return r[0]


def ikke_gemt(c, maaler_id):
    """Forbrug siden sidste gemte aflæsning (live-tillæg)."""
    st = c.execute("SELECT energi_kwh FROM status WHERE maaler_id=?", (maaler_id,)).fetchone()
    sid = c.execute("SELECT energi_kwh FROM aflaesninger WHERE maaler_id=? ORDER BY ts DESC LIMIT 1",
                    (maaler_id,)).fetchone()
    if not st or st[0] is None or not sid:
        return 0.0
    d = st[0] - sid[0]
    return d if 0 <= d < 5 else 0.0     # maks 15 min * 3,84 kW ≈ 1 kWh; ellers ignorér


def forbrug(c, maaler_id, fra, til, live=True):
    s = sum_delta(c, maaler_id, fra, til)
    if live and til > nu() >= fra:
        s += ikke_gemt(c, maaler_id)
    return s


def dagsserie(c, maaler_ids, fra_dato, til_dato):
    """Dagligt forbrug (kWh) for en liste af målere (None = alle) – dict dato -> kWh."""
    fra, til = dato_ts(fra_dato), dato_ts(til_dato)
    if maaler_ids is None:
        rows = c.execute("SELECT ts, SUM(delta_kwh) d FROM alle_delta WHERE ts>? AND ts<=? GROUP BY ts",
                         (fra, til)).fetchall()
    elif not maaler_ids:
        rows = []
    else:
        q = ",".join("?" * len(maaler_ids))
        rows = c.execute(f"SELECT ts, SUM(delta_kwh) d FROM alle_delta WHERE maaler_id IN ({q}) "
                         f"AND ts>? AND ts<=? GROUP BY ts", (*maaler_ids, fra, til)).fetchall()
    ud = defaultdict(float)
    d = fra_dato
    while d < til_dato:
        ud[d] = 0.0
        d += timedelta(days=1)
    for r in rows:
        ud[lokal(r["ts"] - 1).date()] += r["d"] or 0
    return dict(sorted(ud.items()))


def effektserie(c, timer=24):
    """Samlet gennemsnitseffekt (kW) pr. aflæsningsinterval, sidste N timer."""
    fra = nu() - timer * 3600
    rows = c.execute("SELECT ts, SUM(delta_kwh) d, COUNT(*) n FROM alle_delta WHERE ts>? GROUP BY ts ORDER BY ts",
                     (fra,)).fetchall()
    from .config import STORE_MINUTES
    return [(r["ts"], (r["d"] or 0) * 60 / STORE_MINUTES) for r in rows]


# ---------------------------------------------------------------- opgørelser

def _huller(fra, til, intervaller):
    """Dele af [fra, til) der ikke er dækket af intervallerne."""
    huller, pos = [], fra
    for a, b in sorted(intervaller):
        if a > pos:
            huller.append((pos, min(a, til)))
        pos = max(pos, b)
    if pos < til:
        huller.append((pos, til))
    return [(a, b) for a, b in huller if b > a]


def _sortering(r):
    """Ikke-tildelte sidst; navne sorteres naturligt (Medlem 2 før Medlem 10)."""
    navn = [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", (r["navn"] or "").lower())]
    return (r["bruger_id"] is None, navn, r["maaler_id"])


def beregn_maaned(maaned: str, live=True):
    """Beregn opgørelse for en måned: én række pr. tildeling (måler+bruger) i måneden,
    plus rækker for forbrug på målere uden tildeling."""
    fra, til, d0, d1 = maaned_grænser(maaned)
    rows = []
    with db.db() as c:
        tild = c.execute(
            "SELECT t.*, b.navn, b.email, m.placering FROM tildelinger t "
            "JOIN brugere b ON b.id=t.bruger_id JOIN maalere m ON m.id=t.maaler_id "
            "WHERE t.start_ts < ? AND (t.slut_ts IS NULL OR t.slut_ts > ?)", (til, fra)).fetchall()
        daekket = defaultdict(list)
        for t in tild:
            a = max(fra, t["start_ts"])
            b = min(til, t["slut_ts"] or til)
            daekket[t["maaler_id"]].append((a, b))
            kwh = forbrug(c, t["maaler_id"], a, b, live)
            rows.append({
                "maaned": maaned, "maaler_id": t["maaler_id"], "placering": t["placering"],
                "bruger_id": t["bruger_id"], "navn": t["navn"], "email": t["email"], "baad": t["baad"],
                "fra": lokal(a).date().isoformat(), "til": (lokal(b).date() - timedelta(days=1)).isoformat(),
                "kwh": round(kwh, 2), **kategoriser(kwh, maaned),
            })
        for m in c.execute("SELECT id, placering FROM maalere").fetchall():
            for a, b in _huller(fra, til, daekket.get(m["id"], [])):
                kwh = forbrug(c, m["id"], a, b, live)
                if kwh >= 0.5:
                    rows.append({
                        "maaned": maaned, "maaler_id": m["id"], "placering": m["placering"],
                        "bruger_id": None, "navn": "(ikke tildelt)", "email": "", "baad": "",
                        "fra": lokal(a).date().isoformat(),
                        "til": (lokal(b).date() - timedelta(days=1)).isoformat(),
                        "kwh": round(kwh, 2), **kategoriser(kwh, maaned),
                    })
    rows.sort(key=_sortering)
    return rows


def laas_maaned(maaned: str):
    """Gem (frys) opgørelsen for en afsluttet måned."""
    rows = beregn_maaned(maaned, live=False)
    with db.db() as c:
        c.execute("DELETE FROM maanedsopgoerelser WHERE maaned=?", (maaned,))
        for r in rows:
            c.execute("INSERT INTO maanedsopgoerelser(maaned, maaler_id, bruger_id, fra_dato, til_dato, kwh, "
                      "gruppe, pris, beregnet, baad) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (maaned, r["maaler_id"], r["bruger_id"], r["fra"], r["til"], r["kwh"],
                       r["gruppe"], r["pris"], nu(), r["baad"]))
    db.log("opgørelse", f"Opgørelse for {maaned} låst ({len(rows)} rækker)")
    return rows


def er_laast(maaned):
    with db.db() as c:
        return c.execute("SELECT MAX(beregnet) FROM maanedsopgoerelser WHERE maaned=?", (maaned,)).fetchone()[0]


def maanedsopgoerelse(maaned: str):
    """Låst opgørelse hvis den findes, ellers live-beregning."""
    with db.db() as c:
        gemt = c.execute(
            "SELECT o.*, b.navn, b.email, m.placering FROM maanedsopgoerelser o "
            "LEFT JOIN brugere b ON b.id=o.bruger_id JOIN maalere m ON m.id=o.maaler_id "
            "WHERE o.maaned=?", (maaned,)).fetchall()
    if not gemt:
        return beregn_maaned(maaned), False
    rows = []
    for g in gemt:
        k = kategoriser(g["kwh"], maaned)
        k["gruppe"], k["pris"] = g["gruppe"], g["pris"]   # brug de låste værdier
        rows.append({"maaned": maaned, "maaler_id": g["maaler_id"], "placering": g["placering"],
                     "bruger_id": g["bruger_id"], "navn": g["navn"] or "(ikke tildelt)",
                     "email": g["email"] or "", "baad": g["baad"] or "",
                     "fra": g["fra_dato"], "til": g["til_dato"], "kwh": g["kwh"], **k})
    rows.sort(key=_sortering)
    return rows, True


def gruppe_fordeling(rows):
    kat = kategorier()
    ud = {k["nr"]: {"label": k["label"], "pris": k["pris"], "antal": 0, "kwh": 0.0, "beloeb": 0.0} for k in kat}
    for r in rows:
        if r["bruger_id"] is None:
            continue
        g = ud[r["gruppe"]]
        g["antal"] += 1
        g["kwh"] += r["kwh"]
        g["beloeb"] += r["pris"] or 0
    return ud


# ---------------------------------------------------------------- brugere

def aktive_tildelinger(c, bruger_id=None, maaler_id=None):
    q = ("SELECT t.*, m.placering, b.navn, b.email FROM tildelinger t JOIN maalere m ON m.id=t.maaler_id "
         "JOIN brugere b ON b.id=t.bruger_id WHERE t.start_ts <= ? AND (t.slut_ts IS NULL OR t.slut_ts > ?)")
    args = [nu(), nu()]
    if bruger_id is not None:
        q += " AND t.bruger_id=?"
        args.append(bruger_id)
    if maaler_id is not None:
        q += " AND t.maaler_id=?"
        args.append(maaler_id)
    return c.execute(q + " ORDER BY t.maaler_id", args).fetchall()


def bruger_maaned_status(c, tildeling, maaned):
    """Forbrug i indeværende måned for én tildeling + gruppe + prognose."""
    fra, til, d0, d1 = maaned_grænser(maaned)
    a = max(fra, tildeling["start_ts"])
    b = min(til, tildeling["slut_ts"] or til)
    kwh = forbrug(c, tildeling["maaler_id"], a, b)
    k = kategoriser(kwh, maaned)
    gaaet = max(nu() - a, 1)
    prognose = kwh * (b - a) / gaaet if nu() < b else kwh
    return {"kwh": kwh, "prognose": prognose, "prognose_kat": kategoriser(prognose, maaned), **k}


def bruger_historik(bruger_id):
    """Låste månedsopgørelser for brugeren."""
    with db.db() as c:
        rows = c.execute("SELECT * FROM maanedsopgoerelser WHERE bruger_id=? ORDER BY maaned DESC, maaler_id",
                         (bruger_id,)).fetchall()
    ud = []
    for r in rows:
        k = kategoriser(r["kwh"], r["maaned"])
        ud.append({**dict(r), "label": k["label"], "afregnes": k["afregnes"]})
    return ud
