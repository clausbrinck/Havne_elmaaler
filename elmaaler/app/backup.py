"""Backup af databasen: konsistent kopi via SQLites backup-API, gzip og oprydning."""
import glob
import gzip
import os
import shutil
import sqlite3

from . import config, db
from .tid import nu, lokal


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

    graense = nu() - config.BACKUP_KEEP_DAYS * 86400
    for mappe in mapper:
        for f in glob.glob(os.path.join(mappe, "elmaaler-*.db.gz")):
            if os.path.getmtime(f) < graense:
                os.remove(f)

    db.log("backup", f"Backup ({aarsag}): {os.path.basename(ud)} – {os.path.getsize(ud) // 1024} kB")
    return ud


def liste():
    filer = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "elmaaler-*.db.gz")), reverse=True)
    return [{"navn": os.path.basename(f), "kb": os.path.getsize(f) // 1024,
             "ts": int(os.path.getmtime(f))} for f in filer]
