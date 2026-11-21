"""Webinterface: bruger-login (Min side) og havnekontor-login."""
import csv
import io
import os
import re
import secrets
from datetime import timedelta
from functools import wraps

from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from . import backup, collector, config, db, forbrug, mailer, rapport, sikkerhed
from .tid import (nu, i_dag, maaned_str, maaned_grænser, maaned_navn, naeste_maaned,
                  parse_dato, dato_ts, fmt_tid, fmt_dato, siden, lokal)

app = Flask(__name__)
if config.PROXY_HOPS > 0:
    # Bag Cloudflare Tunnel/Caddy/Nginx: brug klientens rigtige IP, https og værtsnavn fra proxyen
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.PROXY_HOPS, x_proto=config.PROXY_HOPS,
                            x_host=config.PROXY_HOPS)
app.secret_key = config.secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- filtre og fælles

app.jinja_env.filters.update(dk=rapport.dk, tid=fmt_tid, dato=fmt_dato, siden=siden, maaned_navn=maaned_navn)


LOKALE_VAERTER = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def base_url_er_lokal():
    """True hvis BASE_URL peger på denne maskine (localhost) – så virker links i mails ikke for andre."""
    return (urlsplit(config.BASE_URL).hostname or "") in LOKALE_VAERTER


def base_url_advarsel():
    """Tekst til havnekontoret hvis BASE_URL ikke passer med den adresse, siden bliver brugt på."""
    if not g.get("bruger") or g.bruger["rolle"] != "kontor":
        return None
    base = urlsplit(config.BASE_URL)
    her = request.host.split(":")[0].lower()
    if base_url_er_lokal() and her not in LOKALE_VAERTER:
        return (f"BASE_URL er sat til {config.BASE_URL}, så links i mails (invitation, nulstil kode) "
                f"virker ikke for brugerne. Sæt BASE_URL={request.scheme}://{request.host} i .env og genstart.")
    if base.scheme == "http" and request.scheme == "https" and (base.hostname or "") == her:
        return (f"Siden bruges via HTTPS, men BASE_URL er {config.BASE_URL}. "
                f"Sæt BASE_URL=https://{request.host} i .env og genstart.")
    return None


