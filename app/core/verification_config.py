"""Configuration for the shared FitPilot verification microservice.

The gym backend is a *client* of the standalone `fitpilot-verification` service
(Twilio Verify + Resend + proof issuance). Step-up verification is gated behind
``STEP_UP_ENABLED`` so this can ship before the service is live without changing
existing behavior.
"""
import os

from app.core.env import load_environment

load_environment()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class VerificationConfig:
    # Master switch. When False, step-up checks are a no-op (no behavior change).
    ENABLED: bool = _as_bool(os.getenv("STEP_UP_ENABLED"), default=False)
    SERVICE_URL: str = (os.getenv("VERIFICATION_SERVICE_URL", "") or "").strip().rstrip("/")
    SERVICE_TOKEN: str = (os.getenv("VERIFICATION_SERVICE_TOKEN", "") or "").strip()
    # Identifies this consumer to the service; proofs are bound to this audience.
    AUDIENCE: str = (os.getenv("VERIFICATION_AUDIENCE", "gym") or "gym").strip()
    TIMEOUT_SECONDS: float = float(os.getenv("VERIFICATION_TIMEOUT_SECONDS", "10"))
    # Comma-separated list of channels the gym is allowed to request.
    # MVP defaults to email only because the project has no SMS budget.
    ALLOWED_CHANNELS: tuple = tuple(
        c.strip().lower()
        for c in (os.getenv("STEP_UP_ALLOWED_CHANNELS", "email") or "email").split(",")
        if c.strip()
    )

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.SERVICE_URL and cls.SERVICE_TOKEN)

    @classmethod
    def channel_allowed(cls, channel: str) -> bool:
        return (channel or "").strip().lower() in cls.ALLOWED_CHANNELS


def step_up_enabled() -> bool:
    """Whether step-up verification is active (enabled and configured)."""
    return VerificationConfig.ENABLED and VerificationConfig.is_configured()
