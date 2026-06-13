"""Deploy-level configuration for the LangChain WhatsApp chatbot agent.

These are the *server* knobs that must come from the environment (the Anthropic
API key, the master kill-switch, defaults). The *operational* configuration the
business edits at runtime — system prompt, business info, per-conversation
enable toggle, model — lives in the ``app.chatbot_config`` table and is editable
from the desktop frontend. Precedence: ``CHATBOT_ENABLED`` here is a deploy-level
kill-switch; even when it is true, the agent only replies when the DB row's
``enabled`` flag is also true.

The Anthropic API key is read by ``ChatAnthropic`` directly from
``ANTHROPIC_API_KEY`` in the environment — it is never hardcoded.
"""
import os

from app.core.env import load_environment

load_environment()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ChatbotEnv:
    # Anthropic credentials (consumed by ChatAnthropic via the environment).
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Default model id; the DB config row can override per deployment.
    MODEL: str = os.getenv("CHATBOT_MODEL", "claude-sonnet-4-6")
    # Deploy-level kill-switch (the DB row's `enabled` is the operational toggle).
    ENABLED: bool = _as_bool(os.getenv("CHATBOT_ENABLED"), default=False)
    # Upper bound on a single reply's length.
    MAX_TOKENS: int = int(os.getenv("CHATBOT_MAX_TOKENS", "1024"))
    # How many prior conversation messages to feed the agent as history.
    HISTORY_LIMIT: int = int(os.getenv("CHATBOT_HISTORY_LIMIT", "20"))

    @classmethod
    def is_configured(cls) -> bool:
        """True when the agent has the credentials it needs to run at all."""
        return bool(cls.ANTHROPIC_API_KEY)


chatbot_env = ChatbotEnv()
