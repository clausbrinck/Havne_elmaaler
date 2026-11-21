"""Backup af databasen: konsistent kopi via SQLites backup-API, gzip, oprydning og kopi uden for serveren.

- Lokalt (i data-volumen): daglige backups gemmes BACKUP_KEEP_DAYS dage; den første backup i hver
  måned gemmes 13 måneder, så afregninger kan genskabes efter standerhejsning.
- BACKUP_EXTRA_DIR: ekstra kopi på USB/NAS (samme oprydning).
- BACKUP_RCLONE_REMOTE: kopi med rclone, fx til en krypteret OneDrive-mappe ("krypt:"). Der bruges
  "rclone copy" – aldrig "sync" – så en tømt server ikke kan slette kopierne i skyen. Skyen ryddes kun
  for filer ældre end BACKUP_RCLONE_KEEP_DAYS.
"""
import glob
import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import threading

from . import config, db
from .tid import fmt_tid, lokal, nu

MAANEDS_DAGE = 400          # månedsbackups (den 1.) gemmes ca. 13 måneder
_sky_laas = threading.Lock()
SKY = {"sidst_ok": None, "sidst_fejl": None, "fejl": "", "koerer": False}


def lav_backup(aarsag="automatisk"):
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    navn = f"elmaaler-{lokal(nu()):%Y%m%d-%H%M%S}.db"
    tmp = os.path.join(config.BACKUP_DIR, navn + ".tmp")
    src = db.connect()
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        os.remove(tmp)
        db.log("fejl", f"Backup fejlede integritetstjek: {ok}")
        raise RuntimeError(f"Integritetstjek fejlede: {ok}")
    ud = os.path.join(config.BACKUP_DIR, navn + ".gz")
    with open(tmp, "rb") as fi, gzip.open(ud, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    os.remove(tmp)

    mapper = [config.BACKUP_DIR]
    if config.BACKUP_EXTRA_DIR:
        try:
            os.makedirs(config.BACKUP_EXTRA_DIR, exist_ok=True)
            shutil.copy2(ud, config.BACKUP_EXTRA_DIR)
            mapper.append(config.BACKUP_EXTRA_DIR)
        except OSError as e:
            db.log("fejl", f"Kunne ikke kopiere backup til {config.BACKUP_EXTRA_DIR}: {e}")
    for mappe in mapper:
        _ryd_op(mappe)

    db.log("backup", f"Backup ({aarsag}): {os.path.basename(ud)} – {os.path.getsize(ud) // 1024} kB")
    if config.BACKUP_RCLONE_REMOTE:
        # i baggrunden, så "Lav backup nu" på System-siden ikke venter på upload
        threading.Thread(target=kopier_til_sky, daemon=True).start()
    return ud


def _er_maanedsbackup(sti):
    """Den første backup i en måned (dato den 1.) gemmes længere."""
    m = re.search(r"elmaaler-(\d{4})(\d{2})(\d{2})-", os.path.basename(sti))
    return bool(m and m.group(3) == "01")


def _ryd_op(mappe):
    t = nu()
    for f in glob.glob(os.path.join(mappe, "elmaaler-*.db.gz")):
        alder = t - os.path.getmtime(f)
        if alder > config.BACKUP_KEEP_DAYS * 86400 and not (_er_maanedsbackup(f) and alder < MAANEDS_DAGE * 86400):
            os.remove(f)


def kopier_til_sky():
    """Kopiér nye backups til BACKUP_RCLONE_REMOTE. Logger resultatet. Returnerer True ved succes."""
    from . import mailer                      # her for at undgå cirkulær import
    if not config.BACKUP_RCLONE_REMOTE:
        return False
    if not _sky_laas.acquire(blocking=False):
        return False                          # en kopiering kører allerede
    SKY["koerer"] = True
    try:
        mål = config.BACKUP_RCLONE_REMOTE
        kommandoer = [
            ["rclone", "copy", config.BACKUP_DIR, mål, "--include", "elmaaler-*.db.gz"],
            # samme regler som lokalt: daglige i BACKUP_KEEP_DAYS dage, månedsbackups (den 1.) i 13 måneder
            ["rclone", "delete", mål, "--min-age", f"{config.BACKUP_KEEP_DAYS}d",
             "--filter", "- elmaaler-??????01-*", "--filter", "+ elmaaler-*.db.gz", "--filter", "- *"],
            ["rclone", "delete", mål, "--min-age", f"{MAANEDS_DAGE}d",
             "--filter", "+ elmaaler-??????01-*.db.gz", "--filter", "- *"],
        ]
        for k in kommandoer:
            r = subprocess.run(k + ["--retries", "3", "--low-level-retries", "5"],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout)
                                   else f"rclone afsluttede med kode {r.returncode}")
        SKY.update(sidst_ok=nu(), fejl="")
        db.log("backup", f"Backup kopieret til {mål}")
        return True
    except FileNotFoundError:
        fejl = "Programmet rclone findes ikke i containeren – byg imaget om (docker compose up -d --build)."
    except subprocess.TimeoutExpired:
        fejl = "Kopiering til skyen tog over 30 minutter og blev stoppet."
    except Exception as e:  # noqa: BLE001
        fejl = str(e)
    finally:
        SKY["koerer"] = False
        _sky_laas.release()
    SKY.update(sidst_fejl=nu(), fejl=fejl)
    db.log("fejl", f"Backup kunne ikke kopieres til {config.BACKUP_RCLONE_REMOTE}: {fejl}")
    try:
        from .jobs import _kontor_modtagere
        mailer.send(_kontor_modtagere("mail_offline"), "Elmålere: backup kunne ikke kopieres til OneDrive",
                    f"Nattens backup blev taget, men kunne ikke kopieres til {config.BACKUP_RCLONE_REMOTE}.\n\n"
                    f"Fejl: {fejl}\n\nTjek System › Database & backup. Har OneDrive-forbindelsen været ubrugt "
                    f"i lang tid, skal den måske logges ind igen (se installationsvejledningen).")
    except Exception:  # noqa: BLE001
        pass
    return False


def sky_status():
    """Seneste kopiering til skyen – fra hukommelsen eller hændelsesloggen (efter genstart)."""
    if not config.BACKUP_RCLONE_REMOTE:
        return None
    st = dict(SKY)
    if st["sidst_ok"] is None:
        with db.db() as c:
            r = c.execute("SELECT MAX(ts) FROM haendelser WHERE type='backup' AND tekst LIKE 'Backup kopieret til %'"
                          ).fetchone()
        st["sidst_ok"] = r[0]
    st["sidst_ok_tekst"] = fmt_tid(st["sidst_ok"]) if st["sidst_ok"] else "aldrig"
    st["maal"] = config.BACKUP_RCLONE_REMOTE
    return st


def liste():
    filer = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "elmaaler-*.db.gz")), reverse=True)
    return [{"navn": os.path.basename(f), "kb": os.path.getsize(f) // 1024,
             "ts": int(os.path.getmtime(f)), "maaned": _er_maanedsbackup(f)} for f in filer]
