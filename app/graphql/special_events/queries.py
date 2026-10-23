"""GraphQL queries for special events. Every resolver requires ``manage_special_events``."""
import logging
from typing import List, Optional

import strawberry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.core.timezones import STUDIO_TIMEZONE
from app.crud.permissions import MANAGE_SPECIAL_EVENTS
from app.crud.special_events import events as events_crud
from app.crud.special_events import takeover as takeover_crud
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.special_events.builders import (
    build_detail,
    build_displacement,
    build_event,
    build_restore_preview,
    build_takeover_preview,
    payments_status,
)
from app.graphql.special_events.types import (
    OptionType,
    RestorePreviewType,
    SpecialEventDetailType,
    SpecialEventDisplacementType,
    SpecialEventFormOptionsType,
    SpecialEventSeatType,
    SpecialEventType,
    TakeoverPreviewType,
    VenueOptionType,
)
from app.models import ClassType, People, PersonRole, Role, Seat, Venue

logger = logging.getLogger(__name__)


@strawberry.type
class SpecialEventsQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_events(self, info: Info, include_past: bool = False) -> List[SpecialEventType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return []
        db: AsyncSession = info.context.db
        events = await events_crud.list_events(db, include_past=include_past)
        return [await build_event(db, e) for e in events]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_event(self, info: Info, id: int) -> Optional[SpecialEventDetailType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return None
        return await build_detail(info.context.db, id)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_event_takeover_preview(
        self, info: Info, event_id: int
    ) -> Optional[TakeoverPreviewType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return None
        db: AsyncSession = info.context.db
        event = await events_crud.get_event(db, event_id)
        if event is None:
            return None
        preview = await takeover_crud.preview_takeover(db, event)
        return build_takeover_preview(preview, event.timezone)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_event_restore_preview(
        self, info: Info, event_id: int
    ) -> Optional[RestorePreviewType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return None
        db: AsyncSession = info.context.db
        event = await events_crud.get_event(db, event_id)
        if event is None:
            return None
        preview = await takeover_crud.preview_restore(db, event)
        return build_restore_preview(preview, event.timezone)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_event_displacements(
        self,
        info: Info,
        event_id: Optional[int] = None,
        follow_up_status: Optional[str] = None,
    ) -> List[SpecialEventDisplacementType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return []
        rows = await takeover_crud.list_displacements(
            info.context.db, event_id=event_id, follow_up_status=follow_up_status
        )
        return [build_displacement(r) for r in rows]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def special_event_form_options(self, info: Info) -> Optional[SpecialEventFormOptionsType]:
        if await require_capability(info, MANAGE_SPECIAL_EVENTS):
            return None
        db: AsyncSession = info.context.db
        venues = (await db.execute(select(Venue).order_by(Venue.id))).scalars().all()
        seats = (
            await db.execute(
                select(Seat)
                .where(Seat.is_active.is_(True))
                .order_by(Seat.venue_id, func.length(Seat.label), Seat.label)
            )
        ).scalars().all()
        by_venue = {}
        for seat in seats:
            by_venue.setdefault(seat.venue_id, []).append(
                SpecialEventSeatType(seat_id=seat.id, label=seat.label, status="free")
            )
        instructors = (
            await db.execute(
                select(People.id, People.full_name)
                .join(PersonRole, PersonRole.person_id == People.id)
                .join(Role, Role.id == PersonRole.role_id)
                .where(Role.code == "instructor", People.deleted_at.is_(None))
                .order_by(People.full_name)
            )
        ).all()
        class_types = (
            await db.execute(select(ClassType.id, ClassType.name).order_by(ClassType.name))
        ).all()
        ready, warning = payments_status()
        return SpecialEventFormOptionsType(
            venues=[
                VenueOptionType(
                    id=v.id, name=v.name, address=v.address, seats=by_venue.get(v.id, [])
                )
                for v in venues
            ],
            instructors=[OptionType(id=i, name=n or f"Persona {i}") for i, n in instructors],
            class_types=[OptionType(id=i, name=n) for i, n in class_types],
            default_timezone=STUDIO_TIMEZONE,
            payments_ready=ready,
            payments_warning=warning,
        )
