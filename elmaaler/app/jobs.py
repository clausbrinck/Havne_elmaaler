"""Baggrundsjob: opsamling, aflæsninger hvert 15. min, varsler, opgørelser og backup."""
import logging
import time
from datetime import timedelta

from . import backup, bestyrelse, collector, config, db, forbrug, mailer, rapport, simulering
from .tid import nu, lokal, i_dag, maaned_str, forrige_maaned, maaned_navn, fmt_tid

log = logging.getLogger("jobs")

MAKS_KW = 3.84 * 1.25   # 16 A × 240 V + margen


# ---------------------------------------------------------------- hjælpere

def _engang(noegle):
    """True første gang en nøgle ses – bruges så job/varsler kun kører én gang."""
    with db.db() as c:
        if c.execute("SELECT 1 FROM varsler_sendt WHERE noegle=?", (noegle,)).fetchone():
            return False
        c.execute("INSERT INTO varsler_sendt VALUES (?,?)", (noegle, nu()))
        c.execute("DELETE FROM varsler_sendt WHERE ts < ?", (nu() - 400 * 86400,))
    return True


def _kontor_modtagere(felt):
    """Emails for havnekontor-brugere med valget slået til + ekstra modtagere."""
    with db.db() as c:
        ud = [r["email"] for r in c.execute(
            f"SELECT email FROM brugere WHERE rolle IN ('kontor','admin') AND aktiv=1 AND {felt}=1").fetchall()]
    if db.hent("ekstra_" + felt):
        ud += [e.strip() for e in (db.hent("ekstra_kontor_emails") or "").replace(";", ",").split(",") if e.strip()]
    return sorted(set(ud))


def _brugere_med(c, felt):
    """Aktive brugere med valget slået til, og som har mindst én aktiv tildeling."""
    return c.execute(f"SELECT DISTINCT b.* FROM brugere b JOIN tildelinger t ON t.bruger_id=b.id "
                     f"WHERE b.aktiv=1 AND b.{felt}=1 AND t.start_ts<=? AND (t.slut_ts IS NULL OR t.slut_ts>?)",
                     (nu(), nu())).fetchall()


