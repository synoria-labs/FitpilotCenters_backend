"""In-process rate limiting for the login mutation.

Tracks recent failed attempts keyed by ``(ip, identifier)`` and enforces a
lockout window once a threshold is exceeded. This is per-process (per worker);
a shared store (e.g. Redis) is the production-grade upgrade for multi-worker
deployments — see the optimization plan, Fase 4.
"""
import os
import time
import threading
from collections import defaultdict, deque

_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
_WINDOW_SECONDS = int(os.getenv("LOGIN_ATTEMPT_WINDOW_SECONDS", "300"))
_LOCKOUT_SECONDS = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "300"))

_lock = threading.Lock()
_failures: "defaultdict[tuple, deque]" = defaultdict(deque)  # key -> timestamps
_locked_until: "dict[tuple, float]" = {}                     # key -> monotonic ts


def _key(ip, identifier) -> tuple:
    return (ip or "?", (identifier or "?").strip().lower())


def check_allowed(ip, identifier):
    """Return ``(allowed, retry_after_seconds)`` without recording anything."""
    key = _key(ip, identifier)
    now = time.monotonic()
    with _lock:
        until = _locked_until.get(key)
        if until is not None:
            remaining = until - now
            if remaining > 0:
                return False, int(remaining) + 1
            # Lockout expired: clear state.
            _locked_until.pop(key, None)
            _failures.pop(key, None)
    return True, 0


def record_failure(ip, identifier) -> None:
    """Record a failed attempt; may trigger a lockout for the key."""
    key = _key(ip, identifier)
    now = time.monotonic()
    with _lock:
        dq = _failures[key]
        dq.append(now)
        cutoff = now - _WINDOW_SECONDS
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _MAX_ATTEMPTS:
            _locked_until[key] = now + _LOCKOUT_SECONDS
            dq.clear()


def record_success(ip, identifier) -> None:
    """Clear failure/lockout state for the key after a successful login."""
    key = _key(ip, identifier)
    with _lock:
        _failures.pop(key, None)
        _locked_until.pop(key, None)
