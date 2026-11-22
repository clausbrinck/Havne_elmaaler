"""Webinterface: bruger-login (Min side) og havnekontor-login."""
import csv
import io
import os
import re
import secrets
import threading
from datetime import timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import (backup, bestyrelse, collector, config, db, forbrug, jobs, mailer, perioder, rapport, sikkerhed,
               simulering, vedligehold)
from .tid import (nu, i_dag, maaned_str, maaned_grænser, maaned_navn, naeste_maaned, forrige_maaned,
                  parse_dato, dato_ts, fmt_tid, fmt_dato, siden, lokal)

app = Flask(__name__)
app.secret_key = config.secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Brugerlag: bruger (medlem) < kontor (havnekontoret) < admin (systemadministration).
# Admin har alle havnekontorets rettigheder + System-siderne.
ROLLER = {"bruger": "Bruger", "kontor": "Havnekontor", "admin": "Systemadministrator"}
KONTOR_ROLLER = ("kontor", "admin")

# Forbrugsrapporter på mail fra Min side: maks 5 pr. bruger pr. time
rapport_begraenser = sikkerhed.Begraenser(maks=5, vindue=3600)


def er_kontor(b):
    return bool(b and b["rolle"] in KONTOR_ROLLER)


def er_admin(b):
    return bool(b and b["rolle"] == "admin")


# ---------------------------------------------------------------- filtre og fælles

app.jinja_env.filters.update(dk=rapport.dk, tid=fmt_tid, dato=fmt_dato, siden=siden, maaned_navn=maaned_navn)


@app.context_processor
def faelles():
    b = g.get("bruger")
    sim = bool(er_kontor(b) and simulering.aktiv())
    return {"klub": config.KLUB_NAVN, "bruger": b, "csrf": csrf_token, "roller": ROLLER,
            "er_kontor": er_kontor(b), "er_admin": er_admin(b),
            "maaned_navn": maaned_navn, "nu": nu(), "sim_aktiv": sim}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def foer():
    g.bruger = None
    uid = session.get("uid")
    if uid:
        with db.db() as c:
            b = c.execute("SELECT * FROM brugere WHERE id=? AND aktiv=1", (uid,)).fetchone()
        if b and session.get("pwv") == (b["pw_hash"] or "")[-12:]:
            g.bruger = b
        else:
            session.clear()
    if request.method == "POST":
        if not session.get("csrf") or request.form.get("csrf") != session.get("csrf"):
            abort(400, "Formularen er udløbet – genindlæs siden og prøv igen.")


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def login_kraevet(f):
    @wraps(f)
    def w(*a, **kw):
        if not g.bruger:
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return w


def kontor_kraevet(f):
    @wraps(f)
    def w(*a, **kw):
        if not g.bruger:
            return redirect(url_for("login", next=request.path))
        if not er_kontor(g.bruger):
            abort(403)
        return f(*a, **kw)
    return w


def admin_kraevet(f):
    @wraps(f)
    def w(*a, **kw):
        if not g.bruger:
            return redirect(url_for("login", next=request.path))
        if not er_admin(g.bruger):
            abort(403)
        return f(*a, **kw)
    return w


def _rolle_fra_form():
    """Rollen fra en formular. Kun admin kan give admin-rollen."""
    r = request.form.get("rolle", "bruger")
    tilladt = ("bruger", "kontor", "admin") if er_admin(g.bruger) else ("bruger", "kontor")
    return r if r in tilladt else "bruger"


def _log_ind(b):
    session.clear()
    session.permanent = True
    session["uid"] = b["id"]
    session["pwv"] = (b["pw_hash"] or "")[-12:]
    with db.db() as c:
        c.execute("UPDATE brugere SET sidste_login=? WHERE id=?", (nu(), b["id"]))


def _send_link(b, formaal):
    timer = 24 * 14 if formaal == "invitation" else 24
    t = sikkerhed.ny_token(b["id"], formaal, timer)
    link = f"{config.BASE_URL}{url_for('nulstil', token=t)}"
    if formaal == "invitation":
        tekst = (f"Hej {b['navn']},\n\nDu er oprettet som bruger af {config.KLUB_NAVN}s elmålere. "
                 f"Her kan du følge dit strømforbrug og vælge, hvilke emails du vil have.\n\n"
                 f"Vælg din adgangskode her (linket virker i 14 dage):\n{link}\n\n"
                 f"Dit brugernavn er din email: {b['email']}\n")
        emne = f"Velkommen til {config.KLUB_NAVN} elmålere"
    else:
        tekst = (f"Hej {b['navn']},\n\nDer er bedt om at nulstille din adgangskode. "
                 f"Vælg en ny her (linket virker i 24 timer):\n{link}\n\n"
                 f"Har du ikke bedt om det, kan du se bort fra denne mail.\n")
        emne = "Nulstil adgangskode – elmålere"
    return mailer.send(b["email"], emne, tekst, signatur=False), link


# ---------------------------------------------------------------- login

@app.route("/")
def forside():
    if not g.bruger:
        return redirect(url_for("login"))
    return redirect(url_for("kontor" if er_kontor(g.bruger) else "min_side"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        kode = request.form.get("kode", "")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if sikkerhed.login_begraenser.blokeret(ip) or sikkerhed.login_begraenser.blokeret(email):
            flash("For mange forkerte forsøg. Vent 15 minutter og prøv igen.", "fejl")
            return render_template("login.html"), 429
        with db.db() as c:
            b = c.execute("SELECT * FROM brugere WHERE email=? AND aktiv=1", (email,)).fetchone()
        if b and sikkerhed.tjek_kode(kode, b["pw_hash"]):
            sikkerhed.login_begraenser.nulstil(ip)
            sikkerhed.login_begraenser.nulstil(email)
            _log_ind(b)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("forside"))
        sikkerhed.login_begraenser.fejl(ip)
        sikkerhed.login_begraenser.fejl(email)
        flash("Forkert email eller adgangskode.", "fejl")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/glemt", methods=["GET", "POST"])
def glemt():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with db.db() as c:
            b = c.execute("SELECT * FROM brugere WHERE email=? AND aktiv=1", (email,)).fetchone()
        if b and not sikkerhed.login_begraenser.blokeret("glemt:" + email):
            sikkerhed.login_begraenser.fejl("glemt:" + email)
            _send_link(b, "nulstil")
        flash("Hvis emailen findes, er der sendt et link til at vælge ny adgangskode.", "ok")
        return redirect(url_for("login"))
    return render_template("glemt.html")


@app.route("/nulstil/<token>", methods=["GET", "POST"])
def nulstil(token):
    t = sikkerhed.brug_token(token)
    if not t:
        flash("Linket er udløbet eller allerede brugt. Bed om et nyt.", "fejl")
        return redirect(url_for("glemt"))
    if request.method == "POST":
        k1, k2 = request.form.get("kode", ""), request.form.get("kode2", "")
        fejl = sikkerhed.kode_ok(k1) or (k1 != k2 and "De to adgangskoder er ikke ens.")
        if fejl:
            flash(fejl, "fejl")
        else:
            sikkerhed.brug_token(token, slet=True)
            with db.db() as c:
                c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (sikkerhed.hash_kode(k1), t["bruger_id"]))
                b = c.execute("SELECT * FROM brugere WHERE id=?", (t["bruger_id"],)).fetchone()
            _log_ind(b)
            flash("Din adgangskode er gemt.", "ok")
            return redirect(url_for("forside"))
    return render_template("nulstil.html", invitation=t["formaal"] == "invitation")


