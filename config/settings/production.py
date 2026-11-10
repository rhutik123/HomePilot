"""
Production settings for HomePilot.
Strict security, HTTPS enforcement, and production logging.
"""
from django.core.exceptions import ImproperlyConfigured
from .base import *

DEBUG = False

# Validate mandatory production secret key
secret_key_env = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not secret_key_env or secret_key_env == "django-insecure-fallback-development-key-homepilot-2026-default":
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a secure, private value in production."
    )
SECRET_KEY = secret_key_env

raw_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = [h.strip() for h in raw_hosts.split(",") if h.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must be explicitly defined in production."
    )

# Validate database credentials if using PostgreSQL
if DB_ENGINE == "django.db.backends.postgresql":
    db_pass = os.getenv("DB_PASSWORD", "").strip()
    if not db_pass or db_pass == "postgres":
        raise ImproperlyConfigured(
            "DB_PASSWORD must be configured with a non-default password in production."
        )

# Security headers & SSL
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "True").lower() == "true"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "same-origin"

# Celery in production should not be eager
CELERY_TASK_ALWAYS_EAGER = False
