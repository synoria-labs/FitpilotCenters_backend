"""Tests for the database-backed half of ``fitness_estimation_service``.

The pure formula is covered in ``test_campaigns.py``; what needs a real database is the
claim this whole change rests on — that the gym's schedule already contains the answer.
``load_schedule`` has to read opening days and class duration out of ``class_templates``,
and ``cadence_for`` has to turn a member's reservations into "sessions per week", so that a
future gym with Saturday classes or 45-minute sessions is correct without configuring
anything.

Same factory pattern and rolled-back ``db`` fixture as
``test_attendance_profile_service.py``.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.crud import fitnessEstimationCrud as crud
from app.models import (
    ClassSession,
    ClassTemplate,
    ClassType,
    People,
    PersonRole,
    Reservation,
    Role,
    Venue,
)
from app.services import fitness_estimation_service as estimation

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


async def _get_or_make_venue(db) -> Venue:
    venue = (await db.execute(select(Venue).limit(1))).scalar_one_or_none()
    if venue is None:
        venue = Venue(name="Test Venue", capacity=20)
        db.add(venue)
        await db.flush()
    return venue


async def _make_class_type(db, *, code: str, name: str, met_value=None) -> ClassType:
    existing = (
        await db.execute(select(ClassType).where(ClassType.code == code))
    ).scalar_one_or_none()
    if existing:
        existing.met_value = met_value
        await db.flush()
        return existing
    ctype = ClassType(code=code, name=name, met_value=met_value)
    db.add(ctype)
    await db.flush()
    return ctype


async def _make_template(
    db, *, class_type, venue, weekday: int, duration_min: int = 60, is_active: bool = True
) -> ClassTemplate:
    tpl = ClassTemplate(
        class_type_id=class_type.id,
        venue_id=venue.id,
        default_duration_min=duration_min,
        weekday=weekday,
        start_time_local=time(7, 0),
        is_active=is_active,
    )
    db.add(tpl)
    await db.flush()
    return tpl


async def _book(db, *, person, class_type, venue, start_at, template=None) -> Reservation:
    sess = ClassSession(
        class_type_id=class_type.id,
        venue_id=venue.id,
        template_id=template.id if template else None,
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        capacity=10,
        status="scheduled",
    )
    db.add(sess)
    await db.flush()
    reservation = Reservation(
        session_id=sess.id, person_id=person.id, status="reserved", reserved_at=start_at
    )
    db.add(reservation)
    await db.flush()
    return reservation


# ---------------------------------------------------------------------------
# load_schedule — the half that needs no configuration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_opening_days_and_duration_come_from_the_weekly_schedule(db):
    """A Monday-to-Friday gym reads as five days, straight from its own templates."""
    venue = await _get_or_make_venue(db)
    spinning = await _make_class_type(db, code="spinning_sched", name="Spinning")
    for weekday in (1, 2, 3, 4, 5):
        await _make_template(db, class_type=spinning, venue=venue, weekday=weekday)

    schedule = await estimation.load_schedule(db)

    assert {1, 2, 3, 4, 5} <= set(schedule.open_weekdays)
    assert schedule.open_days_per_week >= 5
    assert schedule.duration_by_class_type[spinning.id] == 60


@pytest.mark.asyncio
async def test_a_weekend_gym_is_derived_not_configured(db):
    """The requirement that started this: a future gym open on Saturdays gets the right
    number of open days the moment it creates the schedule, with nothing to set up."""
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="weekend_hiit", name="HIIT")
    for weekday in range(7):
        await _make_template(db, class_type=ctype, venue=venue, weekday=weekday)

    schedule = await estimation.load_schedule(db)
    assert schedule.open_days_per_week == 7


@pytest.mark.asyncio
async def test_retired_slots_do_not_keep_the_gym_open(db):
    """An inactive Saturday template is a slot that no longer runs; counting it would
    inflate the estimate for every member forever."""
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="retired_slot", name="Retirada")
    await _make_template(db, class_type=ctype, venue=venue, weekday=6, is_active=False)

    schedule = await estimation.load_schedule(db)
    assert 6 not in schedule.open_weekdays


@pytest.mark.asyncio
async def test_duration_is_per_activity_not_gym_wide(db):
    """A 90-minute yoga class and a 45-minute express must not average into one number for
    the member who only ever attends one of them."""
    venue = await _get_or_make_venue(db)
    yoga = await _make_class_type(db, code="yoga_long", name="Yoga")
    express = await _make_class_type(db, code="express_short", name="Express")
    await _make_template(db, class_type=yoga, venue=venue, weekday=2, duration_min=90)
    await _make_template(db, class_type=express, venue=venue, weekday=3, duration_min=45)

    schedule = await estimation.load_schedule(db)
    assert schedule.duration_by_class_type[yoga.id] == 90
    assert schedule.duration_by_class_type[express.id] == 45


@pytest.mark.asyncio
async def test_a_null_met_inherits_the_code_default_without_a_backfill(db):
    """The constraint behind the nullable column: an existing catalog stays untouched and
    still resolves to a sensible intensity."""
    await _make_class_type(db, code="spinning", name="Spinning", met_value=None)
    schedule = await estimation.load_schedule(db)

    spinning = (
        await db.execute(select(ClassType).where(ClassType.code == "spinning"))
    ).scalar_one()
    assert spinning.met_value is None  # nothing was written to the row
    assert schedule.met_by_class_type[spinning.id] == 8.5  # Compendium 02017


@pytest.mark.asyncio
async def test_an_explicit_met_overrides_the_code_default(db):
    ctype = await _make_class_type(db, code="spinning", name="Spinning", met_value=10.5)
    schedule = await estimation.load_schedule(db)
    assert schedule.met_by_class_type[ctype.id] == 10.5


@pytest.mark.asyncio
async def test_an_unknown_activity_code_falls_through_to_the_global_default(db):
    """A gym inventing its own activity name must still produce a number, not a blank."""
    ctype = await _make_class_type(db, code="aquaspin_novel", name="Aquaspin")
    profile = estimation.EstimationProfile(
        config=estimation.EstimationConfig(), schedule=await estimation.load_schedule(db)
    )
    assert profile.met_for(ctype.id) == estimation.EstimationConfig().default_met


# ---------------------------------------------------------------------------
# cadence_for — the member's real rhythm
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cadence_is_measured_per_active_week(db):
    """Six bookings across two weeks is three a week, not six spread over the lookback.

    Dividing by the whole window would fold the ramp-down before churn into the answer and
    understate every lapsed member — which is exactly the population this is quoted to.
    """
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Cadencia Tres")
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="cadence_spin", name="Spinning")

    # Two distinct weeks, three bookings in each.
    base = _NOW - timedelta(days=21)
    for week_offset in (0, 7):
        for day in (0, 1, 2):
            await _book(
                db,
                person=person,
                class_type=ctype,
                venue=venue,
                start_at=base + timedelta(days=week_offset + day),
            )

    cadence = await estimation.cadence_for(db, [person.id], lookback_days=180, min_bookings=4)
    assert cadence[person.id] == 3.0


@pytest.mark.asyncio
async def test_a_member_below_the_threshold_is_left_out_entirely(db):
    """One booking is not a rhythm. Absent from the map means the caller uses the configured
    default instead of extrapolating a cadence from a single visit."""
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Una Sola Vez")
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="thin_history", name="Spinning")
    await _book(
        db, person=person, class_type=ctype, venue=venue, start_at=_NOW - timedelta(days=10)
    )

    cadence = await estimation.cadence_for(db, [person.id], lookback_days=180, min_bookings=4)
    assert person.id not in cadence


@pytest.mark.asyncio
async def test_cancelled_bookings_do_not_count_as_attendance(db):
    """Consistent with segmentation_service: a cancellation is not evidence of showing up."""
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Canceladas")
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="cancelled_hist", name="Spinning")
    for day in range(6):
        reservation = await _book(
            db,
            person=person,
            class_type=ctype,
            venue=venue,
            start_at=_NOW - timedelta(days=10 + day),
        )
        reservation.status = "canceled"
    await db.flush()

    cadence = await estimation.cadence_for(db, [person.id], lookback_days=180, min_bookings=4)
    assert person.id not in cadence


@pytest.mark.asyncio
async def test_cadence_ignores_bookings_outside_the_lookback(db):
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Historia Vieja")
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="old_history", name="Spinning")
    for day in range(6):
        await _book(
            db,
            person=person,
            class_type=ctype,
            venue=venue,
            start_at=_NOW - timedelta(days=400 + day),
        )

    cadence = await estimation.cadence_for(db, [person.id], lookback_days=180, min_bookings=4)
    assert person.id not in cadence


@pytest.mark.asyncio
async def test_cadence_for_is_empty_for_an_empty_audience(db):
    assert await estimation.cadence_for(db, []) == {}
    assert await estimation.days_since_last_class_for(db, []) == {}


# ---------------------------------------------------------------------------
# days_since_last_class_for
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_days_since_last_class_uses_the_most_recent_past_session(db):
    """The honest anchor for "hace X días que no te vemos": when they last came, not when
    they last paid."""
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Ultima Clase")
    venue = await _get_or_make_venue(db)
    ctype = await _make_class_type(db, code="last_class", name="Spinning")
    await _book(
        db, person=person, class_type=ctype, venue=venue, start_at=_NOW - timedelta(days=60)
    )
    await _book(
        db, person=person, class_type=ctype, venue=venue, start_at=_NOW - timedelta(days=20)
    )
    # A future booking must not read as attendance that already happened.
    await _book(
        db, person=person, class_type=ctype, venue=venue, start_at=_NOW + timedelta(days=3)
    )

    result = await estimation.days_since_last_class_for(db, [person.id])
    assert result[person.id] == 20


@pytest.mark.asyncio
async def test_a_member_who_never_booked_is_absent(db):
    role = await _ensure_member_role(db)
    person = await _make_member(db, role=role, full_name="Nunca Vino")
    result = await estimation.days_since_last_class_for(db, [person.id])
    assert person.id not in result


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_config_rejects_values_that_would_ship_a_broken_message(db):
    """A misplaced decimal is valid NUMERIC and an unsendable claim."""
    with pytest.raises(crud.EstimationConfigError):
        await crud.upsert_config(db, commit=False, reference_weight_kg=700)
    with pytest.raises(crud.EstimationConfigError):
        await crud.upsert_config(db, commit=False, default_met=90)
    with pytest.raises(crud.EstimationConfigError):
        await crud.upsert_config(db, commit=False, horizon_weeks=0)


@pytest.mark.asyncio
async def test_partial_save_leaves_the_other_knobs_alone(db):
    await crud.upsert_config(db, commit=False, reference_weight_kg=75, horizon_weeks=8)
    await crud.upsert_config(db, commit=False, horizon_weeks=10)

    data = await crud.get_config(db)
    assert data.horizon_weeks == 10
    assert float(data.reference_weight_kg) == 75.0


@pytest.mark.asyncio
async def test_clearing_a_met_returns_the_activity_to_its_default(db):
    """Null is a meaningful value here, which is why the column is never backfilled."""
    ctype = await _make_class_type(db, code="spinning", name="Spinning", met_value=11.0)
    await crud.set_class_type_met(db, ctype.id, None, commit=False)

    intensities = await crud.list_class_type_intensities(db)
    row = next(i for i in intensities if i.id == ctype.id)
    assert row.met_value is None
    assert row.is_default is True
    assert row.effective_met == 8.5


@pytest.mark.asyncio
async def test_set_class_type_met_rejects_an_impossible_intensity(db):
    ctype = await _make_class_type(db, code="met_range", name="Rango")
    with pytest.raises(crud.EstimationConfigError):
        await crud.set_class_type_met(db, ctype.id, 40.0, commit=False)