@app.context_processor
def faelles():
    return {"klub": config.KLUB_NAVN, "bruger": g.get("bruger"), "csrf": csrf_token,
            "maaned_navn": maaned_navn, "nu": nu(), "base_url_advarsel": base_url_advarsel}


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
        if g.bruger["rolle"] != "kontor":
            abort(403)
        return f(*a, **kw)
    return w


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
    return redirect(url_for("kontor" if g.bruger["rolle"] == "kontor" else "min_side"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        kode = request.form.get("kode", "")
        ip = request.remote_addr or ""   # rigtig klient-IP via ProxyFix (kan ikke forfalskes af klienten)
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
    fra, til, d0, d1 = maaned_grænser(maaned)
    serie = forbrug.dagsserie(c, [t["maaler_id"]], max(d0, parse_dato(t["start_dato"])), i_dag() + timedelta(days=1))
    return {
        "t": t, "st": st, "m": ms,
        "total_siden_start": forbrug.forbrug(c, t["maaler_id"], t["start_ts"], nu() + 1),
        "serie": [(d.day, v) for d, v in serie.items()],
        "i_dag": serie.get(i_dag(), 0.0),
    }


@app.route("/min-side")
@login_kraevet
def min_side():
    maaned = maaned_str(i_dag())
    with db.db() as c:
        tild = forbrug.aktive_tildelinger(c, bruger_id=g.bruger["id"])
        kort = [_maaler_kort(c, t, maaned) for t in tild]
    return render_template("min_side.html", kort=kort, maaned=maaned,
                           historik=forbrug.bruger_historik(g.bruger["id"]),
                           kategorier=forbrug.kategorier(), afregnes=forbrug.afregnes(maaned))


@app.route("/min-side/emails", methods=["POST"])
@login_kraevet
def mine_emails():
    felter = ["mail_advarsel", "mail_dag", "mail_uge", "mail_maaned", "mail_offline"]
    with db.db() as c:
        c.execute(f"UPDATE brugere SET {', '.join(f + '=?' for f in felter)} WHERE id=?",
                  (*[1 if request.form.get(f) else 0 for f in felter], g.bruger["id"]))
    flash("Dine email-valg er gemt.", "ok")
    return redirect(request.form.get("tilbage") or url_for("min_side"))


@app.route("/min-side/konto", methods=["GET", "POST"])
@login_kraevet
def min_konto():
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
                    mailer.send(  # efter commitb["email"], "Din email er ændret",
                                f"Din email til elmålerne er ændret fra {b['email']} til {ny}.\n"
                                "Har du ikke selv gjort det, så kontakt havnekontoret.")
                    flash("Din email er ændret.", "ok")
                except Exception:  # noqa: BLE001
                    flash("Den email bruges allerede af en anden bruger.", "fejl")
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


@app.route("/api/live")
@login_kraevet
def api_live():
    with db.db() as c:
        if g.bruger["rolle"] == "kontor" and request.args.get("alle"):
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
        effekt = forbrug.effektserie(c, 24)
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
    return render_template("kontor.html", tabel=tabel, tal=tal, maaned=maaned, ha=collector.STATUS,
                           dagserie=[(d.day, v) for d, v in dag.items()],
                           effektserie=[(lokal(ts).strftime("%H:%M"), kw) for ts, kw in effekt])


def _find_eller_opret_bruger(c, email, navn=""):
    email = email.strip().lower()
    b = c.execute("SELECT * FROM brugere WHERE email=?", (email,)).fetchone()
    if b:
        return b, False
    if not EMAIL_RE.match(email):
        raise ValueError(f"Ugyldig email: {email}")
    c.execute("INSERT INTO brugere(email, navn, oprettet) VALUES (?,?,?)", (email, navn.strip(), nu()))
    return c.execute("SELECT * FROM brugere WHERE email=?", (email,)).fetchone(), True


def _tildel(c, maaler_id, bruger_id, start):
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
    c.execute("INSERT INTO tildelinger(maaler_id, bruger_id, start_dato, start_ts, oprettet) VALUES (?,?,?,?,?)",
              (maaler_id, bruger_id, start.isoformat(), s_ts, nu()))


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
                    _tildel(c, nr, b["id"], parse_dato(request.form["start_dato"]))
                    db.log("tildeling", f"Måler {nr:03d} tildelt {b['email']} fra {request.form['start_dato']} "
                                        f"(af {g.bruger['email']})", c)
                    flash(f"Måler {nr:03d} er tildelt {b['navn'] or b['email']}.", "ok")
                elif h == "afslut":
                    tid_ = int(request.form["tildeling_id"])
                    slut = parse_dato(request.form["slut_dato"])
                    t = c.execute("SELECT * FROM tildelinger WHERE id=? AND maaler_id=?", (tid_, nr)).fetchone()
                    if not t or dato_ts(slut) <= t["start_ts"]:
                        raise ValueError("Slutdato skal være efter startdato.")
                    c.execute("UPDATE tildelinger SET slut_dato=?, slut_ts=? WHERE id=?",
                              (slut.isoformat(), dato_ts(slut), tid_))
                    db.log("tildeling", f"Tildeling af måler {nr:03d} afsluttet pr. {slut} (af {g.bruger['email']})", c)
                    flash("Tildelingen er afsluttet.", "ok")
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
        d0 = i_dag() - timedelta(days=30)
        serie = forbrug.dagsserie(c, [nr], d0, i_dag() + timedelta(days=1))
        aflaes = c.execute("SELECT * FROM aflaesninger WHERE maaler_id=? ORDER BY ts DESC LIMIT 12", (nr,)).fetchall()
        fra, til, *_ = maaned_grænser(maaned)
        kwh = forbrug.forbrug(c, nr, fra, til)
    return render_template("kontor_maaler.html", m=m, hist=hist, brugere=brugere, aflaes=aflaes,
                           serie=[(d.strftime("%d/%m"), v) for d, v in serie.items()], kwh=kwh,
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
                rolle = "kontor" if request.form.get("rolle") == "kontor" else "bruger"
                c.execute("UPDATE brugere SET baad=?, telefon=?, rolle=? WHERE id=?",
                          (request.form.get("baad", "").strip(), request.form.get("telefon", "").strip(), rolle, b["id"]))
                kode = request.form.get("kode", "")
                if kode:
                    if sikkerhed.kode_ok(kode):
                        raise ValueError(sikkerhed.kode_ok(kode))
                    c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (sikkerhed.hash_kode(kode), b["id"]))
                maaler = request.form.get("maaler")
                if maaler:
                    _tildel(c, int(maaler), b["id"], parse_dato(request.form.get("start_dato") or i_dag().isoformat()))
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
            "  WHERE t.bruger_id=b.id AND t.start_ts<=?1 AND (t.slut_ts IS NULL OR t.slut_ts>?1)) maalere "
            "FROM brugere b ORDER BY b.rolle DESC, b.navn COLLATE NOCASE, b.email", (nu(),)).fetchall()
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
        try:
            with db.db() as c:
                if h == "gem":
                    email = request.form.get("email", "").strip().lower()
                    if not EMAIL_RE.match(email):
                        raise ValueError("Ugyldig email.")
                    rolle = "kontor" if request.form.get("rolle") == "kontor" else "bruger"
                    aktiv = 1 if request.form.get("aktiv") else 0
                    if bid == g.bruger["id"] and (rolle != "kontor" or not aktiv):
                        raise ValueError("Du kan ikke fjerne din egen kontor-adgang.")
                    c.execute("UPDATE brugere SET email=?, navn=?, baad=?, telefon=?, rolle=?, aktiv=? WHERE id=?",
                              (email, request.form.get("navn", "").strip(), request.form.get("baad", "").strip(),
                               request.form.get("telefon", "").strip(), rolle, aktiv, bid))
                    flash("Bruger gemt.", "ok")
                elif h == "kode":
                    kode = request.form.get("kode", "")
                    if sikkerhed.kode_ok(kode):
                        raise ValueError(sikkerhed.kode_ok(kode))
                    c.execute("UPDATE brugere SET pw_hash=? WHERE id=?", (sikkerhed.hash_kode(kode), bid))
                    flash("Ny adgangskode sat.", "ok")
                elif h == "tildel":
                    _tildel(c, int(request.form["maaler"]), bid, parse_dato(request.form["start_dato"]))
                    flash("Måler tildelt.", "ok")
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


def _maaned_liste():
    with db.db() as c:
        r = c.execute("SELECT MIN(ts) FROM aflaesninger").fetchone()[0]
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
    rows, laast = forbrug.maanedsopgoerelse(maaned)
    return render_template("kontor_opgoerelse.html", rows=rows, laast=laast, maaned=maaned,
                           fordeling=forbrug.gruppe_fordeling(rows), maaneder=_maaned_liste(),
                           afregnes=forbrug.afregnes(maaned), indevaerende=maaned == maaned_str(i_dag()),
                           laast_ts=forbrug.er_laast(maaned))


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
    """Samlet opgørelse for en vinterperiode (nov–mar) pr. bruger – til opkrævning ved standerhejsning."""
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
            s = sum_.setdefault(r["bruger_id"], {"navn": r["navn"], "email": r["email"], "baad": r["baad"],
                                                 "pr": {}, "kwh": 0.0, "pris": 0.0})
            s["pr"][m] = s["pr"].get(m, 0) + (r["pris"] or 0)
            s["kwh"] += r["kwh"]
            s["pris"] += r["pris"] or 0
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Navn", "Email", "Båd", *[maaned_navn(m) + " (kr)" for m in maaneder], "kWh i alt", "Beløb i alt (kr)"])
    for s in sorted(sum_.values(), key=lambda s: s["navn"].lower()):
        w.writerow([s["navn"], s["email"], s["baad"], *[rapport.dk(s["pr"].get(m, 0), 0) for m in maaneder],
                    rapport.dk(s["kwh"], 1), rapport.dk(s["pris"], 0)])
    return Response(buf.getvalue().encode("utf-8"), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=vinterstroem-{aar}-{aar + 1}.csv"})


@app.route("/kontor/netvaerk")
@kontor_kraevet
def kontor_netvaerk():
    with db.db() as c:
        maalere = c.execute("SELECT m.id, m.placering, m.aktiv, s.* FROM maalere m JOIN status s ON s.maaler_id=m.id "
                            "ORDER BY s.tilgaengelig, s.linkquality IS NULL, s.linkquality, m.id").fetchall()
        brugere = c.execute("SELECT * FROM brugere ORDER BY sidste_login DESC").fetchall()
        haend = c.execute("SELECT * FROM haendelser ORDER BY ts DESC LIMIT 150").fetchall()
    lqi = [m["linkquality"] for m in maalere if m["linkquality"] is not None and m["tilgaengelig"]]
    login = {
        "i_alt": len(brugere),
        "aktive": sum(1 for b in brugere if b["aktiv"]),
        "kontor": sum(1 for b in brugere if b["rolle"] == "kontor"),
        "30_dage": sum(1 for b in brugere if b["sidste_login"] and b["sidste_login"] > nu() - 30 * 86400),
        "aldrig": [b for b in brugere if b["aktiv"] and not b["sidste_login"]],
        "uden_kode": sum(1 for b in brugere if b["aktiv"] and not b["pw_hash"]),
    }
    return render_template("kontor_netvaerk.html", maalere=maalere, ha=collector.STATUS, haend=haend,
                           login=login, brugere=brugere[:25], config=config,
                           lqi={"min": min(lqi, default=None), "snit": sum(lqi) / len(lqi) if lqi else None,
                                "svage": sum(1 for x in lqi if x < 40)})


@app.route("/kontor/indstillinger", methods=["GET", "POST"])
@kontor_kraevet
def kontor_indstillinger():
    if request.method == "POST":
        h = request.form.get("handling")
        try:
            if h == "mails":
                with db.db() as c:
                    felter = ["mail_offline", "mail_dag", "mail_uge", "mail_maaned"]
                    c.execute(f"UPDATE brugere SET {', '.join(f + '=?' for f in felter)} WHERE id=?",
                              (*[1 if request.form.get(f) else 0 for f in felter], g.bruger["id"]))
                emails = [e.strip() for e in request.form.get("ekstra_kontor_emails", "").replace(";", ",").split(",")
                          if e.strip()]
                for e in emails:
                    if not EMAIL_RE.match(e):
                        raise ValueError(f"Ugyldig email: {e}")
                db.gem("ekstra_kontor_emails", ", ".join(emails))
                for f in ["offline", "dag", "uge", "maaned"]:
                    db.gem(f"ekstra_mail_{f}", bool(request.form.get(f"ekstra_mail_{f}")))
                db.gem("mail_tidspunkt", f"{int(request.form['mail_time']):02d}:{int(request.form['mail_min']):02d}")
                db.gem("offline_minutter", max(10, int(request.form["offline_minutter"])))
                db.gem("advarsel_procent", min(99, max(50, int(request.form["advarsel_procent"]))))
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
                      "Mail kunne ikke sendes – se hændelsesloggen under Netværk.", "ok" if ok else "fejl")
            elif h == "backup":
                f = backup.lav_backup(f"manuel af {g.bruger['email']}")
                flash(f"Backup lavet: {os.path.basename(f)}", "ok")
        except (ValueError, KeyError) as e:
            flash(str(e) or "Ugyldige værdier.", "fejl")
        return redirect(url_for("kontor_indstillinger"))
    mt = (db.hent("mail_tidspunkt") or "06:00").split(":")
    return render_template("kontor_indstillinger.html", kat=db.hent("kategorier"), mdr=db.hent("afregningsmaaneder"),
                           ind={k: db.hent(k) for k in db.STANDARD_INDSTILLINGER}, mail_time=int(mt[0]),
                           mail_min=int(mt[1]), smtp=mailer.konfigureret(), config=config, backups=backup.liste())


@app.route("/kontor/backup/<navn>")
@kontor_kraevet
def kontor_backup_hent(navn):
    if not re.fullmatch(r"elmaaler-\d{8}-\d{6}\.db\.gz", navn):
        abort(404)
    return send_from_directory(config.BACKUP_DIR, navn, as_attachment=True)


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
                    if r.get("placering"):
                        c.execute("UPDATE maalere SET placering=? WHERE id=?", (r["placering"].upper(), nr))
                    if r.get("email"):
                        b, ny = _find_eller_opret_bruger(c, r["email"], r.get("navn", ""))
                        c.execute("UPDATE brugere SET baad=COALESCE(NULLIF(?,''), baad), "
                                  "telefon=COALESCE(NULLIF(?,''), telefon) WHERE id=?",
                                  (r.get("baad") or r.get("båd") or "", r.get("telefon", ""), b["id"]))
                        aktiv = forbrug.aktive_tildelinger(c, maaler_id=nr)
                        if not (aktiv and aktiv[0]["bruger_id"] == b["id"]):
                            _tildel(c, nr, b["id"], parse_dato(r.get("start_dato") or i_dag().isoformat()))
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
