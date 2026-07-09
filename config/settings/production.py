from .base import *  # noqa: F403
from urllib.parse import urlparse

if not SECRET_KEY or SECRET_KEY == "unsafe-local-development-key" or len(SECRET_KEY) < 50:  # noqa: F405
    raise RuntimeError("DJANGO_SECRET_KEY must be configured with at least 50 characters in production.")

if not ALLOWED_HOSTS or any(  # noqa: F405
    host in {"*", "localhost", "127.0.0.1", "0.0.0.0"} or "*" in host or host.startswith(".")
    for host in ALLOWED_HOSTS
):
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be restricted in production.")

if not CSRF_TRUSTED_ORIGINS:  # noqa: F405
    raise RuntimeError("DJANGO_CSRF_TRUSTED_ORIGINS must be configured in production.")

allowed_host_set = set(ALLOWED_HOSTS)  # noqa: F405
for origin in CSRF_TRUSTED_ORIGINS:  # noqa: F405
    parsed_origin = urlparse(origin)
    if parsed_origin.scheme != "https" or not parsed_origin.hostname or "*" in origin:
        raise RuntimeError("DJANGO_CSRF_TRUSTED_ORIGINS must use explicit https origins in production.")
    if parsed_origin.hostname not in allowed_host_set:
        raise RuntimeError("DJANGO_CSRF_TRUSTED_ORIGINS must match DJANGO_ALLOWED_HOSTS in production.")

if ADMIN_URL in {"admin/", "controle-interno/"}:  # noqa: F405
    raise RuntimeError("DJANGO_ADMIN_URL must be changed to a private, non-default path in production.")

DEBUG = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
