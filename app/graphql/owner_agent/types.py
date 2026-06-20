"""GraphQL types for the owner/admin WhatsApp agent."""
from __future__ import annotations

from typing import Optional

import strawberry

from app.crud.ownerAgentCrud import (
    OwnerAgentAuthorizedPhoneData,
    OwnerAgentConfigData,
)


@strawberry.type
class OwnerAgentConfigType:
    id: Optional[int]
    enabled: bool
    require_confirmation: bool
    model: str
    system_prompt: Optional[str]
    history_limit: int
    max_tokens: int
    server_enabled: bool

    @classmethod
    def from_data(cls, data: OwnerAgentConfigData, *, server_enabled: bool) -> "OwnerAgentConfigType":
        return cls(
            id=data.id,
            enabled=bool(data.enabled),
            require_confirmation=bool(data.require_confirmation),
            model=data.model,
            system_prompt=data.system_prompt,
            history_limit=int(data.history_limit),
            max_tokens=int(data.max_tokens),
            server_enabled=bool(server_enabled),
        )


@strawberry.type
class OwnerAgentAuthorizedPhoneType:
    id: int
    label: str
    phone_number: str
    normalized_wa_id: str
    enabled: bool
    created_by: Optional[int]

    @classmethod
    def from_data(cls, data: OwnerAgentAuthorizedPhoneData) -> "OwnerAgentAuthorizedPhoneType":
        return cls(
            id=data.id,
            label=data.label,
            phone_number=data.phone_number,
            normalized_wa_id=data.normalized_wa_id,
            enabled=bool(data.enabled),
            created_by=data.created_by,
        )


@strawberry.input
class SaveOwnerAgentConfigInput:
    enabled: Optional[bool] = None
    require_confirmation: Optional[bool] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    history_limit: Optional[int] = None
    max_tokens: Optional[int] = None


@strawberry.input
class AddOwnerAgentAuthorizedPhoneInput:
    label: str
    phone_number: str
    enabled: bool = True


@strawberry.input
class UpdateOwnerAgentAuthorizedPhoneInput:
    phone_id: int
    label: Optional[str] = None
    phone_number: Optional[str] = None
    enabled: Optional[bool] = None


@strawberry.type
class OwnerAgentConfigResult:
    success: bool = False
    config: Optional[OwnerAgentConfigType] = None
    error: Optional[str] = None


@strawberry.type
class OwnerAgentAuthorizedPhoneResult:
    success: bool = False
    phone: Optional[OwnerAgentAuthorizedPhoneType] = None
    error: Optional[str] = None