# ---------------------------------------------------------------- bruger: Min side

def _maaler_kort(c, t, maaned):
    st = c.execute("SELECT * FROM status WHERE maaler_id=?", (t["maaler_id"],)).fetchone()
    ms = forbrug.bruger_maaned_status(c, t, maaned)
    midnat = max(dato_ts(i_dag()), t["start_ts"])          # i dag = fra kl. 00:00
    return {
        "t": t, "st": st, "m": ms,
        "total_siden_start": forbrug.forbrug(c, t["maaler_id"], t["start_ts"], nu() + 1),
        "i_dag": forbrug.forbrug(c, t["maaler_id"], midnat, dato_ts(i_dag() + timedelta(days=1))),
    }


# ---------------------------------------------------------------- hjælp (manualer som PDF)

HJAELP_MAPPE = os.path.join(os.path.dirname(__file__), "hjaelp")
# fil, titel, beskrivelse, hvem der må se den: "alle" (også uden login), "kontor" eller "admin"
HJAELP = [
    ("Quickguide-medlem.pdf", "Quickguide for medlemmer", "De vigtigste ting på 2 sider – god at printe.", "alle"),
    ("Manual-medlem.pdf", "Manual for medlemmer", "Log ind, Min side, grafer, e-mails og forbrugsrapport.", "alle"),
    ("Quickguide-havnekontor.pdf", "Quickguide for havnekontoret", "De daglige opgaver på 2 sider.", "kontor"),
    ("Manual-havnekontor.pdf", "Manual for havnekontoret",
     "Tildel og fjern målere, brugere, import, opgørelse og afregning.", "kontor"),
    ("Manual-administration.pdf", "Manual for administration",
     "Menuen System, backup, indstillinger og drift.", "admin"),
]


def _hjaelp_til(b):
    """De hjælpedokumenter, brugeren b må se (b=None: ikke logget ind)."""
    def maa(hvem):
        return hvem == "alle" or (hvem == "kontor" and er_kontor(b)) or (hvem == "admin" and er_admin(b))
    return [h for h in HJAELP if maa(h[3]) and os.path.exists(os.path.join(HJAELP_MAPPE, h[0]))]


@app.route("/hjaelp")
@login_kraevet
def hjaelp():
    return render_template("hjaelp.html", dokumenter=_hjaelp_til(g.bruger))


@app.route("/hjaelp/<navn>")
def hjaelp_fil(navn):
    """Medlemmernes manual og quickguide kan åbnes uden login (fx fra login-siden)."""
    if navn not in {h[0] for h in _hjaelp_til(g.bruger)}:
        if not g.bruger:
            return redirect(url_for("login", next=request.path))
        abort(403)
    return send_from_directory(HJAELP_MAPPE, navn, mimetype="application/pdf", as_attachment=False)


@app.route("/min-side")
@login_kraevet
def min_side():
    maaned = maaned_str(i_dag())
    with db.db() as c:
        tild = forbrug.aktive_tildelinger(c, bruger_id=g.bruger["id"])
        kort = [_maaler_kort(c, t, maaned) for t in tild]
    return render_template("min_side.html", kort=kort, maaned=maaned,
                           historik=forbrug.bruger_historik(g.bruger["id"]),
                           kategorier=forbrug.kategorier(), afregnes=forbrug.afregnes(maaned),
                           i_dag=i_dag().isoformat(), maaned_start=i_dag().replace(day=1).isoformat(),
                           smtp=mailer.konfigureret(), enkel=g.bruger["visning"] != "avanceret")


@app.route("/min-side/visning", methods=["POST"])
@login_kraevet
def min_visning():
    """Skift mellem enkel og avanceret visning af Min side (gemmes på brugeren)."""
    v = "avanceret" if request.form.get("visning") == "avanceret" else "enkel"
    with db.db() as c:
        c.execute("UPDATE brugere SET visning=? WHERE id=?", (v, g.bruger["id"]))
    return redirect(url_for("min_side"))


@app.route("/min-side/emails", methods=["POST"])
@login_kraevet
def mine_emails():
    felter = ["mail_advarsel", "mail_dag", "mail_uge", "mail_maaned", "mail_offline"]
    with db.db() as c:
        c.execute(f"UPDATE brugere SET {', '.join(f + '=?' for f in felter)} WHERE id=?",
                  (*[1 if request.form.get(f) else 0 for f in felter], g.bruger["id"]))
    if request.headers.get("X-Autogem"):          # gemt automatisk fra Min side (app.js)
        return jsonify(ok=True)
    flash("Dine e-mail-valg er gemt.", "ok")
    return redirect(url_for("min_side"))


def _rapport_periode(valg, fra_txt, til_txt):
    """Periode (fra, til – begge inkl.) til forbrugsrapporten ud fra valget på Min side."""
    idag = i_dag()
    if valg == "denne":
        return idag.replace(day=1), idag
    if valg == "forrige":
        slut = idag.replace(day=1) - timedelta(days=1)
        return slut.replace(day=1), slut
    if valg == "vinter":
        aar = idag.year if idag.month >= 11 else idag.year - 1
        start = idag.replace(year=aar, month=11, day=1)
        return start, min(idag, start.replace(year=aar + 1, month=3, day=31))
    fra, til = parse_dato(fra_txt), parse_dato(til_txt)
    if til < fra:
        raise ValueError("Til-datoen skal være samme dag som eller efter fra-datoen.")
    if til > idag:
        til = idag
    if (til - fra).days > 400:
        raise ValueError("Perioden må højst være 400 dage.")
    return fra, til


@app.route("/min-side/rapport", methods=["POST"])
@login_kraevet
def min_rapport():
    """Send en forbrugsrapport for en valgt periode til brugerens egen email."""
    b = g.bruger
    try:
        fra, til = _rapport_periode(request.form.get("periode", "denne"),
                                    request.form.get("fra", ""), request.form.get("til", ""))
    except (ValueError, KeyError) as e:
        flash(str(e) or "Ugyldig periode.", "fejl")
        return redirect(url_for("min_side"))
    if not mailer.konfigureret():
        flash("Der er ikke sat en mailserver op – kontakt havnekontoret.", "fejl")
        return redirect(url_for("min_side"))
    if rapport_begraenser.blokeret(b["id"]):
        flash("Du har bestilt mange rapporter den seneste time. Prøv igen lidt senere.", "fejl")
        return redirect(url_for("min_side"))
    rapport_begraenser.fejl(b["id"])
    with db.db() as c:
        tekst, data = rapport.bruger_periode(c, b, fra, til)
    if data is None:
        flash("Du har ingen måler i den valgte periode.", "fejl")
        return redirect(url_for("min_side"))
    ok = mailer.send(b["email"], f"Forbrugsrapport {fmt_dato(fra)} – {fmt_dato(til)}", tekst,
                     vedhaeft=[(f"elforbrug-{fra}-{til}.csv", data, "text/csv")])
    if ok:
        db.log("mail", f"Forbrugsrapport {fra} – {til} sendt til {b['email']} (bestilt på Min side)")
        flash(f"Rapporten for {fmt_dato(fra)} – {fmt_dato(til)} er sendt til {b['email']}.", "ok")
    else:
        flash("Rapporten kunne ikke sendes. Prøv igen senere, eller kontakt havnekontoret.", "fejl")
    return redirect(url_for("min_side"))


