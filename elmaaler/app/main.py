"""Start: database, første kontor-bruger, baggrundsjob og webserver."""
import logging
import sys
import threading

from waitress import serve

from . import config, db, jobs, sikkerhed
from .tid import nu


def opret_admin():
    if not (config.ADMIN_EMAIL and config.ADMIN_PASSWORD):
        return
    with db.db() as c:
        if c.execute("SELECT 1 FROM brugere WHERE rolle='kontor'").fetchone():
            return
        c.execute("INSERT OR IGNORE INTO brugere(email, navn, rolle, pw_hash, oprettet) VALUES (?,?,?,?,?)",
                  (config.ADMIN_EMAIL.lower(), "Havnekontor", "kontor", sikkerhed.hash_kode(config.ADMIN_PASSWORD), nu()))
        c.execute("UPDATE brugere SET rolle='kontor' WHERE email=?", (config.ADMIN_EMAIL.lower(),))
    db.log("bruger", f"Første kontor-bruger oprettet: {config.ADMIN_EMAIL}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    db.init()
    opret_admin()
    if "--kun-web" not in sys.argv:
        threading.Thread(target=jobs.koer, name="jobs", daemon=True).start()
    from .web import app
    logging.getLogger("main").info("Elmåler-web starter på port %d (kilde: %s)", config.PORT, config.SOURCE)
    serve(app, host="0.0.0.0", port=config.PORT, threads=8, url_scheme="https" if config.COOKIE_SECURE else "http")


if __name__ == "__main__":
    main()
