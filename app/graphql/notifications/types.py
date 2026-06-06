"""GraphQL types for the automated notification configuration.

These power the desktop "Notificaciones" tab: one ``NotificationSettingType`` per business
event (template + variable mapping + offsets + enabled), plus a ``NotificationEventCatalog``
that tells the UI which variables each event can resolve so the placeholder pickers stay in
sync with the backend.
"""
from typing import Any, Dict, List, Optional

import strawberry

from app.crud.notificationsCrud import NotificationSettingData
from app.graphql.whatsapp.template_types import WhatsAppTemplate


@strawberry.type
class NotificationVariable:
    key: str
    label: str
    sample: str


@strawberry.type
class NotificationEventCatalog:
    event_type: str
    label: str
    supports_offsets: bool
    variables: List[NotificationVariable]


@strawberry.type
class NotificationSettingType:
    event_type: str
    label: str
    supports_offsets: bool
    enabled: bool
    template_id: Optional[int]
    param_mapping: Optional[List[str]]
    offsets_days: Optional[List[int]]
    template: Optional[WhatsAppTemplate]

    @classmethod
    def from_data(
        cls,
        event_type: str,
        meta: Dict[str, Any],
        data: Optional[NotificationSettingData],
        template: Optional[WhatsAppTemplate],
    ) -> "NotificationSettingType":
        param_mapping = None
        offsets_days = None
        if data is not None:
            if data.param_mapping is not None:
                param_mapping = [str(v) for v in data.param_mapping]
            if data.offsets_days is not None:
                offsets_days = [int(v) for v in data.offsets_days]
        return cls(
            event_type=event_type,
            label=str(meta.get("label", event_type)),
            supports_offsets=bool(meta.get("supports_offsets", False)),
            enabled=bool(data.enabled) if data is not None else False,
            template_id=data.template_id if data is not None else None,
            param_mapping=param_mapping,
            offsets_days=offsets_days,
            template=template,
        )


@strawberry.input
class SaveNotificationSettingInput:
    event_type: str
    enabled: bool = False
    template_id: Optional[int] = None
    param_mapping: Optional[List[str]] = None
    offsets_days: Optional[List[int]] = None


@strawberry.type
class NotificationSettingResult:
    success: bool = False
    setting: Optional[NotificationSettingType] = None
    error: Optional[str] = None


@strawberry.type
class SweepResult:
    success: bool = False
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    error: Optional[str] = None
