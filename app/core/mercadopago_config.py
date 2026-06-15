"""Configuration for the MercadoPago (Checkout Pro) payment gateway."""
import os

from app.core.env import load_environment

load_environment()


class MercadoPagoConfig:
    ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    PUBLIC_KEY: str = os.getenv("MP_PUBLIC_KEY", "")
    SECRET_KEY: str = os.getenv("MP_SECRET_KEY", "")
    WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "")
    NOTIFICATION_URL: str = os.getenv(
        "MP_NOTIFICATION_URL", "https://webhook.fitpilot.fit/webhook/mercadopago"
    )
    API_BASE: str = os.getenv("MP_API_BASE", "https://api.mercadopago.com")
    CURRENCY: str = os.getenv("MP_CURRENCY", "MXN")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.ACCESS_TOKEN)

    @classmethod
    def is_test(cls) -> bool:
        return cls.ACCESS_TOKEN.startswith("TEST-")


mercadopago_config = MercadoPagoConfig()
