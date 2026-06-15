"""GraphQL types for the chatbot configuration.

Powers the desktop "Chatbot" admin tab: a single ``ChatbotConfigType`` (system prompt +
business info + toggles + model) that the business edits at runtime.
"""
from typing import Optional

import strawberry

from app.crud.chatbotConfigCrud import ChatbotConfigData


@strawberry.type
class ChatbotConfigType:
    id: Optional[int]
    enabled: bool
    require_confirmation: bool
    require_mp_payment: bool
    model: str
    system_prompt: Optional[str]
    business_name: Optional[str]
    address: Optional[str]
    operating_hours: Optional[str]
    phone: Optional[str]
    policies: Optional[str]
    tone: Optional[str]
    extra_info: Optional[str]

    @classmethod
    def from_data(cls, data: ChatbotConfigData) -> "ChatbotConfigType":
        return cls(
            id=data.id,
            enabled=bool(data.enabled),
            require_confirmation=bool(data.require_confirmation),
            require_mp_payment=bool(data.require_mp_payment),
            model=data.model,
            system_prompt=data.system_prompt,
            business_name=data.business_name,
            address=data.address,
            operating_hours=data.operating_hours,
            phone=data.phone,
            policies=data.policies,
            tone=data.tone,
            extra_info=data.extra_info,
        )


@strawberry.input
class SaveChatbotConfigInput:
    enabled: Optional[bool] = None
    require_confirmation: Optional[bool] = None
    require_mp_payment: Optional[bool] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    business_name: Optional[str] = None
    address: Optional[str] = None
    operating_hours: Optional[str] = None
    phone: Optional[str] = None
    policies: Optional[str] = None
    tone: Optional[str] = None
    extra_info: Optional[str] = None


@strawberry.type
class ChatbotConfigResult:
    success: bool = False
    config: Optional[ChatbotConfigType] = None
    error: Optional[str] = None
