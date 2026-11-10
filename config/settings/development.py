"""
Development settings for HomePilot.
Enables debug mode and allows localhost access.
"""
from .base import *

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# CORS settings for local development
CORS_ALLOW_ALL_ORIGINS = True