@app.route("/min-side/konto", methods=["GET", "POST"])
@login_kraevet
def min_konto():
    if request.method == "POST" and request.form.get("handling") == "kontor_mails" and er_kontor(g.bruger):
        felter = ["mail_offline", "mail_dag", "mail_uge", "mail_maaned"]
        with db.db() as c:
            c.execute(f"UPDATE brugere SET {', '.join(f + '=?' for f in felter)} WHERE id=?",
                      (*[1 if request.form.get(f) else 0 for f in felter], g.bruger["id"]))
        flash("Dine havnekontor-mails er gemt.", "ok")
        return redirect(url_for("min_konto"))
    if request.method == "POST":
        b = g.bruger
        if not sikkerhed.tjek_kode(request.form.get("nuvaerende", ""), b["pw_hash"]):
            flash("Din nuværende adgangskode er forkert.", "fejl")
            return redirect(url_for("min_konto"))
        handling = request.form.get("handling")
        if handling == "email":
            ny = request.form.get("email", "").strip().lower()
            if not EMAIL_RE.match(ny):
                flash("Ugyldig email-adresse.", "fejl")
            else:
                try:
                    with db.db() as c:
                        c.execute("UPDATE brugere SET email=? WHERE id=?", (ny, b["id"]))
                except Exception:  # noqa: BLE001
                    flash("Den email bruges allerede af en anden bruger.", "fejl")
                else:
                    # mail til den gamle adresse – sendes efter commit
                    mailer.send(b["email"], "Din email er ændret",
                                f"Din email til elmålerne er ændret fra {b['email']} til {ny}.\n"
                                "Har du ikke selv gjort det, så kontakt havnekontoret.")
                    flash("Din email er ændret.", "ok")
        elif handling == "kode":
            k1, k2 = request.form.get("kode", ""), request.form.get("kode2", "")
            fejl = sikkerhed.kode_ok(k1) or (k1 != k2 and "De to adgangskoder er ikke ens.")
            if fejl:
                flash(fejl, "fejl")
            else:
                h = sikkerhed.hash_kode(k1)
                with db.db() as c:
                    c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (h, b["id"]))
                session["pwv"] = h[-12:]
                flash("Din adgangskode er ændret.", "ok")
        return redirect(url_for("min_konto"))
    return render_template("konto.html")


@app.route("/api/forbrug")
@login_kraevet
def api_forbrug():
    """Data til periode-grafen (døgn/uge/måned/vinter).
    maaler=<nr> eller 'alle' (kun havnekontor). egen=1: kun brugerens egne tildelingsperioder."""
    visning = request.args.get("visning", "maaned")
    if visning not in perioder.VISNINGER:
        abort(400)
    try:
        d = parse_dato(request.args.get("dato", ""))
    except ValueError:
        d = i_dag()
    egen = request.args.get("egen") == "1" or not er_kontor(g.bruger)
    maaler = request.args.get("maaler", "")
    with db.db() as c:
        tilladt = None
        if maaler == "alle":
            if egen:
                abort(403)
            mid = None
        else:
            try:
                mid = int(maaler)
            except ValueError:
                abort(400)
            if egen:   # medlemmer ser kun forbrug fra de perioder, hvor måleren var deres
                tilladt = [(r["start_ts"], r["slut_ts"]) for r in c.execute(
                    "SELECT start_ts, slut_ts FROM tildelinger WHERE bruger_id=? AND maaler_id=?",
                    (g.bruger["id"], mid))]
                if not tilladt:
                    abort(403)
        return jsonify(perioder.serie(c, mid, visning, d, tilladt))


@app.route("/api/live")
@login_kraevet
def api_live():
    with db.db() as c:
        if er_kontor(g.bruger) and request.args.get("alle"):
            rows = c.execute("SELECT s.* FROM status s JOIN maalere m ON m.id=s.maaler_id WHERE m.aktiv=1").fetchall()
        else:
            ids = [t["maaler_id"] for t in forbrug.aktive_tildelinger(c, bruger_id=g.bruger["id"])]
            rows = [c.execute("SELECT * FROM status WHERE maaler_id=?", (i,)).fetchone() for i in ids]
    data = {r["maaler_id"]: {"effekt": r["effekt_w"], "online": bool(r["tilgaengelig"]),
                             "energi": r["energi_kwh"], "sidst": siden(r["sidst_set"])} for r in rows}
    return jsonify(maalere=data, total_kw=round(sum((r["effekt_w"] or 0) for r in rows) / 1000, 2),
                   online=sum(1 for r in rows if r["tilgaengelig"]))


# ---------------------------------------------------------------- havnekontor

def _maaler_tabel(c, maaned):
    fra, til, *_ = maaned_grænser(maaned)
    aktive = {t["maaler_id"]: t for t in forbrug.aktive_tildelinger(c)}
    ud = []
    for m in c.execute("SELECT m.*, s.* FROM maalere m JOIN status s ON s.maaler_id=m.id ORDER BY m.id").fetchall():
        t = aktive.get(m["id"])
        kwh = forbrug.forbrug(c, m["id"], max(fra, t["start_ts"]) if t else fra, til)
        if m["tilgaengelig"]:
            tilstand = "online"
        elif m["sidst_set"]:
            tilstand = "offline"
        else:
            tilstand = "ukendt"
        ud.append({"m": m, "t": t, "kwh": kwh, "kat": forbrug.kategoriser(kwh, maaned), "tilstand": tilstand})
    return ud


@app.route("/kontor")
@kontor_kraevet
def kontor():
    maaned = maaned_str(i_dag())
    with db.db() as c:
        tabel = _maaler_tabel(c, maaned)
        d0 = maaned_grænser(maaned)[2]
        dag = forbrug.dagsserie(c, None, d0, i_dag() + timedelta(days=1))
    aktive = [r for r in tabel if r["m"]["aktiv"]]
    tal = {
        "effekt_kw": sum((r["m"]["effekt_w"] or 0) for r in aktive) / 1000,
        "online": sum(r["tilstand"] == "online" for r in aktive),
        "offline": sum(r["tilstand"] == "offline" for r in aktive),
        "ukendt": sum(r["tilstand"] == "ukendt" for r in aktive),
        "tildelt": sum(1 for r in tabel if r["t"]),
        "i_dag": dag.get(i_dag(), 0.0),
        "maaned": sum(dag.values()),
    }
    return render_template("kontor.html", tabel=tabel, tal=tal, maaned=maaned, ha=collector.STATUS)


def _find_eller_opret_bruger(c, email, navn=""):
    email = email.strip().lower()
    b = c.execute("SELECT * FROM brugere WHERE email=?", (email,)).fetchone()
    if b:
        return b, False
    if not EMAIL_RE.match(email):
        raise ValueError(f"Ugyldig email: {email}")
    c.execute("INSERT INTO brugere(email, navn, oprettet) VALUES (?,?,?)", (email, navn.strip(), nu()))
    return c.execute("SELECT * FROM brugere WHERE email=?", (email,)).fetchone(), True


