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

    # WhatsApp Business Account (template management).
    # The ACCESS_TOKEN must carry the ``whatsapp_business_management`` permission
    # to create/edit/delete/list message templates on this WABA.
    BUSINESS_ACCOUNT_ID: str = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")

    # Inbound webhook
    WEBHOOK_VERIFY_TOKEN: str = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "")
    APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")
    # Owner used by Meta's Resumable Upload API to generate template header sample handles.
    # Usually the Meta app id; deployments may override it when Meta requires another owner.
    APP_ID: str = os.getenv("WHATSAPP_APP_ID", "")
    UPLOAD_OWNER_ID: str = os.getenv("WHATSAPP_UPLOAD_OWNER_ID", "")

    @classmethod
    def upload_owner_id(cls) -> str:
        return cls.UPLOAD_OWNER_ID or cls.APP_ID or cls.BUSINESS_ACCOUNT_ID

    @classmethod
    def graph_url(cls, path: str) -> str:
        """Build a full Graph API URL for ``path`` (no leading slash needed)."""
        return f"{cls.GRAPH_BASE.rstrip('/')}/{cls.API_VERSION}/{path.lstrip('/')}"

    @classmethod
    def is_send_configured(cls) -> bool:
        return bool(cls.PHONE_NUMBER_ID and cls.ACCESS_TOKEN)

    @classmethod
    def is_management_configured(cls) -> bool:
        """True when template management (Business Management API) can be called."""
        return bool(cls.BUSINESS_ACCOUNT_ID and cls.ACCESS_TOKEN)


whatsapp_config = WhatsAppConfig()
