"""Configuration for the WhatsApp Cloud API integration."""
import os

from app.core.env import load_environment

load_environment()


class WhatsAppConfig:
    # Outbound / Graph API
    PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    ACCESS_TOKEN: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    GRAPH_BASE: str = os.getenv("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com")

    # Inbound webhook
    WEBHOOK_VERIFY_TOKEN: str = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "")
    APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")

    @classmethod
    def graph_url(cls, path: str) -> str:
        """Build a full Graph API URL for ``path`` (no leading slash needed)."""
        return f"{cls.GRAPH_BASE.rstrip('/')}/{cls.API_VERSION}/{path.lstrip('/')}"

    @classmethod
    def is_send_configured(cls) -> bool:
        return bool(cls.PHONE_NUMBER_ID and cls.ACCESS_TOKEN)


whatsapp_config = WhatsAppConfig()