def _tildel(c, maaler_id, bruger_id, start, baad=""):
    """Tildel måler fra startdato. En aktiv tildeling afsluttes automatisk pr. samme dato."""
    s_ts = dato_ts(start)
    seneste = c.execute("SELECT * FROM tildelinger WHERE maaler_id=? ORDER BY start_ts DESC LIMIT 1",
                        (maaler_id,)).fetchone()
    if seneste:
        if s_ts <= seneste["start_ts"]:
            raise ValueError(f"Startdato skal være efter seneste tildelings start ({fmt_dato(seneste['start_dato'])}).")
        if seneste["slut_ts"] is None or seneste["slut_ts"] > s_ts:
            c.execute("UPDATE tildelinger SET slut_dato=?, slut_ts=? WHERE id=?",
                      (start.isoformat(), s_ts, seneste["id"]))
    c.execute("INSERT INTO tildelinger(maaler_id, bruger_id, start_dato, start_ts, oprettet, baad) VALUES (?,?,?,?,?,?)",
              (maaler_id, bruger_id, start.isoformat(), s_ts, nu(), (baad or "").strip()))


def _fjern_tildeling(c, tildeling_id, slut, maaler_id=None, bruger_id=None):
    """Fjern måler fra bruger pr. dato (tildelingen afsluttes – historik og opgørelser bevares)."""
    t = c.execute("SELECT t.*, b.navn, b.email FROM tildelinger t JOIN brugere b ON b.id=t.bruger_id WHERE t.id=?",
                  (tildeling_id,)).fetchone()
    if not t or (maaler_id and t["maaler_id"] != maaler_id) or (bruger_id and t["bruger_id"] != bruger_id):
        raise ValueError("Tildelingen findes ikke.")
    if t["slut_ts"] is not None and t["slut_ts"] <= nu():
        raise ValueError(f"Måler {t['maaler_id']:03d} er allerede fjernet fra brugeren.")
    if dato_ts(slut) <= t["start_ts"]:
        raise ValueError(f"Datoen skal være efter tildelingens start ({fmt_dato(t['start_dato'])}).")
    c.execute("UPDATE tildelinger SET slut_dato=?, slut_ts=? WHERE id=?", (slut.isoformat(), dato_ts(slut), t["id"]))
    db.log("tildeling", f"Måler {t['maaler_id']:03d} fjernet fra {t['email']}"
                        + (f" ({t['baad']})" if t["baad"] else "") + f" pr. {slut} (af {g.bruger['email']})", c)
    return t


@app.route("/kontor/maaler/<int:nr>", methods=["GET", "POST"])
@kontor_kraevet
def kontor_maaler(nr):
    with db.db() as c:
        m = c.execute("SELECT m.*, s.* FROM maalere m JOIN status s ON s.maaler_id=m.id WHERE m.id=?", (nr,)).fetchone()
    if not m:
        abort(404)
    if request.method == "POST":
        h = request.form.get("handling")
        try:
            if h in ("gem", "slet_tildeling") and not er_admin(g.bruger):
                abort(403)   # måleropsætning og sletning er systemadministration
            with db.db() as c:
                if h == "gem":
                    c.execute("UPDATE maalere SET placering=?, aktiv=?, note=? WHERE id=?",
                              (request.form.get("placering", "").strip().upper(), 1 if request.form.get("aktiv") else 0,
                               request.form.get("note", "").strip(), nr))
                    flash("Måler gemt.", "ok")
                elif h == "tildel":
                    bid = request.form.get("bruger_id")
                    if bid:
                        b = c.execute("SELECT * FROM brugere WHERE id=?", (int(bid),)).fetchone()
                    else:
                        b, ny = _find_eller_opret_bruger(c, request.form.get("email", ""), request.form.get("navn", ""))
                    _tildel(c, nr, b["id"], parse_dato(request.form["start_dato"]), request.form.get("baad", ""))
                    db.log("tildeling", f"Måler {nr:03d} tildelt {b['email']} fra {request.form['start_dato']} "
                                        f"(af {g.bruger['email']})", c)
                    flash(f"Måler {nr:03d} er tildelt {b['navn'] or b['email']}.", "ok")
                elif h == "afslut":
                    slut = parse_dato(request.form["slut_dato"])
                    t = _fjern_tildeling(c, int(request.form["tildeling_id"]), slut, maaler_id=nr)
                    flash(f"Måler {nr:03d} er fjernet fra {t['navn'] or t['email']} pr. {fmt_dato(slut)}.", "ok")
                elif h == "ret_baad":
                    c.execute("UPDATE tildelinger SET baad=? WHERE id=? AND maaler_id=?",
                              (request.form.get("baad", "").strip(), int(request.form["tildeling_id"]), nr))
                    flash("Båd gemt.", "ok")
                elif h == "slet_tildeling":
                    c.execute("DELETE FROM tildelinger WHERE id=? AND maaler_id=?", (int(request.form["tildeling_id"]), nr))
                    db.log("tildeling", f"Tildeling på måler {nr:03d} slettet (af {g.bruger['email']})", c)
                    flash("Tildelingen er slettet.", "ok")
        except (ValueError, KeyError) as e:
            flash(str(e) or "Ugyldige data.", "fejl")
        return redirect(url_for("kontor_maaler", nr=nr))

    maaned = maaned_str(i_dag())
    with db.db() as c:
        hist = c.execute("SELECT t.*, b.navn, b.email FROM tildelinger t JOIN brugere b ON b.id=t.bruger_id "
                         "WHERE t.maaler_id=? ORDER BY t.start_ts DESC", (nr,)).fetchall()
        brugere = c.execute("SELECT id, navn, email FROM brugere WHERE aktiv=1 ORDER BY navn, email").fetchall()
        aflaes = c.execute("SELECT * FROM aflaesninger WHERE maaler_id=? ORDER BY ts DESC LIMIT 12", (nr,)).fetchall()
        fra, til, *_ = maaned_grænser(maaned)
        kwh = forbrug.forbrug(c, nr, fra, til)
    return render_template("kontor_maaler.html", m=m, hist=hist, brugere=brugere, aflaes=aflaes,
                           kwh=kwh,
                           kat=forbrug.kategoriser(kwh, maaned), i_dag=i_dag().isoformat(), maaned=maaned)


