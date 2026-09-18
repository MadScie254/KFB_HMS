from pathlib import Path
import os
from urllib.parse import urlparse, unquote


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
TIME_ZONE = os.getenv("KFB_TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

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
SECURE_SSL_REDIRECT = os.getenv("KFB_SECURE_SSL_REDIRECT", "0") == "1"
SESSION_COOKIE_SECURE = os.getenv("KFB_SESSION_COOKIE_SECURE", "0") == "1"
CSRF_COOKIE_SECURE = os.getenv("KFB_CSRF_COOKIE_SECURE", "0") == "1"
SECURE_HSTS_SECONDS = int(os.getenv("KFB_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("KFB_SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
SECURE_HSTS_PRELOAD = os.getenv("KFB_SECURE_HSTS_PRELOAD", "0") == "1"

HOSPITAL_NAME = os.getenv("KFB_HOSPITAL_NAME", "Kingdom Faith Based Hospital")
MPESA_MODE = os.getenv("KFB_MPESA_MODE", "manual")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}
