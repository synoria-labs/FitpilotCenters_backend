"""Configuration for the unified WhatsApp outbound coordination layer.

Operational policy knobs (frequency cap, quiet hours, human-takeover window, opt-out keywords)
read from the environment with safe defaults. Mirrors ``chatbot_env`` / ``mercadopago_config``.
A future iteration can promote these to a single-row DB config edited from the frontend.
"""
import os
from typing import Set

from app.core.env import load_environment

load_environment()


def _csv_set(raw: str, default: Set[str]) -> Set[str]:
    items = {p.strip().upper() for p in (raw or "").split(",") if p.strip()}
    return items or set(default)


class OutboundConfig:
    # Max MARKETING messages per contact per local day (transactional are exempt).
    MARKETING_DAILY_CAP: int = int(os.getenv("MARKETING_DAILY_CAP", "1"))
    # Quiet hours for MARKETING (local hours, [start, end) overnight allowed if start > end).
    QUIET_HOURS_START: int = int(os.getenv("QUIET_HOURS_START", "21"))
    QUIET_HOURS_END: int = int(os.getenv("QUIET_HOURS_END", "9"))
    QUIET_HOURS_TZ: str = os.getenv("QUIET_HOURS_TZ", "America/Mexico_City")
    # How long the bot stays paused for a conversation after a human replies in Chats.
    HUMAN_TAKEOVER_HOURS: int = int(os.getenv("HUMAN_TAKEOVER_HOURS", "3"))
    # Inbound keyword sets (exact whole-message token match, case/accent-insensitive).
    OPTOUT_KEYWORDS: Set[str] = _csv_set(
        os.getenv("OPTOUT_KEYWORDS", ""), {"STOP", "BAJA", "CANCELAR"}
    )
    OPTIN_KEYWORDS: Set[str] = _csv_set(
        os.getenv("OPTIN_KEYWORDS", ""), {"ALTA", "START"}
    )


outbound_config = OutboundConfig()