@app.route("/kontor/brugere", methods=["GET", "POST"])
@kontor_kraevet
def kontor_brugere():
    if request.method == "POST":
        try:
            with db.db() as c:
                email = request.form.get("email", "").strip().lower()
                if c.execute("SELECT 1 FROM brugere WHERE email=?", (email,)).fetchone():
                    raise ValueError("Der findes allerede en bruger med den email.")
                b, _ = _find_eller_opret_bruger(c, email, request.form.get("navn", ""))
                rolle = _rolle_fra_form()
                c.execute("UPDATE brugere SET telefon=?, rolle=? WHERE id=?",
                          (request.form.get("telefon", "").strip(), rolle, b["id"]))
                kode = request.form.get("kode", "")
                if kode:
                    if sikkerhed.kode_ok(kode):
                        raise ValueError(sikkerhed.kode_ok(kode))
                    c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (sikkerhed.hash_kode(kode), b["id"]))
                maaler = request.form.get("maaler")
                if maaler:
                    _tildel(c, int(maaler), b["id"], parse_dato(request.form.get("start_dato") or i_dag().isoformat()),
                            request.form.get("baad", ""))
                b = c.execute("SELECT * FROM brugere WHERE id=?", (b["id"],)).fetchone()
            db.log("bruger", f"Bruger {email} oprettet (af {g.bruger['email']})")
            if request.form.get("inviter"):
                sendt, link = _send_link(b, "invitation")
                flash(f"Bruger oprettet og invitation sendt til {email}." if sendt else
                      f"Bruger oprettet, men mail kunne ikke sendes. Giv brugeren dette link: {link}",
                      "ok" if sendt else "fejl")
            else:
                flash(f"Bruger {email} oprettet.", "ok")
            return redirect(url_for("kontor_bruger", bid=b["id"]))
        except ValueError as e:
            flash(str(e), "fejl")
        return redirect(url_for("kontor_brugere"))
    with db.db() as c:
        brugere = c.execute(
            "SELECT b.*, (SELECT GROUP_CONCAT(printf('%03d', t.maaler_id), ', ') FROM tildelinger t "
            "  WHERE t.bruger_id=b.id AND t.start_ts<=?1 AND (t.slut_ts IS NULL OR t.slut_ts>?1)) maalere, "
            "(SELECT GROUP_CONCAT(NULLIF(t.baad, ''), ', ') FROM tildelinger t "
            "  WHERE t.bruger_id=b.id AND t.start_ts<=?1 AND (t.slut_ts IS NULL OR t.slut_ts>?1)) baade "
            "FROM brugere b ORDER BY CASE b.rolle WHEN 'admin' THEN 0 WHEN 'kontor' THEN 1 ELSE 2 END, "
            "b.navn COLLATE NOCASE, b.email", (nu(),)).fetchall()
    return render_template("kontor_brugere.html", brugere=brugere, i_dag=i_dag().isoformat())


@app.route("/kontor/bruger/<int:bid>", methods=["GET", "POST"])
@kontor_kraevet
def kontor_bruger(bid):
    with db.db() as c:
        b = c.execute("SELECT * FROM brugere WHERE id=?", (bid,)).fetchone()
    if not b:
        abort(404)
    if request.method == "POST":
        h = request.form.get("handling")
        if er_admin(b) and not er_admin(g.bruger) and h not in ("tildel", "afslut", "ret_baad"):
            abort(403)   # havnekontoret kan ikke ændre en systemadministrators konto
        try:
            with db.db() as c:
                if h == "gem":
                    email = request.form.get("email", "").strip().lower()
                    if not EMAIL_RE.match(email):
                        raise ValueError("Ugyldig email.")
                    rolle = _rolle_fra_form()
                    aktiv = 1 if request.form.get("aktiv") else 0
                    if bid == g.bruger["id"] and (rolle != g.bruger["rolle"] or not aktiv):
                        raise ValueError("Du kan ikke ændre din egen rolle eller gøre dig selv inaktiv.")
                    c.execute("UPDATE brugere SET email=?, navn=?, telefon=?, rolle=?, aktiv=? WHERE id=?",
                              (email, request.form.get("navn", "").strip(),
                               request.form.get("telefon", "").strip(), rolle, aktiv, bid))
                    flash("Bruger gemt.", "ok")
                elif h == "kode":
                    kode = request.form.get("kode", "")
                    if sikkerhed.kode_ok(kode):
                        raise ValueError(sikkerhed.kode_ok(kode))
                    c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (sikkerhed.hash_kode(kode), bid))
                    flash("Ny adgangskode sat.", "ok")
                elif h == "tildel":
                    _tildel(c, int(request.form["maaler"]), bid, parse_dato(request.form["start_dato"]),
                            request.form.get("baad", ""))
                    flash("Måler tildelt.", "ok")
                elif h == "afslut":
                    slut = parse_dato(request.form["slut_dato"])
                    t = _fjern_tildeling(c, int(request.form["tildeling_id"]), slut, bruger_id=bid)
                    flash(f"Måler {t['maaler_id']:03d} er fjernet fra brugeren pr. {fmt_dato(slut)}.", "ok")
                elif h == "ret_baad":
                    c.execute("UPDATE tildelinger SET baad=? WHERE id=? AND bruger_id=?",
                              (request.form.get("baad", "").strip(), int(request.form["tildeling_id"]), bid))
                    flash("Båd gemt.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(str(e) if isinstance(e, ValueError) else "Kunne ikke gemme (email bruges måske allerede).", "fejl")
        if h in ("invitation", "nulstil"):
            sendt, link = _send_link(b, h)
            flash(f"Mail sendt til {b['email']}." if sendt else f"Mail kunne ikke sendes. Link: {link}",
                  "ok" if sendt else "fejl")
        return redirect(url_for("kontor_bruger", bid=bid))
    maaned = maaned_str(i_dag())
    with db.db() as c:
        tild = c.execute("SELECT t.*, m.placering FROM tildelinger t JOIN maalere m ON m.id=t.maaler_id "
                         "WHERE t.bruger_id=? ORDER BY t.start_ts DESC", (bid,)).fetchall()
        aktive = forbrug.aktive_tildelinger(c, bruger_id=bid)
        status = {t["maaler_id"]: forbrug.bruger_maaned_status(c, t, maaned) for t in aktive}
    return render_template("kontor_bruger.html", b=b, tild=tild, status=status, maaned=maaned,
                           historik=forbrug.bruger_historik(bid), i_dag=i_dag().isoformat())


@app.route("/kontor/ubrugte", methods=["GET", "POST"])
@kontor_kraevet
def kontor_ubrugte():
    """Tildelte målere uden forbrug i en periode – kan fjernes fra brugerne samlet."""
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("tildeling_id")]
        if not ids:
            flash("Sæt flueben ved de målere, der skal fjernes.", "fejl")
        else:
            try:
                slut = parse_dato(request.form["slut_dato"])
                with db.db() as c:   # alt eller intet: fejler én, fjernes ingen
                    for tid_ in ids:
                        _fjern_tildeling(c, tid_, slut)
                flash(f"{len(ids)} måler(e) er fjernet fra brugerne pr. {fmt_dato(slut)}. "
                      "Historik og opgørelser er bevaret.", "ok")
            except (ValueError, KeyError) as e:
                flash((str(e) or "Ugyldige data.") + " Ingen målere blev fjernet.", "fejl")
        return redirect(url_for("kontor_ubrugte", dage=request.form.get("dage", 30), kwh=request.form.get("kwh", 1)))

    try:
        dage = min(max(int(request.args.get("dage") or 30), 1), 730)
        graense = max(float((request.args.get("kwh") or "1").replace(",", ".")), 0)
    except ValueError:
        dage, graense = 30, 1.0
    fra = nu() - dage * 86400
    rows = []
    with db.db() as c:
        for t in forbrug.aktive_tildelinger(c):
            if t["start_ts"] > fra:
                continue   # tildelt for nylig – giv den en chance
            kwh = forbrug.forbrug(c, t["maaler_id"], fra, nu() + 1)
            if kwh >= graense:
                continue
            sidst = c.execute("SELECT MAX(ts) FROM alle_delta WHERE maaler_id=? AND delta_kwh>0.01",
                              (t["maaler_id"],)).fetchone()[0]
            st = c.execute("SELECT * FROM status WHERE maaler_id=?", (t["maaler_id"],)).fetchone()
            andre = c.execute("SELECT COUNT(*) FROM tildelinger WHERE bruger_id=? AND id<>? AND start_ts<=? "
                              "AND (slut_ts IS NULL OR slut_ts>?)", (t["bruger_id"], t["id"], nu(), nu())).fetchone()[0]
            rows.append({"t": t, "kwh": kwh, "sidst_forbrug": sidst, "st": st, "andre": andre})
    return render_template("kontor_ubrugte.html", rows=rows, dage=dage, kwh=graense, i_dag=i_dag().isoformat())


