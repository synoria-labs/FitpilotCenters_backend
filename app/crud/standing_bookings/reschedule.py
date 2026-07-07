from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.classModel import (
    ClassTemplate,
    StandingBooking,
    StandingBookingException,
    Reservation,
)

from .bookings import create_standing_booking_exception
from .data import RescheduleItem
from .utils import (
    _build_template_weekday_map,
    _get_group_standing_bookings,
    _get_person_reservation,
    _get_templates_in_same_group,
    _load_sessions_by_template_ids,
    _resolve_seat_for_session,
    _session_has_capacity,
)


def _count_reschedule_items(items: List[RescheduleItem]) -> Dict[str, int]:
    counts: Dict[str, int] = {"total": len(items)}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


async def build_reschedule_plan(
    db: AsyncSession,
    *,
    standing_booking_id: int,
    start_date: date,
    end_date: date,
    target_template_id: int,
    target_seat_id: Optional[int] = None,
) -> List[RescheduleItem]:
    if start_date > end_date:
        raise ValueError("Start date must be before end date")

    sb_stmt = (
        select(StandingBooking)
        .options(joinedload(StandingBooking.template))
        .where(StandingBooking.id == standing_booking_id)
    )
    sb_result = await db.execute(sb_stmt)
    standing_booking = sb_result.scalar_one_or_none()

    if not standing_booking or standing_booking.status != "active":
        raise ValueError("Standing booking not found or inactive")

    source_template = standing_booking.template
    if not source_template:
        template_stmt = select(ClassTemplate).where(ClassTemplate.id == standing_booking.template_id)
        template_result = await db.execute(template_stmt)
        source_template = template_result.scalar_one_or_none()

    if not source_template:
        raise ValueError("Source template not found")

    target_template_stmt = select(ClassTemplate).where(ClassTemplate.id == target_template_id)
    target_template_result = await db.execute(target_template_stmt)
    target_template = target_template_result.scalar_one_or_none()

    if not target_template:
        raise ValueError("Target template not found")

    if source_template.class_type_id != target_template.class_type_id:
        raise ValueError("Target template must match class type")

    source_templates = await _get_templates_in_same_group(db, standing_booking.template_id)
    if not source_templates:
        source_templates = [source_template]

    target_templates = await _get_templates_in_same_group(db, target_template_id)
    if not target_templates:
        target_templates = [target_template]

    source_templates_by_day = _build_template_weekday_map(source_templates)
    target_templates_by_day = _build_template_weekday_map(target_templates)

    source_template_ids = [template.id for template in source_templates]
    target_template_ids = [template.id for template in target_templates]

    group_bookings = await _get_group_standing_bookings(db, standing_booking, source_template_ids)
    source_sessions = await _load_sessions_by_template_ids(db, source_template_ids, start_date, end_date)
    target_sessions = await _load_sessions_by_template_ids(db, target_template_ids, start_date, end_date)

    items: List[RescheduleItem] = []
    current_date = start_date

    while current_date <= end_date:
        weekday = current_date.isoweekday()
        source_template = source_templates_by_day.get(weekday)
        if source_template is None:
            current_date += timedelta(days=1)
            continue

        sb_for_date = group_bookings.get(source_template.id, standing_booking)

        if current_date < sb_for_date.start_date or current_date > sb_for_date.end_date:
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=None,
                    target_session_id=None,
                    seat_id=None,
                    status="outside_window",
                    reason="Outside standing booking window",
                )
            )
            current_date += timedelta(days=1)
            continue

        source_session = source_sessions.get((source_template.id, current_date))
        if source_session is None:
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=None,
                    target_session_id=None,
                    seat_id=None,
                    status="missing_source_session",
                    reason="Source session not found",
                )
            )
            current_date += timedelta(days=1)
            continue

        existing_source = await _get_person_reservation(db, sb_for_date.person_id, source_session.id)
        if existing_source and existing_source.status == "checked_in":
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=None,
                    seat_id=None,
                    status="blocked_checked_in",
                    reason="Source session already checked in",
                )
            )
            current_date += timedelta(days=1)
            continue

        exception_stmt = select(StandingBookingException).where(
            and_(
                StandingBookingException.standing_booking_id == sb_for_date.id,
                StandingBookingException.session_date == current_date,
            )
        )
        exception_result = await db.execute(exception_stmt)
        if exception_result.scalar_one_or_none():
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=None,
                    seat_id=None,
                    status="existing_exception",
                    reason="Exception already exists for this date",
                )
            )
            current_date += timedelta(days=1)
            continue

        target_template = target_templates_by_day.get(weekday)
        if target_template is None:
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=None,
                    seat_id=None,
                    status="missing_target_template",
                    reason="Target schedule not available for this weekday",
                )
            )
            current_date += timedelta(days=1)
            continue

        target_session = target_sessions.get((target_template.id, current_date))
        if target_session is None:
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=None,
                    seat_id=None,
                    status="missing_target_session",
                    reason="Target session not found",
                )
            )
            current_date += timedelta(days=1)
            continue

        if target_session.start_at <= datetime.now(target_session.start_at.tzinfo):
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=target_session.id,
                    seat_id=None,
                    status="past_session",
                    reason="Target session is in the past",
                )
            )
            current_date += timedelta(days=1)
            continue

        existing_target = await _get_person_reservation(db, sb_for_date.person_id, target_session.id)
        if existing_target:
            items.append(
                RescheduleItem(
                    session_date=current_date,
                    standing_booking_id=sb_for_date.id,
                    source_session_id=source_session.id,
                    target_session_id=target_session.id,
                    seat_id=None,
                    status="existing_target_reservation",
                    reason="Target reservation already exists",
                )
            )
            current_date += timedelta(days=1)
            continue

        preferred_seat_id = target_seat_id if target_seat_id is not None else sb_for_date.seat_id
        seat_id = None
        if preferred_seat_id is not None:
            seat_id, seat_reason = await _resolve_seat_for_session(db, target_session, preferred_seat_id)
            if seat_id is None:
                items.append(
                    RescheduleItem(
                        session_date=current_date,
                        standing_booking_id=sb_for_date.id,
                        source_session_id=source_session.id,
                        target_session_id=target_session.id,
                        seat_id=None,
                        status="blocked_seat_taken",
                        reason=seat_reason or "Seat not available",
                    )
                )
                current_date += timedelta(days=1)
                continue
        else:
            has_capacity = await _session_has_capacity(db, target_session)
            if not has_capacity:
                items.append(
                    RescheduleItem(
                        session_date=current_date,
                        standing_booking_id=sb_for_date.id,
                        source_session_id=source_session.id,
                        target_session_id=target_session.id,
                        seat_id=None,
                        status="blocked_no_capacity",
                        reason="Target session is full",
                    )
                )
                current_date += timedelta(days=1)
                continue

        items.append(
            RescheduleItem(
                session_date=current_date,
                standing_booking_id=sb_for_date.id,
                source_session_id=source_session.id,
                target_session_id=target_session.id,
                seat_id=seat_id,
                status="will_create",
                reason="Ready to reschedule",
            )
        )

        current_date += timedelta(days=1)

    return items


