"""Adgangskoder, engangs-links og simpel beskyttelse mod gætteangreb."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from . import db
from .tid import nu

ITER = 310_000


def hash_kode(kode: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", kode.encode(), salt.encode(), ITER).hex()
    return f"pbkdf2_sha256${ITER}${salt}${h}"


def tjek_kode(kode: str, gemt: str) -> bool:
    if not gemt:
        return False
    try:
        _, it, salt, h = gemt.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", kode.encode(), salt.encode(), int(it)).hex()
    return hmac.compare_digest(test, h)


def kode_ok(kode: str):
    """Returnerer fejltekst eller None."""
    if len(kode) < 8:
        return "Adgangskoden skal være mindst 8 tegn."
    return None


def ny_token(bruger_id, formaal="nulstil", gyldig_timer=48):
    t = secrets.token_urlsafe(32)
    with db.db() as c:
        c.execute("DELETE FROM tokens WHERE udloeber < ?", (nu(),))
        c.execute("INSERT INTO tokens VALUES (?,?,?,?)", (t, bruger_id, formaal, nu() + gyldig_timer * 3600))
    return t


def brug_token(t, slet=False):
    with db.db() as c:
        r = c.execute("SELECT * FROM tokens WHERE token=? AND udloeber>?", (t, nu())).fetchone()
        if r and slet:
            c.execute("DELETE FROM tokens WHERE bruger_id=? AND formaal=?", (r["bruger_id"], r["formaal"]))
    return r


class Begraenser:
    """Maks N mislykkede logins pr. IP/email inden for et tidsvindue."""

    def __init__(self, maks=10, vindue=900):
        self.maks, self.vindue = maks, vindue
        self.forsoeg = defaultdict(deque)

    def blokeret(self, noegle):
        q = self.forsoeg[noegle]
        while q and q[0] < time.time() - self.vindue:
            q.popleft()
        return len(q) >= self.maks

    def fejl(self, noegle):
        self.forsoeg[noegle].append(time.time())

    def nulstil(self, noegle):
        self.forsoeg.pop(noegle, None)


login_begraenser = Begraenser()