def _maaned_liste():
    with db.db() as c:
        r = c.execute("SELECT MIN(ts) FROM alle_delta").fetchone()[0]
    nu_m = maaned_str(i_dag())
    m = maaned_str(lokal(r).date()) if r else nu_m
    ud = []
    while m <= nu_m:
        ud.append(m)
        m = naeste_maaned(m)
    return list(reversed(ud))


@app.route("/kontor/opgoerelse", methods=["GET", "POST"])
@kontor_kraevet
def kontor_opgoerelse():
    maaned = request.values.get("maaned") or maaned_str(i_dag())
    if not re.fullmatch(r"\d{4}-\d{2}", maaned):
        abort(400)
    if request.method == "POST" and request.form.get("handling") == "laas":
        if maaned >= maaned_str(i_dag()):
            flash("Indeværende måned kan ikke låses før den er slut.", "fejl")
        else:
            forbrug.laas_maaned(maaned)
            flash(f"Opgørelsen for {maaned_navn(maaned)} er (gen)beregnet og låst.", "ok")
        return redirect(url_for("kontor_opgoerelse", maaned=maaned))
    if request.method == "POST" and request.form.get("handling") == "send_bestyrelse":
        if not er_admin(g.bruger):
            abort(403)
        if not bestyrelse.modtagere():
            flash("Der er ingen bestyrelses-adresse sat op under System › Indstillinger.", "fejl")
        elif jobs.send_bestyrelsesrapport(maaned):
            flash(f"Månedsrapporten for {maaned_navn(maaned)} er sendt til {', '.join(bestyrelse.modtagere())}.", "ok")
        else:
            flash("Rapporten kunne ikke sendes – se System › Log.", "fejl")
        return redirect(url_for("kontor_opgoerelse", maaned=maaned))
    rows, laast = forbrug.maanedsopgoerelse(maaned)
    return render_template("kontor_opgoerelse.html", rows=rows, laast=laast, maaned=maaned,
                           fordeling=forbrug.gruppe_fordeling(rows), maaneder=_maaned_liste(),
                           afregnes=forbrug.afregnes(maaned), indevaerende=maaned == maaned_str(i_dag()),
                           laast_ts=forbrug.er_laast(maaned), bestyrelse_emails=", ".join(bestyrelse.modtagere()))


@app.route("/kontor/rapport.pdf")
@kontor_kraevet
def kontor_rapport_pdf():
    """Månedsrapporten til bestyrelsen som PDF (vises i browseren)."""
    maaned = request.args.get("maaned") or forrige_maaned(maaned_str(i_dag()))
    if not re.fullmatch(r"\d{4}-\d{2}", maaned):
        abort(400)
    try:
        data = bestyrelse.pdf(bestyrelse.data(maaned))
    except Exception as e:  # noqa: BLE001
        app.logger.exception("PDF-rapport fejlede")
        db.log("fejl", f"PDF-rapport for {maaned} fejlede: {e}")
        flash(f"PDF-rapporten kunne ikke laves: {e}", "fejl")
        return redirect(url_for("kontor_opgoerelse", maaned=maaned))
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=maanedsrapport-{maaned}.pdf"})


