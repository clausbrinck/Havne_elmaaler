"""Opsamling af tællerstand (kWh), effekt (W), strøm (A), spænding (V) og frekvens (Hz) fra Home Assistant.

Home Assistant får målerne fra ZHA (Zigbee Home Automation) via SLZB-MR1U. Vi henter alle
states i ét kald (GET /api/states) og plukker målernes entiteter ud via
navnemønstrene i .env, fx sensor.elmaaler001_summation_delivered / sensor.elmaaler001_power.
Et mønster kan have flere bud adskilt med komma (det første bud, der findes, vinder).
"""
import logging
import random
import time
from datetime import datetime

import requests

from . import config, db
from .tid import nu

log = logging.getLogger("collector")

STATUS = {"ok": False, "fejl": "Ikke startet", "sidst_ok": None,
          "fundne_maalere": 0, "kilde": config.SOURCE, "ha_fejl_siden": None}

UTILGAENGELIG = ("unavailable", "unknown", "", None)


def _iso(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _tal(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


class HomeAssistant:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {config.HA_TOKEN}"
        self.s.verify = config.HA_VERIFY_TLS
        self.kort = {}   # entity_id -> (målernr, art, prioritet)
        for nr in range(1, config.METER_COUNT + 1):
            for moenstre, art in ((config.HA_ENERGY_ENTITY, "energi"), (config.HA_POWER_ENTITY, "effekt"),
                                  (config.HA_CURRENT_ENTITY, "strom"), (config.HA_VOLTAGE_ENTITY, "volt"),
                                  (config.HA_FREQUENCY_ENTITY, "hz"), (config.HA_LQI_ENTITY, "lqi")):
                for prio, m in enumerate(p.strip() for p in (moenstre or "").split(",") if p.strip()):
                    self.kort[m.format(nr=nr)] = (nr, art, prio)

    def hent(self):
        r = self.s.get(f"{config.HA_URL}/api/states", timeout=20)
        r.raise_for_status()
        alle = r.json()
        data = {}
        # bedste bud først, så et reserve-navn aldrig overskriver det foretrukne
        states = sorted((st for st in alle if st.get("entity_id") in self.kort),
                        key=lambda st: -self.kort[st["entity_id"]][2])
        for st in states:
            eid = st["entity_id"]
            nr, art, _ = self.kort[eid]
            d = data.setdefault(nr, {"energi": None, "effekt": None, "lqi": None, "strom": None,
                                     "volt": None, "hz": None, "tilg": False, "sidst_set": None})
            tilstand = st.get("state")
            enhed = (st.get("attributes") or {}).get("unit_of_measurement", "")
            sidst = max(filter(None, [_iso(st.get("last_updated")), _iso(st.get("last_reported"))]), default=None)
            if art == "energi":
                d["tilg"] = tilstand not in UTILGAENGELIG
                v = _tal(tilstand)
                if v is not None:
                    d["energi"] = v / 1000 if enhed == "Wh" else v * 1000 if enhed == "MWh" else v
                d["sidst_set"] = max(filter(None, [d["sidst_set"], sidst]), default=None)
            elif art == "effekt":
                v = _tal(tilstand)
                if v is not None:
                    d["effekt"] = v * 1000 if enhed == "kW" else v
                if tilstand not in UTILGAENGELIG:
                    d["sidst_set"] = max(filter(None, [d["sidst_set"], sidst]), default=None)
            elif art == "lqi":
                v = _tal(tilstand)
                d["lqi"] = int(v) if v is not None else None
            elif art in ("strom", "volt", "hz"):
                v = _tal(tilstand)
                if v is not None and enhed in ("mA", "mV"):
                    v /= 1000
                d[art] = v
        _diagnose(alle, self.kort, data)
        return data


def _ligner_elmaaler(tekst):
    t = tekst.lower().replace("å", "aa")
    return "elmaal" in t or "elmal" in t


def _diagnose(alle, kort, data):
    """Til System → Status: sensorer i HA, der ligner en elmåler, men ikke matcher navnemønstret,
    og målere hvor andre sensorer blev fundet, men ikke tællerstanden (summation_delivered)."""
    ukendte = []
    for st in alle:
        eid = st.get("entity_id") or ""
        navn = (st.get("attributes") or {}).get("friendly_name") or ""
        if eid.startswith("sensor.") and eid not in kort and (_ligner_elmaaler(eid) or _ligner_elmaaler(navn)):
            ukendte.append({"entity_id": eid, "navn": navn, "state": st.get("state")})
    STATUS["ikke_genkendt"] = sorted(ukendte, key=lambda u: u["entity_id"])[:200]
    STATUS["uden_taeller"] = sorted(nr for nr, d in data.items() if d["energi"] is None)


class Simulator:
    """Falske målere til test af webinterface uden Home Assistant."""

    def __init__(self):
        rnd = random.Random(42)
        self.m = {}
        for nr in range(1, config.METER_COUNT + 1):
            p = rnd.random()
            profil = ("tom" if p < .15 else "lille" if p < .45 else "affugter" if p < .8 else "varme")
            self.m[nr] = {"profil": profil, "energi": rnd.uniform(0, 400), "offline": False,
                          "lqi": rnd.randint(20, 220)}
        # fortsæt fra sidst gemte tællerstand, så en genstart ikke ligner en tæller-nulstilling
        with db.db() as c:
            for r in c.execute("SELECT a.maaler_id, a.energi_kwh FROM aflaesninger a JOIN "
                               "(SELECT maaler_id, MAX(ts) ts FROM aflaesninger GROUP BY maaler_id) s "
                               "ON s.maaler_id=a.maaler_id AND s.ts=a.ts"):
                if r[0] in self.m:
                    self.m[r[0]]["energi"] = r[1]
        self.t = time.time()

    @staticmethod
    def effekt(profil, t):
        r = random.random()
        if profil == "tom":
            return 0.0
        if profil == "lille":
            return 15.0 if r < .7 else 120.0
        if profil == "affugter":
            return 210.0 if r < .65 else 3.0
        return 1950.0 if r < .45 else 950.0 if r < .7 else 5.0

    def backfill(self, dage):
        with db.db() as c:
            if c.execute("SELECT 1 FROM aflaesninger LIMIT 1").fetchone():
                return
            step = config.STORE_MINUTES * 60
            slut = nu() // step * step
            start = slut - dage * 86400
            for nr, m in self.m.items():
                e = m["energi"]
                rows = [(nr, start, e, 0.0)]
                ts = start + step
                while ts <= slut:
                    d = self.effekt(m["profil"], ts) * step / 3600 / 1000
                    e += d
                    rows.append((nr, ts, round(e, 4), round(d, 4)))
                    ts += step
                c.executemany("INSERT OR IGNORE INTO aflaesninger VALUES (?,?,?,?)", rows)
                m["energi"] = e
        log.info("Simulator: %d dages historik oprettet", dage)

    def hent(self):
        t = time.time()
        dt, self.t = t - self.t, t
        data = {}
        for nr, m in self.m.items():
            if random.random() < 0.002:
                m["offline"] = not m["offline"]
            p = self.effekt(m["profil"], t)
            m["energi"] += p * dt / 3600 / 1000
            v = round(random.uniform(228, 236), 1)
            data[nr] = {"energi": round(m["energi"], 3), "effekt": p, "lqi": m["lqi"],
                        "strom": round(p / v, 2), "volt": v, "hz": round(random.uniform(49.95, 50.05), 2),
                        "tilg": not m["offline"], "sidst_set": int(t)}
        return data


def _gem_status(data):
    t = nu()
    with db.db() as c:
        tidligere = {r["maaler_id"]: r for r in c.execute("SELECT * FROM status").fetchall()}
        for nr in range(1, config.METER_COUNT + 1):
            d = data.get(nr)
            gl = tidligere.get(nr)
            if d and d["tilg"] and d["energi"] is not None:
                c.execute("UPDATE status SET energi_kwh=?, effekt_w=?, linkquality=COALESCE(?, linkquality), "
                          "strom_a=?, spaending_v=?, frekvens_hz=?, "
                          "tilgaengelig=1, sidst_set=COALESCE(?, ?), offline_siden=NULL, offline_varslet=0, "
                          "opdateret=? WHERE maaler_id=?",
                          (d["energi"], d["effekt"], d["lqi"], d.get("strom"), d.get("volt"), d.get("hz"),
                           d["sidst_set"], t, t, nr))
                if gl and gl["offline_varslet"]:
                    c.execute("INSERT INTO haendelser(ts,type,tekst) VALUES (?,?,?)",
                              (t, "måler", f"Måler {nr:03d} er tilbage online"))
            elif gl and gl["sidst_set"]:
                # måleren har været set før, men er nu utilgængelig eller forsvundet
                c.execute("UPDATE status SET tilgaengelig=0, effekt_w=NULL, strom_a=NULL, spaending_v=NULL, frekvens_hz=NULL, "
                          "offline_siden=COALESCE(offline_siden, ?), opdateret=? WHERE maaler_id=?",
                          (t, t, nr))


def lav_kilde():
    if config.SOURCE == "sim":
        s = Simulator()
        if config.SIM_BACKFILL_DAYS:
            s.backfill(config.SIM_BACKFILL_DAYS)
        return s
    return HomeAssistant()


def poll_en_gang(kilde):
    try:
        data = kilde.hent()
        _gem_status(data)
        STATUS.update(ok=True, fejl=None, sidst_ok=nu(),
                      fundne_maalere=sum(1 for d in data.values() if d.get("energi") is not None),
                      ha_fejl_siden=None)
    except Exception as e:  # noqa: BLE001 – vi vil aldrig have at opsamlingen dør
        if STATUS["ok"] or STATUS["ha_fejl_siden"] is None:
            db.log("fejl", f"Opsamling fra Home Assistant fejlede: {e}")
            STATUS["ha_fejl_siden"] = nu()
        STATUS.update(ok=False, fejl=str(e)[:300])
        log.warning("Opsamling fejlede: %s", e)
