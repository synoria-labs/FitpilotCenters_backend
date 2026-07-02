import os
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from app.core.env import is_production


class SecurityFilter(logging.Filter):
    """Filter to remove sensitive information from logs while preserving context"""

    SENSITIVE_PATTERNS = [
        # JWT tokens (eyJ...)
        (r'eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*', '[JWT_TOKEN]'),
        # Bearer tokens
        (r'Bearer\s+[A-Za-z0-9._-]+', 'Bearer [TOKEN]'),
        # Password values
        (r'password["\s]*[:=]["\s]*[^,}\s]+', 'password: [HIDDEN]'),
        # Secret keys
        (r'secret["\s]*[:=]["\s]*[^,}\s]+', 'secret: [HIDDEN]'),
        # Person IDs in specific contexts (but allow "person_id" in error messages)
        (r'"person_id":\s*"?\d+"?', '"person_id": "[ID]"'),
        (r'person_id["\s]*[:=]["\s]*"?\d+"?', 'person_id: [ID]'),
        # Session tokens
        (r'session-id[a-f0-9]{32,}', 'session-id[SESSION]'),
    ]

    def filter(self, record):
        import re

        # Merge args into the message first so parameterised records (e.g.
        # SQLAlchemy's "%r" bound parameters, which arrive in record.args, not
        # record.msg) are also subject to redaction.
        if record.args:
            try:
                record.msg = record.getMessage()
                record.args = None
            except Exception:
                pass

        if hasattr(record, 'msg'):
            msg = str(record.msg)

            # Only apply pattern-based filtering, not keyword blanket censoring
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)

            record.msg = msg
        return True


def setup_logging():
    """Configure application logging based on environment variables"""

    # Environment configuration
    # Defaults are production-safe: quiet level, SQL echo off, redaction on.
    # Override with env vars for local diagnostics (e.g. LOG_LEVEL=DEBUG).
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    sql_log_level = os.getenv("SQL_LOG_LEVEL", "WARNING").upper()
    log_format = os.getenv("LOG_FORMAT", "text").lower()

    # Resolve default log file within backend/logs/app.log regardless of CWD
    default_log_path = Path(__file__).resolve().parents[2] / "logs" / "app.log"
    log_file_path = os.getenv("LOG_FILE_PATH", str(default_log_path))
    auth_log_events = os.getenv("AUTH_LOG_EVENTS", "true").lower() == "true"
    # Redaction is opt-out via env var, but always forced on in production so a
    # misconfigured deploy cannot log secrets/PII unredacted.
    enable_security_filter = (
        os.getenv("ENABLE_SECURITY_FILTER", "false").lower() == "true" or is_production()
    )

    # Create logs directory
    log_dir = Path(log_file_path).parent
    log_dir.mkdir(exist_ok=True)

    # Configure formatters
    if log_format == "json":
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
            '"module": "%(name)s", "message": "%(message)s", '
            '"line": %(lineno)d, "function": "%(funcName)s"}'
        )
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level, logging.INFO))
    console_handler.setFormatter(formatter)

    # Add security filter only if enabled (for production)
    if enable_security_filter:
        security_filter = SecurityFilter()
        console_handler.addFilter(security_filter)

    root_logger.addHandler(console_handler)

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(getattr(logging, log_level, logging.INFO))
    file_handler.setFormatter(formatter)

    # Add security filter to file handler only if enabled
    if enable_security_filter:
        file_handler.addFilter(security_filter)

    root_logger.addHandler(file_handler)

    # Configure SQLAlchemy logging
    sql_logger = logging.getLogger('sqlalchemy.engine')
    sql_logger.setLevel(getattr(logging, sql_log_level, logging.WARN))

    # Configure application loggers
    app_logger = logging.getLogger('app')
    app_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Auth logger (if enabled)
    if auth_log_events:
        auth_logger = logging.getLogger('app.auth')
        auth_logger.setLevel(logging.INFO)
    else:
        auth_logger = logging.getLogger('app.auth')
        auth_logger.setLevel(logging.ERROR)

    # Emit an initialization line (goes to console and file)
    try:
        root_logger.info(
            "Logging initialized level=%s sql=%s file=%s",
            log_level,
            sql_log_level,
            log_file_path,
        )
    except Exception:
        pass

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module name"""
    return logging.getLogger(f"app.{name}")


# Auth event logging helpers
def log_auth_event(event_type: str, username: Optional[str] = None,
                  session_id: Optional[str] = None, success: bool = True):
    """Log authentication events safely"""
    auth_logger = get_logger("auth")

    # Hash session_id for privacy
    session_hash = hash(session_id) % 10000 if session_id else "unknown"

    if success:
        auth_logger.info(
            f"Auth {event_type} successful - user: {username or 'unknown'} "
            f"session: #{session_hash}"
        )
    else:
        auth_logger.warning(
            f"Auth {event_type} failed - user: {username or 'unknown'} "
            f"session: #{session_hash}"
        )


def log_security_event(event_type: str, details: str, level: str = "WARNING"):
    """Log security-related events"""
    security_logger = get_logger("security")
    log_level = getattr(logging, level.upper(), logging.WARNING)
    security_logger.log(log_level, f"Security event: {event_type} - {details}")