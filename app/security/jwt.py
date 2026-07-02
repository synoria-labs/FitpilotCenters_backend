import os
from datetime import datetime, timedelta
from datetime import timezone
from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from zoneinfo import ZoneInfo

from app.core.env import load_environment
from app.core.logging_config import get_logger

# Make sure backend/.env is loaded before we read the secrets (systemd/container
# runtimes may also supply them directly, which takes precedence).
load_environment()

logger = get_logger("auth.jwt")

# Minimum secret strength. HS256 is symmetric, so a weak/known key lets anyone
# forge tokens for any person_id/capabilities.
_MIN_SECRET_BYTES = 32


def _require_secret(env_name: str) -> str:
    """Read a mandatory JWT secret from the environment, failing fast if weak.

    There is deliberately NO fallback default: a missing or trivial secret must
    abort startup instead of silently running in an insecure mode.
    """
    value = os.getenv(env_name)
    if not value or len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
        raise RuntimeError(
            f"{env_name} must be set to a strong secret of at least "
            f"{_MIN_SECRET_BYTES} bytes. Refusing to start with a missing or weak JWT secret."
        )
    return value


SECRET_KEY_ACCESS_TOKEN = _require_secret("SECRET_KEY_ACCESS_TOKEN")
SECRET_KEY_REFRESH_TOKEN = _require_secret("SECRET_KEY_REFRESH_TOKEN")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 5))  # default 5 minutes
ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("ACCESS_TOKEN_EXPIRE_DAYS", 30))

# Security configuration helper
def get_cookie_secure_setting():
    """Return secure cookie setting based on environment"""
    return os.getenv("ENVIRONMENT", "development") == "production"

def get_cookie_samesite_setting():
    """Return samesite cookie setting based on environment"""
    env = os.getenv("ENVIRONMENT", "development")
    if env == "production":
        return "strict"  # O "lax" si usas subdominios
    else:
        return "lax"  # Para dev: permite cross-port en localhost

def get_access_cookie_max_age_seconds() -> int:
    """Return cookie Max-Age in seconds for access token."""
    return ACCESS_TOKEN_EXPIRE_MINUTES * 60

def get_refresh_cookie_max_age_seconds() -> int:
    """Return cookie Max-Age in seconds for refresh token."""
    return ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

def create_refresh_token(data: dict, expires_delta: timedelta = None):
    Mexico_City = ZoneInfo("America/Mexico_City")
    # now = datetime().now()

    to_encode = data.copy()
    expire = datetime.now(Mexico_City) + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    logger.debug(f"Refresh token expires at: {expire}")
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY_REFRESH_TOKEN, algorithm=ALGORITHM)

def create_access_token(data: dict, expires_delta: timedelta = None):
    Mexico_City = ZoneInfo("America/Mexico_City")
    logger.debug(f"Using timezone: {Mexico_City}")
    to_encode = data.copy()
    expire = datetime.now(Mexico_City) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    logger.debug(f"Access token expires at: {expire}")
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY_ACCESS_TOKEN, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY_ACCESS_TOKEN, algorithms=[ALGORITHM])
    except JWTError as e:
        logger.warning(f"Error verifying access token: {e}")
        return None
    
def verify_refresh_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY_REFRESH_TOKEN, algorithms=[ALGORITHM])
    except JWTError as e:
        logger.warning(f"Error verifying refresh token: {e}")
        return None
