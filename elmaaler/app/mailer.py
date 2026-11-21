"""Afsendelse af email via SMTP."""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config, db

log = logging.getLogger("mail")

SIGNATUR = f"\n\n--\n{config.KLUB_NAVN} – elmålere\n{config.BASE_URL}\n" \
           "Du kan slå disse emails til og fra under 'Min side'."


def konfigureret():
    return bool(config.SMTP_HOST)


def send(til, emne, tekst, vedhaeft=None, signatur=True):
    """til: str eller liste. vedhaeft: liste af (filnavn, bytes, mimetype)."""
    modtagere = [til] if isinstance(til, str) else [t for t in til if t]
    if not modtagere:
        return False
    if not konfigureret():
        log.info("SMTP ikke sat op – mail til %s ikke sendt: %s", modtagere, emne)
        return False
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(modtagere)
    msg["Subject"] = emne
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config.SMTP_FROM.split("@")[-1])
    msg.set_content(tekst + (SIGNATUR if signatur else ""))
    for navn, data, mime in vedhaeft or []:
        hoved, under = mime.split("/")
        msg.add_attachment(data, maintype=hoved, subtype=under, filename=navn)
    try:
        if config.SMTP_SECURITY == "ssl":
            s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30,
                                 context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
            if config.SMTP_SECURITY == "starttls":
                s.starttls(context=ssl.create_default_context())
        with s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Mail fejlede (%s): %s", emne, e)
        db.log("fejl", f"Mail '{emne}' til {', '.join(modtagere)} fejlede: {e}")
        return False
