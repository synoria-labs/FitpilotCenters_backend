"""Tests for attendance_profile_service.favorite_classes_for's standing-booking priority.

An active standing booking is an explicit recurring commitment, not a statistical inference,
so it must win over (and does not need) a dominant slot in booking history. These tests build
real People/ClassType/ClassTemplate/ClassSession/Reservation/StandingBooking rows against the
rolled-back ``db`` fixture, following the factory pattern from test_dashboard_metrics.py.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    ClassSession,
    ClassTemplate,
    ClassType,
    MembershipPlan,
    MembershipSubscription,
    People,
    PersonRole,
    Reservation,
    Role,
    StandingBooking,
    Venue,
)
from app.services import attendance_profile_service as attendance

_NOW = datetime.now(timezone.utc)


async def _ensure_member_role(db) -> Role:
    existing = (await db.execute(select(Role).where(Role.code == "member"))).scalar_one_or_none()
    if existing:
        return existing
    role = Role(code="member", description="Member")
    db.add(role)
    await db.flush()
    return role


async def _make_member(db, *, role: Role, full_name: str) -> People:
    person = People(full_name=full_name)
    db.add(person)
    await db.flush()
    db.add(PersonRole(person_id=person.id, role_id=role.id, created_at=_NOW))
    await db.flush()
    return person


async def _make_plan_and_subscription(db, person: People) -> MembershipSubscription:
    plan = MembershipPlan(
        name="Test Plan", price=Decimal("100"), duration_value=1, duration_unit="month"
    )
    db.add(plan)
    await db.flush()
    sub = MembershipSubscription(
        person_id=person.id,
        plan_id=plan.id,
        start_at=_NOW - timedelta(days=30),
        end_at=_NOW + timedelta(days=30),
        status="active",
    )
    db.add(sub)
    await db.flush()
    return sub


async def _get_or_make_venue(db) -> Venue:
    venue = (await db.execute(select(Venue).limit(1))).scalar_one_or_none()
    if venue is None:
        venue = Venue(name="Test Venue", capacity=20)
        db.add(venue)
        await db.flush()
    return venue


async def _make_class_type(db, *, code: str, name: str) -> ClassType:
    existing = (
        await db.execute(select(ClassType).where(ClassType.code == code))
    ).scalar_one_or_none()
    if existing:
        return existing
    ctype = ClassType(code=code, name=name)
    db.add(ctype)
    await db.flush()
    return ctype


async def _make_template(
    db, *, class_type: ClassType, venue: Venue, weekday: int, start_time_local: time
) -> ClassTemplate:
    tpl = ClassTemplate(
        class_type_id=class_type.id,
        venue_id=venue.id,
        default_duration_min=60,
        weekday=weekday,
        start_time_local=start_time_local,
    )
    db.add(tpl)
    await db.flush()
    return tpl


async def _make_session(
    db, *, template: ClassTemplate, class_type: ClassType, venue: Venue, start_at: datetime
) -> ClassSession:
    sess = ClassSession(
        class_type_id=class_type.id,
        venue_id=venue.id,
        template_id=template.id,
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        capacity=10,
        status="scheduled",
    )
    db.add(sess)
    await db.flush()
    return sess


async def _reserve(db, *, session: ClassSession, person: People, status: str) -> None:
    db.add(Reservation(session_id=session.id, person_id=person.id, status=status))
    await db.flush()


async def _make_standing_booking(
    db, *, person: People, subscription: MembershipSubscription, template: ClassTemplate,
    status: str = "active",
) -> StandingBooking:
    sb = StandingBooking(
        person_id=person.id,
        subscription_id=subscription.id,
        template_id=template.id,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=180),
        status=status,
    )
    db.add(sb)
    await db.flush()
    return sb


async def test_standing_booking_resolves_schedule_even_without_a_dominant_slot(db):
    """The exact bug report: bookings split across two slots leave no statistical winner,
    but an active standing booking still names a schedule."""
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Standing Booking Member")
    subscription = await _make_plan_and_subscription(db, person)
    venue = await _get_or_make_venue(db)
    spinning = await _make_class_type(db, code="test_spin_std", name="Spinning")

    monday = await _make_template(
        db, class_type=spinning, venue=venue, weekday=1, start_time_local=time(7, 0)
    )
    wednesday = await _make_template(
        db, class_type=spinning, venue=venue, weekday=3, start_time_local=time(18, 0)
    )
    await _make_standing_booking(db, person=person, subscription=subscription, template=monday)

    # Bookings split evenly across both slots: neither template dominates.
    for i in range(3):
        sess = await _make_session(
            db, template=monday, class_type=spinning, venue=venue,
            start_at=_NOW - timedelta(days=7 * i, hours=-1),
        )
        await _reserve(db, session=sess, person=person, status="checked_in")
        sess2 = await _make_session(
            db, template=wednesday, class_type=spinning, venue=venue,
            start_at=_NOW - timedelta(days=7 * i, hours=-2),
        )
        await _reserve(db, session=sess2, person=person, status="checked_in")

    result = await attendance.favorite_classes_for(db, [person.id])
    favorite = result[person.id]
    assert favorite.class_template_id == monday.id
    assert favorite.day_label == "lunes"
    assert favorite.time_label == "7:00 a. m."


async def test_multiple_standing_bookings_pick_the_one_attended_most(db):
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Double Standing Booking Member")
    subscription = await _make_plan_and_subscription(db, person)
    venue = await _get_or_make_venue(db)
    spinning = await _make_class_type(db, code="test_spin_multi", name="Spinning")
    yoga = await _make_class_type(db, code="test_yoga_multi", name="Yoga")

    monday_spin = await _make_template(
        db, class_type=spinning, venue=venue, weekday=1, start_time_local=time(7, 0)
    )
    wednesday_yoga = await _make_template(
        db, class_type=yoga, venue=venue, weekday=3, start_time_local=time(18, 0)
    )
    await _make_standing_booking(
        db, person=person, subscription=subscription, template=monday_spin
    )
    await _make_standing_booking(
        db, person=person, subscription=subscription, template=wednesday_yoga
    )

    # Attends Yoga (checked_in) far more than Spinning (only reserved, never attended).
    for i in range(4):
        sess = await _make_session(
            db, template=wednesday_yoga, class_type=yoga, venue=venue,
            start_at=_NOW - timedelta(days=7 * i, hours=-1),
        )
        await _reserve(db, session=sess, person=person, status="checked_in")
    sess = await _make_session(
        db, template=monday_spin, class_type=spinning, venue=venue,
        start_at=_NOW - timedelta(days=1),
    )
    await _reserve(db, session=sess, person=person, status="reserved")

    result = await attendance.favorite_classes_for(db, [person.id])
    favorite = result[person.id]
    assert favorite.class_template_id == wednesday_yoga.id
    assert favorite.class_type_name == "Yoga"


async def test_paused_standing_booking_is_ignored(db):
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Paused Standing Booking Member")
    subscription = await _make_plan_and_subscription(db, person)
    venue = await _get_or_make_venue(db)
    spinning = await _make_class_type(db, code="test_spin_paused", name="Spinning")

    monday = await _make_template(
        db, class_type=spinning, venue=venue, weekday=1, start_time_local=time(7, 0)
    )
    await _make_standing_booking(
        db, person=person, subscription=subscription, template=monday, status="paused"
    )
    # No booking history at all otherwise.

    result = await attendance.favorite_classes_for(db, [person.id])
    assert person.id not in result


async def test_no_standing_booking_keeps_existing_dominant_slot_behavior(db):
    """Regression check: someone with no standing booking still gets the old inference."""
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="No Standing Booking Member")
    await _make_plan_and_subscription(db, person)
    venue = await _get_or_make_venue(db)
    spinning = await _make_class_type(db, code="test_spin_dominant", name="Spinning")

    monday = await _make_template(
        db, class_type=spinning, venue=venue, weekday=1, start_time_local=time(7, 0)
    )
    for i in range(3):
        sess = await _make_session(
            db, template=monday, class_type=spinning, venue=venue,
            start_at=_NOW - timedelta(days=7 * i, hours=-1),
        )
        await _reserve(db, session=sess, person=person, status="checked_in")

    result = await attendance.favorite_classes_for(db, [person.id])
    favorite = result[person.id]
    assert favorite.class_template_id == monday.id
    assert favorite.day_label == "lunes"


async def test_standing_booking_ranking_ignores_another_members_classes(db):
    """Two members with multiple standing bookings, one dropping in on the other's slot.

    The attendance-weight query filters people and templates independently, so it also
    returns (member, someone else's template) rows. Ranking those picked a template the
    member has no standing booking on, and resolving it raised StopIteration inside the
    coroutine — which took the whole campaign audience build down with it.
    """
    role = await _ensure_member_role(db)
    venue = await _get_or_make_venue(db)
    dropper = await _make_member(db, role=role, full_name="Cross Attendance Member")
    other = await _make_member(db, role=role, full_name="Popular Slot Owner")
    dropper_sub = await _make_plan_and_subscription(db, dropper)
    other_sub = await _make_plan_and_subscription(db, other)

    spinning = await _make_class_type(db, code="test_spin_cross", name="Spinning")
    yoga = await _make_class_type(db, code="test_yoga_cross", name="Yoga")
    boxing = await _make_class_type(db, code="test_box_cross", name="Boxeo")
    hiit = await _make_class_type(db, code="test_hiit_cross", name="HIIT")

    monday_spin = await _make_template(
        db, class_type=spinning, venue=venue, weekday=1, start_time_local=time(7, 0)
    )
    wednesday_yoga = await _make_template(
        db, class_type=yoga, venue=venue, weekday=3, start_time_local=time(18, 0)
    )
    friday_box = await _make_template(
        db, class_type=boxing, venue=venue, weekday=5, start_time_local=time(19, 0)
    )
    saturday_hiit = await _make_template(
        db, class_type=hiit, venue=venue, weekday=6, start_time_local=time(9, 0)
    )

    for template in (monday_spin, wednesday_yoga):
        await _make_standing_booking(
            db, person=dropper, subscription=dropper_sub, template=template
        )
    for template in (friday_box, saturday_hiit):
        await _make_standing_booking(
            db, person=other, subscription=other_sub, template=template
        )

    # Where this member actually shows up most is HIIT — but that is the *other* member's
    # standing booking, so it can never be this member's standing-booking answer.
    for i in range(4):
        sess = await _make_session(
            db, template=saturday_hiit, class_type=hiit, venue=venue,
            start_at=_NOW - timedelta(days=7 * i, hours=-1),
        )
        await _reserve(db, session=sess, person=dropper, status="checked_in")
    # Of their own two standing bookings, Yoga is the one they attend.
    sess = await _make_session(
        db, template=wednesday_yoga, class_type=yoga, venue=venue,
        start_at=_NOW - timedelta(days=2),
    )
    await _reserve(db, session=sess, person=dropper, status="checked_in")
    # The other member attends their own Boxing slot and never the HIIT one.
    for i in range(2):
        sess = await _make_session(
            db, template=friday_box, class_type=boxing, venue=venue,
            start_at=_NOW - timedelta(days=7 * i + 1),
        )
        await _reserve(db, session=sess, person=other, status="checked_in")

    result = await attendance.favorite_classes_for(db, [dropper.id, other.id])

    assert result[dropper.id].class_template_id == wednesday_yoga.id
    assert result[dropper.id].class_type_name == "Yoga"
    assert result[other.id].class_template_id == friday_box.id
    assert result[other.id].class_type_name == "Boxeo"
