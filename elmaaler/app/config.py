"""Konfiguration – læses fra miljøvariabler (.env via docker compose)."""
import os
import secrets
from zoneinfo import ZoneInfo


def _env(key, default=None):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def _bool(key, default=False):
    v = _env(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "ja", "yes", "on")


DATA_DIR = _env("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "elmaaler.db")
TZ = ZoneInfo(_env("TZ", "Europe/Copenhagen"))

KLUB_NAVN = _env("KLUB_NAVN", "Aabenraa Sejl Club")
BASE_URL = _env("BASE_URL", "http://localhost:8000").rstrip("/")
PORT = int(_env("PORT", "8000"))

# Målere
METER_COUNT = int(_env("METER_COUNT", "200"))

# Datakilde: "homeassistant" (normal drift) eller "sim" (simulator til test)
SOURCE = _env("SOURCE", "homeassistant")
HA_URL = _env("HA_URL", "http://homeassistant:8123").rstrip("/")
HA_TOKEN = _env("HA_TOKEN", "")
HA_ENERGY_ENTITY = _env("HA_ENERGY_ENTITY", "sensor.elmaaler_{nr:03d}_energy")
HA_POWER_ENTITY = _env("HA_POWER_ENTITY", "sensor.elmaaler_{nr:03d}_power")
HA_LQI_ENTITY = _env("HA_LQI_ENTITY", "sensor.elmaaler_{nr:03d}_linkquality")
HA_BRIDGE_ENTITY = _env("HA_BRIDGE_ENTITY", "binary_sensor.zigbee2mqtt_bridge_connection_state")
HA_VERIFY_TLS = _bool("HA_VERIFY_TLS", True)

POLL_SECONDS = int(_env("POLL_SECONDS", "30"))
STORE_MINUTES = int(_env("STORE_MINUTES", "15"))   # aflæsning gemmes hvert N. minut

# Email (SMTP)
SMTP_HOST = _env("SMTP_HOST", "")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER", "")
SMTP_PASSWORD = _env("SMTP_PASSWORD", "")
SMTP_FROM = _env("SMTP_FROM", SMTP_USER or "elmaaler@localhost")
SMTP_SECURITY = _env("SMTP_SECURITY", "starttls")   # starttls | ssl | none

# Første havnekontor-bruger (oprettes kun hvis der ingen kontor-brugere er)
ADMIN_EMAIL = _env("ADMIN_EMAIL", "")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")

# Backup
BACKUP_DIR = _env("BACKUP_DIR", os.path.join(DATA_DIR, "backup"))
BACKUP_EXTRA_DIR = _env("BACKUP_EXTRA_DIR", "")      # fx /backup-usb eller NAS-mount
BACKUP_KEEP_DAYS = int(_env("BACKUP_KEEP_DAYS", "30"))
BACKUP_TIME = _env("BACKUP_TIME", "03:00")
BACKUP_RCLONE_REMOTE = _env("BACKUP_RCLONE_REMOTE", "")   # fx "krypt:" = krypteret OneDrive-mappe (rclone)

# Cookies: sæt til true når siden kører bag HTTPS
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

# Simulator
SIM_BACKFILL_DAYS = int(_env("SIM_BACKFILL_DAYS", "0"))


def secret_key():
    key = _env("SECRET_KEY")
    if key:
        return key
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key
