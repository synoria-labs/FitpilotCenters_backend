"""Regression tests for the Fase 1 security-hardening changes.

These tests deliberately avoid the database so they run without a provisioned
Postgres (they do not use the ``db`` fixture).
"""
import importlib
import logging
import os
import subprocess
import sys

import pytest


# --------------------------------------------------------------------------- #
# env.is_production
# --------------------------------------------------------------------------- #
def test_is_production_toggles(monkeypatch):
    from app.core.env import is_production

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert is_production() is True
    monkeypatch.setenv("ENVIRONMENT", "PRODUCTION")  # case-insensitive
    assert is_production() is True
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert is_production() is False
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert is_production() is False  # defaults to non-production


# --------------------------------------------------------------------------- #
# Login rate limiter
# --------------------------------------------------------------------------- #
def test_login_rate_limit_locks_out_and_resets(monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    import app.security.login_rate_limit as rl
    importlib.reload(rl)

    ip, ident = "203.0.113.7", "Admin@Gym.com"
    for _ in range(2):
        allowed, _ = rl.check_allowed(ip, ident)
        assert allowed
        rl.record_failure(ip, ident)

    rl.record_failure(ip, ident)  # third failure crosses the threshold
    allowed, retry_after = rl.check_allowed(ip, ident)
    assert not allowed and retry_after > 0

    # identifier match is case-insensitive
    allowed_ci, _ = rl.check_allowed(ip, "admin@gym.com")
    assert not allowed_ci

    # a successful login clears the lockout
    rl.record_success(ip, ident)
    allowed_after, _ = rl.check_allowed(ip, ident)
    assert allowed_after


# --------------------------------------------------------------------------- #
# Password hashing / timing-equalisation helper
# --------------------------------------------------------------------------- #
def test_password_roundtrip_and_dummy_verify():
    from app.security.hashing import hash_password, verify_password, dummy_verify

    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h) is True
    assert verify_password("wrong", h) is False
    dummy_verify()  # must never raise, even though there is no real account


# --------------------------------------------------------------------------- #
# Log redaction now also covers record.args (SQLAlchemy-style bound params)
# --------------------------------------------------------------------------- #
def test_security_filter_redacts_args():
    from app.core.logging_config import SecurityFilter

    f = SecurityFilter()
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=%s",
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",), None,
    )
    f.filter(rec)
    assert "[JWT_TOKEN]" in rec.getMessage()
    assert rec.args in (None, ())  # args merged into msg


# --------------------------------------------------------------------------- #
# JWT fail-fast (needs python-jose; runs in a subprocess for a clean import)
# --------------------------------------------------------------------------- #
_HAS_JOSE = importlib.util.find_spec("jose") is not None
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_import_jwt(env_extra):
    env = dict(os.environ)
    for k in ("SECRET_KEY_ACCESS_TOKEN", "SECRET_KEY_REFRESH_TOKEN"):
        env.pop(k, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "import app.security.jwt"],
        cwd=_BACKEND_ROOT, env=env, capture_output=True, text=True,
    )


@pytest.mark.skipif(not _HAS_JOSE, reason="python-jose not installed")
def test_jwt_fails_fast_without_secrets():
    result = _run_import_jwt({})
    assert result.returncode != 0
    assert "SECRET_KEY_ACCESS_TOKEN" in result.stderr


@pytest.mark.skipif(not _HAS_JOSE, reason="python-jose not installed")
def test_jwt_fails_fast_on_weak_secret():
    result = _run_import_jwt(
        {"SECRET_KEY_ACCESS_TOKEN": "short", "SECRET_KEY_REFRESH_TOKEN": "x" * 32}
    )
    assert result.returncode != 0


@pytest.mark.skipif(not _HAS_JOSE, reason="python-jose not installed")
def test_jwt_imports_with_strong_secrets():
    result = _run_import_jwt(
        {"SECRET_KEY_ACCESS_TOKEN": "a" * 40, "SECRET_KEY_REFRESH_TOKEN": "b" * 40}
    )
    assert result.returncode == 0, result.stderr
