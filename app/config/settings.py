from datetime import datetime
from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Cache busting: YYYYMMDDHHMMSS from boot (set in Docker entrypoint) or first load
BOOTSTAMP = os.environ.get("DJANGO_BOOTSTAMP") or datetime.now().strftime("%Y%m%d%H%M%S")

# App version (display in UI). Override via PING_TRAIL_VERSION env.
PING_TRAIL_VERSION = os.environ.get("PING_TRAIL_VERSION", "2.0")

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# The dev defaults below are PUBLISHED CONSTANTS — this source is public, so
# every reader knows them. They exist so `git clone && docker compose up` works
# with no setup, and they are fail-closed: `require_configured_secrets` refuses
# to let either of them boot a process with DEBUG off.
DEV_SECRET_KEY = "dev-only-change-me"
DEV_INGEST_TOKEN = "dev-ingest-token"

# Django signing key (sessions, CSRF, cookies). Env: DJANGO_SIGNING_KEY or DJANGO_SECRET_KEY.
SECRET_KEY = os.environ.get("DJANGO_SIGNING_KEY") or os.environ.get("DJANGO_SECRET_KEY") or DEV_SECRET_KEY

# Shared secret for POST /ingest/. The host prober sends X-Ping-Trail-Token.
# Measurement is decoupled from presentation (spec VANTAGE) — the ingest
# endpoint is the only write path from a prober.
INGEST_TOKEN = os.environ.get("PING_TRAIL_INGEST_TOKEN", DEV_INGEST_TOKEN)


def require_configured_secrets(debug, secret_key, ingest_token):
    """Refuse to start a non-DEBUG process still holding a dev default.

    Both defaults are readable in this file, so shipping either one with
    DEBUG off hands out a working attack:

    * `SECRET_KEY` signs sessions, CSRF tokens and cookies — a known key
      means anyone can forge all three.
    * `INGEST_TOKEN` is the ONLY authentication on `POST /ingest/` and
      `GET /control/<vantage>/` — a known token is an open write path into
      the measurement record and read access to the ladder.

    Fail at import, loudly, rather than serving a false sense of security.
    DEBUG=True keeps both defaults working so local dev needs no setup.
    """
    if debug:
        return
    if secret_key == DEV_SECRET_KEY:
        raise ImproperlyConfigured(
            "DJANGO_SIGNING_KEY is still the published dev default "
            f"({DEV_SECRET_KEY!r}) while DEBUG is off. Anyone reading this "
            "repo can forge sessions and CSRF tokens. Set DJANGO_SIGNING_KEY "
            "(or DJANGO_SECRET_KEY) to a real secret, or run with "
            "DJANGO_DEBUG=1 for local development."
        )
    if ingest_token == DEV_INGEST_TOKEN:
        raise ImproperlyConfigured(
            "PING_TRAIL_INGEST_TOKEN is still the published dev default "
            f"({DEV_INGEST_TOKEN!r}) while DEBUG is off. It is the only auth "
            "on POST /ingest/ and GET /control/<vantage>/, so a known value "
            "is an unauthenticated write path. Set PING_TRAIL_INGEST_TOKEN to "
            "a real secret, or run with DJANGO_DEBUG=1 for local development."
        )


require_configured_secrets(DEBUG, SECRET_KEY, INGEST_TOKEN)

# Where a host-side prober POSTs its sweeps. Used by `ping_sweep` as its default.
INGEST_URL = os.environ.get("PING_TRAIL_INGEST_URL", "http://localhost:8030/ingest/")

# Where a host-side prober PULLS its marching orders and the ladder. The vantage
# slug is appended by `ping_sweep`. This endpoint is what lets the prober hold no
# database configuration at all (spec-ping-trail-control-plane-and-rfc1918-classification).
CONTROL_URL = os.environ.get("PING_TRAIL_CONTROL_URL", "http://localhost:8030/control/")

# Optional hostnames from env (add to ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS)
_server1 = os.environ.get("SERVERNAME1", "").strip()
_server2 = os.environ.get("SERVERNAME2", "").strip()

_allowed = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,0.0.0.0").split(",") if h.strip()]
for _sn in (_server1, _server2):
    if _sn and _sn not in _allowed:
        _allowed.append(_sn)
ALLOWED_HOSTS = _allowed

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_ORIGINS", "").split(",") if o.strip()
]
for _sn in (_server1, _server2):
    if _sn:
        CSRF_TRUSTED_ORIGINS.extend([f"https://{_sn}", f"http://{_sn}"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "trail",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.bootstamp",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

_db_path = os.environ.get("DJANGO_DB_PATH")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(_db_path) if _db_path else (BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            # WAL + a real busy timeout: the ingest endpoint writes a tick every
            # interval while the dashboard's live regions read continuously, and
            # a reader must never block a tick out of the record.
            #
            # As of v2 the host prober does NOT open this file — it talks HTTP
            # only (see trail/management/commands/ping_sweep.py). If anything
            # ever needs DJANGO_DB_PATH on the host again, that is a regression.
            "init_command": "PRAGMA journal_mode=WAL;",
            "timeout": 20,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Toronto"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
# assets/ = source (images, js, css); collectstatic copies to static/
STATICFILES_DIRS = [BASE_DIR / "assets"]
STATIC_ROOT = BASE_DIR / "static"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"
