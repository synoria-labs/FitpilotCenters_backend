"""Deploy-level switches for the owner/admin WhatsApp agent."""
import os

from app.core.env import load_environment

load_environment()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class OwnerAgentEnv:
    # Absolute server kill-switch. DB config can only enable the agent when this is true.
    SERVER_ENABLED: bool = _as_bool(os.getenv("OWNER_AGENT_SERVER_ENABLED"), default=False)
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    DEFAULT_MODEL: str = os.getenv("OWNER_AGENT_MODEL", "claude-sonnet-4-6")
    RECURSION_LIMIT: int = int(os.getenv("OWNER_AGENT_RECURSION_LIMIT", "12"))

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.SERVER_ENABLED and cls.ANTHROPIC_API_KEY)


owner_agent_env = OwnerAgentEnv()