@app.route("/kontor/opgoerelse.csv")
@kontor_kraevet
def kontor_opgoerelse_csv():
    maaned = request.args.get("maaned") or maaned_str(i_dag())
    if not re.fullmatch(r"\d{4}-\d{2}", maaned):
        abort(400)
    return Response(rapport.opgoerelse_csv(maaned), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=elforbrug-{maaned}.csv"})


@app.route("/kontor/vinter.csv")
@kontor_kraevet
def kontor_vinter_csv():
    """Samlet opgørelse for en vinterperiode (nov–mar) pr. bruger og båd – til opkrævning ved standerhejsning."""
    aar = int(request.args.get("aar") or (i_dag().year if i_dag().month >= 11 else i_dag().year - 1))
    maaneder = [f"{aar}-{m:02d}" for m in (11, 12)] + [f"{aar + 1}-{m:02d}" for m in (1, 2, 3)]
    sum_ = {}
    for m in maaneder:
        if m > maaned_str(i_dag()):
            continue
        rows, _ = forbrug.maanedsopgoerelse(m)
        for r in rows:
            if r["bruger_id"] is None or not r["afregnes"]:
                continue
            s = sum_.setdefault((r["bruger_id"], r["baad"]), {"navn": r["navn"], "email": r["email"], "baad": r["baad"],
                                                 "pr": {}, "kwh": 0.0, "pris": 0.0})
            s["pr"][m] = s["pr"].get(m, 0) + (r["pris"] or 0)
            s["kwh"] += r["kwh"]
            s["pris"] += r["pris"] or 0
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Navn", "Email", "Båd", *[maaned_navn(m) + " (kr)" for m in maaneder], "kWh i alt", "Beløb i alt (kr)"])
    for s in sorted(sum_.values(), key=lambda s: (s["navn"].lower(), s["baad"].lower())):
        w.writerow([s["navn"], s["email"], s["baad"], *[rapport.dk(s["pr"].get(m, 0), 0) for m in maaneder],
                    rapport.dk(s["kwh"], 1), rapport.dk(s["pris"], 0)])
    return Response(buf.getvalue().encode("utf-8"), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=vinterstroem-{aar}-{aar + 1}.csv"})


# ---------------------------------------------------------------- systemadministration (kun admin)

@app.route("/system")
@admin_kraevet
def system_status():
    with db.db() as c:
        maalere = c.execute("SELECT m.id, m.placering, m.aktiv, s.* FROM maalere m JOIN status s ON s.maaler_id=m.id "
                            "ORDER BY s.tilgaengelig, s.linkquality IS NULL, s.linkquality, m.id").fetchall()
        brugere = c.execute("SELECT * FROM brugere ORDER BY sidste_login DESC").fetchall()
        haend = c.execute("SELECT * FROM haendelser ORDER BY ts DESC LIMIT 30").fetchall()
    lqi = [m["linkquality"] for m in maalere if m["linkquality"] is not None and m["tilgaengelig"]]
    login = {
        "i_alt": len(brugere),
        "aktive": sum(1 for b in brugere if b["aktiv"]),
        "kontor": sum(1 for b in brugere if b["rolle"] == "kontor"),
        "admin": sum(1 for b in brugere if b["rolle"] == "admin"),
        "30_dage": sum(1 for b in brugere if b["sidste_login"] and b["sidste_login"] > nu() - 30 * 86400),
        "aldrig": [b for b in brugere if b["aktiv"] and not b["sidste_login"]],
        "uden_kode": sum(1 for b in brugere if b["aktiv"] and not b["pw_hash"]),
    }
    return render_template("system_status.html", maalere=maalere, ha=collector.STATUS, haend=haend,
                           login=login, brugere=brugere[:25], config=config,
                           lqi={"min": min(lqi, default=None), "snit": sum(lqi) / len(lqi) if lqi else None,
                                "svage": sum(1 for x in lqi if x < 40)})


@app.route("/system/log")
@admin_kraevet
def system_log():
    type_ = request.args.get("type", "")
    q = request.args.get("q", "").strip()
    sql, args = "SELECT * FROM haendelser WHERE 1=1", []
    if type_:
        sql += " AND type=?"
        args.append(type_)
    if q:
        sql += " AND tekst LIKE ?"
        args.append(f"%{q}%")
    with db.db() as c:
        haend = c.execute(sql + " ORDER BY ts DESC LIMIT 1000", args).fetchall()
        typer = [r[0] for r in c.execute("SELECT DISTINCT type FROM haendelser ORDER BY type")]
    return render_template("system_log.html", haend=haend, typer=typer, type_=type_, q=q)


@app.route("/system/maalere", methods=["GET", "POST"])
@admin_kraevet
def system_maalere():
    """Måleropsætning: placering, aktiv og note for alle målere på én side."""
    if request.method == "POST":
        aendret = 0
        with db.db() as c:
            for m in c.execute("SELECT * FROM maalere").fetchall():
                if f"placering_{m['id']}" not in request.form:
                    continue
                ny = (request.form.get(f"placering_{m['id']}", "").strip().upper(),
                      1 if request.form.get(f"aktiv_{m['id']}") else 0,
                      request.form.get(f"note_{m['id']}", "").strip())
                if ny != (m["placering"], m["aktiv"], m["note"]):
                    c.execute("UPDATE maalere SET placering=?, aktiv=?, note=? WHERE id=?", (*ny, m["id"]))
                    aendret += 1
            if aendret:
                db.log("system", f"Måleropsætning: {aendret} målere ændret (af {g.bruger['email']})", c)
        flash(f"{aendret} måler(e) ændret." if aendret else "Ingen ændringer.", "ok")
        return redirect(url_for("system_maalere"))
    with db.db() as c:
        maalere = c.execute("SELECT m.*, s.tilgaengelig, s.sidst_set, s.energi_kwh FROM maalere m "
                            "JOIN status s ON s.maaler_id=m.id ORDER BY m.id").fetchall()
        aktive = {t["maaler_id"]: t for t in forbrug.aktive_tildelinger(c)}
    return render_template("system_maalere.html", maalere=maalere, aktive=aktive, config=config)


@app.route("/system/database", methods=["GET", "POST"])
@admin_kraevet
def system_database():
    resultat = None
    if request.method == "POST":
        h = request.form.get("handling")
        try:
            if h == "backup":
                f = backup.lav_backup(f"manuel af {g.bruger['email']}")
                flash(f"Backup lavet: {os.path.basename(f)}" +
                      (" – kopieres nu til skyen i baggrunden." if config.BACKUP_RCLONE_REMOTE else ""), "ok")
            elif h == "sky":
                threading.Thread(target=backup.kopier_til_sky, daemon=True).start()
                flash("Kopiering til skyen er startet. Genindlæs siden om lidt for at se resultatet.", "ok")
            elif h == "integritet":
                r = vedligehold.integritet()
                flash("Integritetstjek: databasen er i orden." if r == ["ok"] else
                      "Integritetstjek fandt fejl: " + "; ".join(r[:10]), "ok" if r == ["ok"] else "fejl")
            elif h == "optimer":
                sparet = vedligehold.optimer(g.bruger["email"])
                flash(f"Databasen er optimeret ({sparet} kB frigjort).", "ok")
            elif h in ("vis_forbrug", "nulstil_forbrug"):
                nr = int(request.form["maaler"])
                # fra kl. (standard 00) på fra-dato til kl. (standard: døgnets slut) på til-dato
                fra = dato_ts(parse_dato(request.form["fra"])) + int(request.form.get("fra_time") or 0) * 3600
                til_d = parse_dato(request.form["til"])
                til = (dato_ts(til_d) + int(request.form["til_time"]) * 3600 if request.form.get("til_time")
                       else dato_ts(til_d + timedelta(days=1)))
                if h == "vis_forbrug":
                    kwh, n = vedligehold.fejlmaalt_forbrug(nr, fra, til)
                    resultat = {"form": request.form, "kwh": kwh, "n": n, "nr": nr,
                                "aflaes": _aflaesninger(nr, fra, til)}
                else:
                    if request.form.get("bekraeft") != "JA":
                        raise ValueError("Skriv JA i bekræft-feltet for at nulstille forbruget.")
                    kwh, n, laast = vedligehold.nulstil_forbrug(nr, fra, til, request.form.get("aarsag", "").strip(),
                                                                g.bruger["email"])
                    flash(f"{rapport.dk(kwh, 2)} kWh i {n} aflæsninger er nulstillet på måler {nr:03d}. Der blev taget "
                          "backup først." + (f" Låste måneder genberegnet: {', '.join(laast)}." if laast else ""), "ok")
        except (ValueError, KeyError, RuntimeError) as e:
            flash(str(e) or "Ugyldige værdier.", "fejl")
        if resultat is None:
            return redirect(url_for("system_database"))
    return render_template("system_database.html", info=vedligehold.info(), backups=backup.liste(), config=config,
                           resultat=resultat, i_dag=i_dag().isoformat(), sky=backup.sky_status())


def _aflaesninger(nr, fra, til):
    with db.db() as c:
        return c.execute("SELECT * FROM aflaesninger WHERE maaler_id=? AND ts>? AND ts<=? AND delta_kwh<>0 "
                         "ORDER BY delta_kwh DESC LIMIT 20", (nr, fra, til)).fetchall()


@app.route("/system/indstillinger", methods=["GET", "POST"])
@admin_kraevet
def system_indstillinger():
    if request.method == "POST":
        h = request.form.get("handling")
        try:
            if h == "mails":
                emails = [e.strip() for e in request.form.get("ekstra_kontor_emails", "").replace(";", ",").split(",")
                          if e.strip()]
                for e in emails:
                    if not EMAIL_RE.match(e):
                        raise ValueError(f"Ugyldig email: {e}")
                db.gem("ekstra_kontor_emails", ", ".join(emails))
                best = [e.strip() for e in request.form.get("bestyrelse_emails", "").replace(";", ",").split(",")
                        if e.strip()]
                for e in best:
                    if not EMAIL_RE.match(e):
                        raise ValueError(f"Ugyldig email: {e}")
                db.gem("bestyrelse_emails", ", ".join(best))
                for f in ["offline", "dag", "uge", "maaned"]:
                    db.gem(f"ekstra_mail_{f}", bool(request.form.get(f"ekstra_mail_{f}")))
                db.gem("mail_tidspunkt", f"{int(request.form['mail_time']):02d}:{int(request.form['mail_min']):02d}")
                db.gem("offline_minutter", max(10, int(request.form["offline_minutter"])))
                db.gem("advarsel_procent", min(99, max(50, int(request.form["advarsel_procent"]))))
                db.log("indstilling", f"Mail-opsætning ændret af {g.bruger['email']}")
                flash("Email-opsætning gemt.", "ok")
            elif h == "kategorier":
                kat = []
                for i in range(10):
                    til, pris = request.form.get(f"til_{i}", "").strip(), request.form.get(f"pris_{i}", "").strip()
                    if pris == "":
                        continue
                    kat.append({"til": int(til) if til else None, "pris": float(pris)})
                if not kat or kat[-1]["til"] is not None or any(k["til"] is None for k in kat[:-1]):
                    raise ValueError("Sidste gruppe skal være uden øvre grænse, alle andre skal have en.")
                if any(kat[i]["til"] >= kat[i + 1]["til"] for i in range(len(kat) - 2)):
                    raise ValueError("Grænserne skal være stigende.")
                db.gem("kategorier", kat)
                db.gem("afregningsmaaneder", sorted(int(m) for m in request.form.getlist("mdr")))
                db.log("indstilling", f"Forbrugsgrupper ændret af {g.bruger['email']}: {kat}")
                flash("Forbrugsgrupper gemt. Låste måneder ændres ikke, før de genberegnes.", "ok")
            elif h == "testmail":
                ok = mailer.send(g.bruger["email"], "Testmail fra elmålerne", "Det virker! Mailopsætningen er i orden.")
                flash("Testmail sendt til " + g.bruger["email"] if ok else
                      "Mail kunne ikke sendes – se systemloggen.", "ok" if ok else "fejl")
        except (ValueError, KeyError) as e:
            flash(str(e) or "Ugyldige værdier.", "fejl")
        return redirect(url_for("system_indstillinger"))
    mt = (db.hent("mail_tidspunkt") or "06:00").split(":")
    return render_template("system_indstillinger.html", kat=db.hent("kategorier"), mdr=db.hent("afregningsmaaneder"),
                           ind={k: db.hent(k) for k in db.STANDARD_INDSTILLINGER}, mail_time=int(mt[0]),
                           mail_min=int(mt[1]), smtp=mailer.konfigureret(), config=config)


@app.route("/system/backup/<navn>")
@admin_kraevet
def system_backup_hent(navn):
    if not re.fullmatch(r"elmaaler-\d{8}-\d{6}\.db\.gz", navn):
        abort(404)
    return send_from_directory(config.BACKUP_DIR, navn, as_attachment=True)


@app.route("/system/debug", methods=["GET", "POST"])
@admin_kraevet
def system_debug():
    """Simulér forbrug på udvalgte målere – til test af opgørelser, grupper og grafer."""
    if request.method == "POST":
        h = request.form.get("handling")
        try:
            if h == "tilfoej":
                maalere = simulering.parse_maalere(request.form.get("maalere", ""))
                kwh = float(request.form.get("kwh", "0").replace(",", "."))
                laast = simulering.tilfoej(maalere, parse_dato(request.form["fra"]), parse_dato(request.form["til"]),
                                           kwh, request.form.get("profil", "dag"), g.bruger["email"])
                flash(f"{kwh:g} kWh simuleret på {len(maalere)} måler(e)."
                      + (f" Låste måneder genberegnet: {', '.join(laast)}." if laast else ""), "ok")
            elif h in ("slet", "slet_alle"):
                nr = int(request.form["maaler_id"]) if h == "slet" else None
                laast = simulering.slet(nr, g.bruger["email"])
                flash("Simuleret forbrug slettet."
                      + (f" Låste måneder genberegnet: {', '.join(laast)}." if laast else ""), "ok")
        except (ValueError, KeyError) as e:
            flash(str(e) or "Ugyldige værdier.", "fejl")
        return redirect(url_for("system_debug"))
    idag = i_dag()
    return render_template("system_debug.html", sim=simulering.oversigt(), kategorier=forbrug.kategorier(),
                           i_dag=idag.isoformat(), maaned_start=idag.replace(day=1).isoformat())


@app.route("/kontor/import", methods=["GET", "POST"])
@kontor_kraevet
def kontor_import():
    """CSV med kolonnerne: maaler;placering;email;navn;baad;telefon;start_dato (alt undtagen maaler valgfrit)."""
    resultat = None
    if request.method == "POST":
        f = request.files.get("fil")
        if not f:
            flash("Vælg en fil.", "fejl")
            return redirect(url_for("kontor_import"))
        tekst = f.read().decode("utf-8-sig", errors="replace")
        dialekt = ";" if tekst.count(";") >= tekst.count(",") else ","
        rows = list(csv.DictReader(io.StringIO(tekst), delimiter=dialekt))
        inviter = bool(request.form.get("inviter"))
        resultat = {"ok": 0, "nye": 0, "fejl": []}
        for i, r in enumerate(rows, start=2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            try:
                nr = int(r.get("maaler") or r.get("måler") or 0)
                if not 1 <= nr <= config.METER_COUNT:
                    raise ValueError(f"målernummer '{nr}' findes ikke")
                with db.db() as c:
                    if r.get("placering") and er_admin(g.bruger):   # måleropsætning er systemadministration
                        c.execute("UPDATE maalere SET placering=? WHERE id=?", (r["placering"].upper(), nr))
                    if r.get("email"):
                        b, ny = _find_eller_opret_bruger(c, r["email"], r.get("navn", ""))
                        baad = r.get("baad") or r.get("båd") or ""
                        c.execute("UPDATE brugere SET telefon=COALESCE(NULLIF(?,''), telefon) WHERE id=?",
                                  (r.get("telefon", ""), b["id"]))
                        aktiv = forbrug.aktive_tildelinger(c, maaler_id=nr)
                        if not (aktiv and aktiv[0]["bruger_id"] == b["id"]):
                            _tildel(c, nr, b["id"], parse_dato(r.get("start_dato") or i_dag().isoformat()), baad)
                        elif baad:
                            c.execute("UPDATE tildelinger SET baad=? WHERE id=?", (baad, aktiv[0]["id"]))
                        resultat["nye"] += ny
                if r.get("email") and inviter and ny:
                    with db.db() as c:
                        b = c.execute("SELECT * FROM brugere WHERE id=?", (b["id"],)).fetchone()
                    _send_link(b, "invitation")
                resultat["ok"] += 1
            except Exception as e:  # noqa: BLE001
                resultat["fejl"].append(f"Linje {i}: {e}")
        db.log("import", f"CSV-import af {g.bruger['email']}: {resultat['ok']} ok, {len(resultat['fejl'])} fejl")
    return render_template("kontor_import.html", resultat=resultat)


@app.errorhandler(403)
def e403(e):
    return render_template("fejl.html", kode=403, tekst="Du har ikke adgang til denne side."), 403


@app.errorhandler(404)
def e404(e):
    return render_template("fejl.html", kode=404, tekst="Siden findes ikke."), 404


@app.errorhandler(400)
def e400(e):
    return render_template("fejl.html", kode=400, tekst=getattr(e, "description", "Ugyldig forespørgsel.")), 400


@app.route("/sundhed")
def sundhed():
    return jsonify(ok=True, ha=collector.STATUS.get("ok"))
