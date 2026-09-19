import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
ENVIRONMENT = os.getenv("KFB_ENV", "demo").lower()
DEMO_MODE = ENVIRONMENT == "demo"

SECRET_KEY = os.getenv("KFB_SECRET_KEY", "")
if not SECRET_KEY:
    if DEMO_MODE:
        SECRET_KEY = "demo-only-kfb-hms-key-not-for-production"
    else:
        raise RuntimeError("KFB_SECRET_KEY is required outside demo mode")

DEBUG = os.getenv("KFB_DEBUG", "1" if DEMO_MODE else "0") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("KFB_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [u.strip() for u in os.getenv("KFB_CSRF_TRUSTED_ORIGINS", "").split(",") if u.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "hospital.apps.HospitalConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Django does not serve static files when DEBUG is off, and the supplied
    # Caddy configuration reverse-proxies every path to the application. Without
    # this the production deployment answers 404 for its own stylesheet and
    # script: a hospital system rendered as unstyled HTML with no JavaScript.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "hospital.middleware.ScreenLockMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "hospital.middleware.AuditRequestMiddleware",
]

ROOT_URLCONF = "kfb_hms.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "hospital.context_processors.application_context",
            ],
        },
    },
]

WSGI_APPLICATION = "kfb_hms.wsgi.application"
ASGI_APPLICATION = "kfb_hms.asgi.application"


def database_config():
    url = os.getenv("KFB_DATABASE_URL", "").strip()
    if not url:
        if not DEMO_MODE:
            raise RuntimeError("KFB_DATABASE_URL is required outside demo mode")
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "demo.sqlite3", "TIMEOUT": 20}
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("KFB_DATABASE_URL must use PostgreSQL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60,
        # Persistent connections without a health check hand the next request a
        # socket the database has already closed — an InterfaceError that looks
        # random and happens under exactly the load you cannot reproduce.
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    }


DATABASES = {"default": database_config()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
# An owner scanning a column of figures reads 1,540,200.00 and 1540200.00 very
# differently. Django applies this to every rendered number; form inputs stay
# unlocalised, so typing and parsing are unaffected.
USE_THOUSAND_SEPARATOR = True
TIME_ZONE = os.getenv("KFB_TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # Hashed filenames let the browser cache assets indefinitely and still
        # pick up a change the moment one is deployed.
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEMO_MODE
        else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
# A missing manifest entry falls back to the plain name rather than raising in
# the middle of rendering a page. collectstatic is still a deployment step; the
# readiness check reports when it has not been run.
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
LOG_ROOT = Path(os.getenv("KFB_LOG_ROOT", BASE_DIR / "logs"))
try:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_TO_FILE = os.access(LOG_ROOT, os.W_OK)
except OSError:
    # A read-only or unwritable log path must not stop the hospital starting.
    # Logging falls back to the console, which a container platform collects.
    LOG_TO_FILE = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


def flag(name, *, secure_default):
    """Read a boolean switch that is ON by default once out of demo mode.

    These defaulted to off everywhere, so a production deployment that simply
    forgot four environment variables served its session and CSRF cookies over
    plain HTTP with no HSTS — patient data and a live session on the wire. The
    safe value is the default outside demo; a site that genuinely terminates
    TLS elsewhere can still opt out explicitly.
    """
    raw = os.getenv(name)
    if raw is None:
        return secure_default and not DEMO_MODE
    return raw == "1"


SECURE_SSL_REDIRECT = flag("KFB_SECURE_SSL_REDIRECT", secure_default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = flag("KFB_SESSION_COOKIE_SECURE", secure_default=True)
CSRF_COOKIE_SECURE = flag("KFB_CSRF_COOKIE_SECURE", secure_default=True)
SECURE_HSTS_SECONDS = int(os.getenv("KFB_SECURE_HSTS_SECONDS", "0" if DEMO_MODE else str(60 * 60 * 24 * 365)))
SECURE_HSTS_INCLUDE_SUBDOMAINS = flag("KFB_SECURE_HSTS_INCLUDE_SUBDOMAINS", secure_default=True)
SECURE_HSTS_PRELOAD = flag("KFB_SECURE_HSTS_PRELOAD", secure_default=False)

# A secret that is short, repetitive or still the Django placeholder is not a
# secret. Outside demo mode this is a startup failure, not a deploy-check warning
# nobody read.
if not DEMO_MODE:
    if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-") or len(set(SECRET_KEY)) < 5:
        raise RuntimeError(
            "KFB_SECRET_KEY must be at least 50 characters of unpredictable text outside demo mode."
        )
    if DEBUG:
        raise RuntimeError("KFB_DEBUG must not be enabled outside demo mode; it leaks settings and stack traces.")
    if not CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS if host not in {"*", ""}]

# Uploads are photographs of invoices and clinical scans. Bounding them in the
# handler stops a large file from being buffered whole before a form ever sees it.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("KFB_DATA_UPLOAD_MAX_MEMORY_SIZE", str(12 * 1024 * 1024)))
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
FILE_UPLOAD_PERMISSIONS = 0o640

# HSTS preload (security.W021) is deliberately not enabled by default. Preloading
# submits the domain to a list baked into browsers, which is slow and awkward to
# reverse, and the documented deployment is an internal hostname behind Caddy's
# own certificate authority — a name that cannot be preloaded at all. A hospital
# publishing on a real public domain can turn it on with
# KFB_SECURE_HSTS_PRELOAD=1 once it is certain every subdomain is HTTPS-only.
SILENCED_SYSTEM_CHECKS = [] if SECURE_HSTS_PRELOAD else ["security.W021"]

HOSPITAL_NAME = os.getenv("KFB_HOSPITAL_NAME", "Kingdom Faith Based Hospital")
MPESA_MODE = os.getenv("KFB_MPESA_MODE", "manual")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
        "json": {"()": "hospital.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "json_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_ROOT / "kfb-hms.jsonl",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "encoding": "utf-8",
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console", "json_file"] if LOG_TO_FILE else ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["console", "json_file"] if LOG_TO_FILE else ["console"],
            "level": "WARNING",
            "propagate": False,
        }
    },
}
if not LOG_TO_FILE:
    LOGGING["handlers"].pop("json_file")
