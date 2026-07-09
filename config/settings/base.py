from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BASE_DIR

load_dotenv(PROJECT_ROOT / ".env")


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


SECRET_KEY = env("DJANGO_SECRET_KEY", "unsafe-local-development-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
ADMIN_URL = env("DJANGO_ADMIN_URL", "controle-interno/")
TIME_ZONE = env("DJANGO_TIME_ZONE", "America/Fortaleza")

if not ADMIN_URL.endswith("/"):
    ADMIN_URL += "/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "apps.core",
    "apps.accounts",
    "apps.counterparties",
    "apps.documents",
    "apps.payments",
    "apps.banking",
    "apps.exports",
    "apps.telegrambot",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "axes.middleware.AxesMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_ROOT / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "inplant"),
        "USER": env("POSTGRES_USER", "inplant"),
        "PASSWORD": env("POSTGRES_PASSWORD", "change-me"),
        "HOST": env("POSTGRES_HOST", "db"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeYesilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_REDIRECT_URL = "/interno/dashboard/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

LANGUAGE_CODE = "pt-br"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "staticfiles"
STATICFILES_DIRS = [PROJECT_ROOT / "static"]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = PROJECT_ROOT / "storage" / "media"
PRIVATE_STORAGE_ROOT = PROJECT_ROOT / "storage" / "private"
EXPORTS_ROOT = PROJECT_ROOT / "storage" / "exports"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
FILE_UPLOAD_PERMISSIONS = 0o640
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_FILE_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)

AXES_ENABLED = env_bool("AXES_ENABLED", True)
AXES_FAILURE_LIMIT = env_int("AXES_FAILURE_LIMIT", 5)
AXES_COOLOFF_TIME = env_int("AXES_COOLOFF_TIME", 1)
AXES_LOCKOUT_TEMPLATE = None
AXES_RESET_ON_SUCCESS = True

CELERY_BROKER_URL = env("CELERY_BROKER_URL", env("REDIS_URL", "redis://redis:6379/0"))
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
CELERY_TIMEZONE = TIME_ZONE

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_IDS = env_list("TELEGRAM_ALLOWED_USER_IDS")

OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_REQUEST_TIMEOUT_SECONDS = env_float("OPENAI_REQUEST_TIMEOUT_SECONDS", 8.0)
OPENAI_DRAFT_REARRANGE_ENABLED = env_bool("OPENAI_DRAFT_REARRANGE_ENABLED", True)
OPENAI_OFX_CLASSIFICATION_ENABLED = env_bool("OPENAI_OFX_CLASSIFICATION_ENABLED", False)

DEFAULT_PAYER = env("DEFAULT_PAYER", "Company")
DEFAULT_BANK_ACCOUNT = env("DEFAULT_BANK_ACCOUNT", "Main Account")

LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "sanitize_sensitive": {
            "()": "apps.core.log_safety.SafeLogFilter",
        },
    },
    "formatters": {
        "safe": {
            "()": "apps.core.log_safety.SafeFormatter",
            "format": "%(levelname)s %(asctime)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "safe",
            "filters": ["sanitize_sensitive"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "telegram": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "openai": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "httpx": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

PAYMENT_ACCOUNTING_TEMPLATE = PROJECT_ROOT / "files" / "modelo_exportacao.xlsx"
PAYMENT_IMPORT_TEMPLATE = PROJECT_ROOT / "files" / "modelo_importacao.xlsx"
SUPPLIER_IMPORT_TEMPLATE = PROJECT_ROOT / "otherfiles" / "FORNECEDORES (1).xlsx"
SUPPLIERS_NORMALIZED_FILE = PROJECT_ROOT / "otherfiles" / "vendors.xlsx"
WORKERS_NORMALIZED_FILE = PROJECT_ROOT / "otherfiles" / "workers.xlsx"
PAYMENTS_HISTORY_DIR = PROJECT_ROOT / "files" / "payments"
BUDGET_WORKBOOKS = [
    PROJECT_ROOT / "files" / "Orçamento Sintético - Sertãozinho.xlsx",
    PROJECT_ROOT / "files" / "Orçamento Sintético - Jaurez Távora.xlsx",
]