async def preview_reschedule_standing_booking(
    db: AsyncSession,
    *,
    standing_booking_id: int,
    start_date: date,
    end_date: date,
    target_template_id: int,
    target_seat_id: Optional[int] = None,
) -> Dict[str, Any]:
    items = await build_reschedule_plan(
        db,
        standing_booking_id=standing_booking_id,
        start_date=start_date,
        end_date=end_date,
        target_template_id=target_template_id,
        target_seat_id=target_seat_id,
    )
    return {"items": items, "counts": _count_reschedule_items(items)}


async def reschedule_standing_booking(
    db: AsyncSession,
    *,
    standing_booking_id: int,
    start_date: date,
    end_date: date,
    target_template_id: int,
    target_seat_id: Optional[int] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    sb_stmt = select(StandingBooking).where(StandingBooking.id == standing_booking_id)
    sb_result = await db.execute(sb_stmt)
    standing_booking = sb_result.scalar_one_or_none()
    if not standing_booking:
        raise ValueError("Standing booking not found")

    items = await build_reschedule_plan(
        db,
        standing_booking_id=standing_booking_id,
        start_date=start_date,
        end_date=end_date,
        target_template_id=target_template_id,
        target_seat_id=target_seat_id,
    )

    if strict:
        blocked = [item for item in items if item.status != "will_create"]
        if blocked:
            return {
                "success": False,
                "items": items,
                "counts": _count_reschedule_items(items),
                "message": "Reschedule aborted due to blocked items",
            }

    for item in items:
        if item.status != "will_create":
            continue

        try:
            from app.crud.reservationsCrud import create_reservation

            # SAVEPOINT per item: a failed insert (e.g. constraint violation from a
            # concurrent booking) must skip only this item, not poison the whole
            # transaction so every later item fails with InFailedSQLTransaction.
            async with db.begin_nested():
                await create_reservation(
                    db=db,
                    session_id=item.target_session_id,
                    person_id=standing_booking.person_id,
                    seat_id=item.seat_id,
                    source="override",
                    commit=False,
                )

                await create_standing_booking_exception(
                    db=db,
                    standing_booking_id=item.standing_booking_id,
                    session_date=item.session_date,
                    action="reschedule",
                    new_session_id=item.target_session_id,
                    new_seat_id=item.seat_id,
                )

                if item.source_session_id:
                    reservation_stmt = select(Reservation).where(
                        and_(
                            Reservation.session_id == item.source_session_id,
                            Reservation.person_id == standing_booking.person_id,
                        )
                    )
                    reservation_result = await db.execute(reservation_stmt)
                    reservation = reservation_result.scalar_one_or_none()
                    if reservation and reservation.status in ["reserved", "waitlisted"]:
                        reservation.status = "canceled"

            item.status = "rescheduled"
            item.reason = "Rescheduled"
        except Exception as exc:
            item.status = "error"
            item.reason = f"Failed to reschedule: {exc}"
            if strict:
                await db.rollback()
                return {
                    "success": False,
                    "items": items,
                    "counts": _count_reschedule_items(items),
                    "message": "Reschedule failed",
                }

    await db.commit()
    return {
        "success": True,
        "items": items,
        "counts": _count_reschedule_items(items),
        "message": "Reschedule completed",
    }