def _klokken_passeret(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    t = lokal(nu())
    return (t.hour, t.minute) >= (h, m)


# ---------------------------------------------------------------- aflæsninger

def gem_aflaesninger(slot):
    gemt = 0
    with db.db() as c:
        for s in c.execute("SELECT * FROM status WHERE tilgaengelig=1 AND energi_kwh IS NOT NULL").fetchall():
            nr, e = s["maaler_id"], s["energi_kwh"]
            forr = c.execute("SELECT ts, energi_kwh FROM aflaesninger WHERE maaler_id=? ORDER BY ts DESC LIMIT 1",
                             (nr,)).fetchone()
            if forr and forr["ts"] >= slot:
                continue
            if not forr:
                d = 0.0
            else:
                timer = (slot - forr["ts"]) / 3600
                maks = MAKS_KW * timer + 0.05
                d = e - forr["energi_kwh"]
                if d < -0.01:
                    # tæller er nulstillet eller måler udskiftet
                    d = e if e <= maks else 0.0
                    c.execute("INSERT INTO haendelser(ts,type,tekst) VALUES (?,?,?)",
                              (nu(), "måler", f"Måler {nr:03d}: tæller faldt fra {forr['energi_kwh']:.2f} "
                                              f"til {e:.2f} kWh (nulstillet/udskiftet)"))
                elif d < 0:
                    d = 0.0
                elif d > maks:
                    c.execute("INSERT INTO haendelser(ts,type,tekst) VALUES (?,?,?)",
                              (nu(), "måler", f"Måler {nr:03d}: usandsynligt spring på {d:.2f} kWh på "
                                              f"{timer:.1f} t ignoreret (ny startværdi)"))
                    d = 0.0
            c.execute("INSERT OR IGNORE INTO aflaesninger VALUES (?,?,?,?)", (nr, slot, e, round(d, 4)))
            gemt += 1
    return gemt


# ---------------------------------------------------------------- varsler

def tjek_advarsler():
    """Advarsel når forbruget nærmer sig grænsen til næste forbrugsgruppe."""
    maaned = maaned_str(i_dag())
    if not forbrug.afregnes(maaned):
        return
    if simulering.aktiv():
        return   # ingen advarsler til medlemmer mens der er simuleret forbrug
    pct = float(db.hent("advarsel_procent") or 90)
    with db.db() as c:
        for t in forbrug.aktive_tildelinger(c):
            b = c.execute("SELECT * FROM brugere WHERE id=?", (t["bruger_id"],)).fetchone()
            if not (b["aktiv"] and b["mail_advarsel"]):
                continue
            st = forbrug.bruger_maaned_status(c, t, maaned)
            if st["graense"] is None or st["afrundet"] < st["graense"] * pct / 100:
                continue
            if not _engang(f"adv:{b['id']}:{t['maaler_id']}:{maaned}:{st['gruppe']}"):
                continue
            n = st["naeste"]
            tekst = (f"Hej {b['navn']},\n\n"
                     f"Dit forbrug på måler {t['maaler_id']:03d}"
                     + (f" ({t['placering']})" if t["placering"] else "")
                     + f" er nu {rapport.dk(st['kwh'])} kWh i {maaned_navn(maaned)}.\n\n"
                     f"Du er i forbrugsgruppe {st['label']} ({rapport.dk(st['pris'], 0)} kr), "
                     f"og nærmer dig grænsen på {st['graense']} kWh.\n"
                     f"Næste gruppe er {n['label']} til {rapport.dk(n['pris'], 0)} kr for måneden.\n\n"
                     f"Fortsætter forbruget som hidtil, forventes ca. {rapport.dk(st['prognose'], 0)} kWh "
                     f"for hele måneden ({st['prognose_kat']['label']}).\n")
            mailer.send(b["email"], f"Elforbrug: du nærmer dig næste forbrugsgruppe (måler {t['maaler_id']:03d})", tekst)
            db.log("mail", f"Advarsel om næste gruppe sendt til {b['email']} (måler {t['maaler_id']:03d})")


def tjek_offline():
    minutter = int(db.hent("offline_minutter") or 60)
    graense = nu() - minutter * 60
    kontor_liste, bruger_mails = [], []
    with db.db() as c:
        rows = c.execute("SELECT s.*, m.placering FROM status s JOIN maalere m ON m.id=s.maaler_id "
                         "WHERE m.aktiv=1 AND s.tilgaengelig=0 AND s.offline_siden IS NOT NULL "
                         "AND s.offline_siden<=? AND s.offline_varslet=0", (graense,)).fetchall()
        for s in rows:
            c.execute("UPDATE status SET offline_varslet=1 WHERE maaler_id=?", (s["maaler_id"],))
            tl = forbrug.aktive_tildelinger(c, maaler_id=s["maaler_id"])
            kontor_liste.append((s, tl[0]["navn"] if tl else "ikke tildelt"))
            for t in tl:
                b = c.execute("SELECT * FROM brugere WHERE id=?", (t["bruger_id"],)).fetchone()
                if b["aktiv"] and b["mail_offline"]:
                    bruger_mails.append((b, s))
    # mails sendes først når databasen er skrevet (så en mailfejl kan logges)
    for b, s in bruger_mails:
        mailer.send(b["email"], f"Elmåler {s['maaler_id']:03d} har mistet forbindelsen",
                    f"Hej {b['navn']},\n\nVi har ikke haft forbindelse til måler {s['maaler_id']:03d}"
                    + (f" ({s['placering']})" if s["placering"] else "")
                    + f" siden {fmt_tid(s['offline_siden'])}.\n\n"
                    "Det kan skyldes, at stikket er taget ud, eller at strømmen til standeren er "
                    "afbrudt. Forbruget tælles stadig i måleren og kommer med, når forbindelsen er "
                    "tilbage, så længe måleren har strøm.\n")
    if kontor_liste:
        db.log("måler", "Offline > %d min: %s" % (minutter, ", ".join(f"{s['maaler_id']:03d}" for s, _ in kontor_liste)))
        tekst = f"Følgende målere har været uden forbindelse i mere end {minutter} minutter:\n\n" + "\n".join(
            f"   Måler {s['maaler_id']:03d} {s['placering'] or '':8} offline siden {fmt_tid(s['offline_siden'])} – {hvem}"
            for s, hvem in kontor_liste)
        numre = [f"{s['maaler_id']:03d}" for s, _ in kontor_liste]
        emne = ("Elmåler " if len(numre) == 1 else "Elmålere ") + ", ".join(numre[:10]) \
            + (f" m.fl. ({len(numre)} i alt)" if len(numre) > 10 else "") + " uden forbindelse"
        mailer.send(_kontor_modtagere("mail_offline"), emne, tekst)

    # Ingen forbindelse til Home Assistant overhovedet
    fejl = collector.STATUS.get("ha_fejl_siden")
    if fejl and fejl <= graense and _engang(f"ha:{fejl}"):
        mailer.send(_kontor_modtagere("mail_offline"), "Elmålere: ingen forbindelse til Home Assistant",
                    f"Elmåler-serveren har ikke kunnet hente data fra Home Assistant siden {fmt_tid(fejl)}.\n\n"
                    f"Fejl: {collector.STATUS.get('fejl')}\n\nMålerne tæller stadig selv; forbruget kommer med, "
                    "når forbindelsen er genoprettet.")


# ---------------------------------------------------------------- planlagte opgørelser

def planlagte():
    dato = i_dag()
    if _klokken_passeret("00:20") and dato.day == 1:
        m = forrige_maaned(maaned_str(dato))
        if _engang(f"job:laas:{m}"):
            forbrug.laas_maaned(m)

    if _klokken_passeret(config.BACKUP_TIME) and _engang(f"job:backup:{dato}"):
        try:
            backup.lav_backup()
        except Exception as e:  # noqa: BLE001
            db.log("fejl", f"Backup fejlede: {e}")
            mailer.send(_kontor_modtagere("mail_offline"), "Elmålere: backup fejlede", str(e))

    if not _klokken_passeret(db.hent("mail_tidspunkt") or "06:00"):
        return

    # mails til medlemmer sættes på pause, så længe der findes simuleret forbrug (debug-siden)
    medlemmer = not simulering.aktiv()
    i_gaar = dato - timedelta(days=1)
    if _engang(f"job:dag:{dato}"):
        with db.db() as c:
            for b in (_brugere_med(c, "mail_dag") if medlemmer else []):
                tl = forbrug.aktive_tildelinger(c, bruger_id=b["id"])
                mailer.send(b["email"], f"Dagligt elforbrug {i_gaar:%d-%m-%Y}", rapport.bruger_dag(c, b, tl, i_gaar))
            modt = _kontor_modtagere("mail_dag")
            if modt:
                mailer.send(modt, f"Elmålere – døgnopgørelse {i_gaar:%d-%m-%Y}",
                            rapport.kontor_periode(c, i_gaar, dato, f"Døgnopgørelse {i_gaar:%d-%m-%Y}"),
                            signatur=False)

    if dato.weekday() == 0 and _engang(f"job:uge:{dato}"):
        mandag = dato - timedelta(days=7)
        with db.db() as c:
            for b in (_brugere_med(c, "mail_uge") if medlemmer else []):
                tl = forbrug.aktive_tildelinger(c, bruger_id=b["id"])
                mailer.send(b["email"], f"Ugentligt elforbrug uge {mandag.isocalendar()[1]}",
                            rapport.bruger_uge(c, b, tl, mandag))
            modt = _kontor_modtagere("mail_uge")
            if modt:
                mailer.send(modt, f"Elmålere – ugeopgørelse uge {mandag.isocalendar()[1]}",
                            rapport.kontor_periode(c, mandag, dato, f"Ugeopgørelse uge {mandag.isocalendar()[1]}"),
                            signatur=False)

    if dato.day == 1:
        m = forrige_maaned(maaned_str(dato))
        if _engang(f"job:maaned:{m}"):
            rows, _ = forbrug.maanedsopgoerelse(m)
            pr_bruger = {}
            for r in rows:
                if r["bruger_id"]:
                    pr_bruger.setdefault(r["bruger_id"], []).append(r)
            with db.db() as c:
                for bid, br in pr_bruger.items():
                    b = c.execute("SELECT * FROM brugere WHERE id=?", (bid,)).fetchone()
                    if medlemmer and b["aktiv"] and b["mail_maaned"]:
                        mailer.send(b["email"], f"Elforbrug {maaned_navn(m)}", rapport.bruger_maaned(b, br, m))
            modt = _kontor_modtagere("mail_maaned")
            if modt:
                mailer.send(modt, f"Elmålere – månedsopgørelse {maaned_navn(m)}", rapport.kontor_maaned(rows, m),
                            vedhaeft=[(f"elforbrug-{m}.csv", rapport.opgoerelse_csv(m), "text/csv")],
                            signatur=False)

        # PDF-rapport til bestyrelsen – kun for afregningsmåneder (nov–mar)
        if forbrug.afregnes(m) and bestyrelse.modtagere() and _engang(f"job:bestyrelse:{m}"):
            if simulering.aktiv():
                db.log("mail", f"Bestyrelsesrapport for {m} ikke sendt – der er simuleret forbrug i systemet")
            else:
                send_bestyrelsesrapport(m)


def send_bestyrelsesrapport(m):
    """Lav PDF-rapporten for måned m og send den til bestyrelsens mailgruppe. Returnerer True hvis sendt."""
    try:
        d = bestyrelse.data(m)
        data = bestyrelse.pdf(d)
    except Exception as e:  # noqa: BLE001
        log.exception("Bestyrelsesrapport fejlede")
        db.log("fejl", f"Bestyrelsesrapport for {m} kunne ikke laves: {e}")
        return False
    ok = mailer.send(bestyrelse.modtagere(), f"Elmålere – månedsrapport {maaned_navn(m)}", bestyrelse.mailtekst(d),
                     vedhaeft=[(f"maanedsrapport-{m}.pdf", data, "application/pdf"),
                               (f"elforbrug-{m}.csv", rapport.opgoerelse_csv(m), "text/csv")],
                     signatur=False)
    if ok:
        db.log("mail", f"Månedsrapport for {m} sendt til bestyrelsen ({', '.join(bestyrelse.modtagere())})")
    return ok


# ---------------------------------------------------------------- hovedløkke

def koer():
    kilde = collector.lav_kilde()
    step = config.STORE_MINUTES * 60
    sidste_poll = 0.0
    sidste_slot = nu() // step * step
    sidste_minut = 0
    while True:
        t = time.time()
        try:
            if t - sidste_poll >= config.POLL_SECONDS:
                sidste_poll = t
                collector.poll_en_gang(kilde)
            slot = int(t) // step * step
            if slot > sidste_slot:
                sidste_slot = slot
                n = gem_aflaesninger(slot)
                log.info("Aflæsning %s: %d målere gemt", fmt_tid(slot), n)
                tjek_advarsler()
            if int(t) // 60 != sidste_minut:
                sidste_minut = int(t) // 60
                tjek_offline()
                planlagte()
        except Exception:  # noqa: BLE001
            log.exception("Fejl i baggrundsjob")
        time.sleep(2)
