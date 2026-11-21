"""Tidshjælpere. Alt gemmes som UTC epoch-sekunder; perioder regnes i dansk tid."""
import time
from datetime import datetime, date, timedelta

from .config import TZ

MAANEDER = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
            "august", "september", "oktober", "november", "december"]
UGEDAGE = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]


def nu() -> int:
    return int(time.time())


def lokal(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def dato_ts(d: date) -> int:
    """Epoch for kl. 00:00 dansk tid på datoen d."""
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def i_dag() -> date:
    return lokal(nu()).date()


def maaned_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def maaned_grænser(maaned: str):
    """'2026-11' -> (fra_ts, til_ts, første dato, første dato i næste måned)."""
    y, m = (int(x) for x in maaned.split("-"))
    start = date(y, m, 1)
    slut = date(y + (m == 12), m % 12 + 1, 1)
    return dato_ts(start), dato_ts(slut), start, slut


def forrige_maaned(maaned: str) -> str:
    y, m = (int(x) for x in maaned.split("-"))
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


def naeste_maaned(maaned: str) -> str:
    y, m = (int(x) for x in maaned.split("-"))
    return f"{y + (m == 12):04d}-{m % 12 + 1:02d}"


def maaned_navn(maaned: str) -> str:
    y, m = (int(x) for x in maaned.split("-"))
    return f"{MAANEDER[m - 1]} {y}"


def dag_grænser(d: date):
    return dato_ts(d), dato_ts(d + timedelta(days=1))


def parse_dato(s: str) -> date:
    return date.fromisoformat(s.strip())


def fmt_tid(ts):
    if not ts:
        return "–"
    return lokal(ts).strftime("%d-%m-%Y %H:%M")


def fmt_dato(s):
    if not s:
        return "–"
    if isinstance(s, (int, float)):
        return lokal(s).strftime("%d-%m-%Y")
    d = date.fromisoformat(s) if isinstance(s, str) else s
    return d.strftime("%d-%m-%Y")


def siden(ts):
    """Kort 'for X min siden' tekst."""
    if not ts:
        return "aldrig"
    s = nu() - ts
    if s < 90:
        return "lige nu"
    if s < 3600:
        return f"{s // 60} min siden"
    if s < 86400 * 2:
        return f"{s // 3600} t siden"
    return f"{s // 86400} dage siden"
