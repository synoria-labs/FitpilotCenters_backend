"""Paid historical classes and administrator-approved overlapping renewals."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.crud.memberships.day_passes import purchase_day_pass
from app.crud.memberships.enrollment import create_member_enrollment_with_standing_booking, renew_subscription_with_standing_booking
from app.crud.memberships.overlaps import OverlapAuthorizationError, authorize_overlap
from app.crud.pos.sales import create_sale, get_sale, SaleLineInput, SalePaymentInput
from app.crud.reservationsCrud import get_available_sessions
from app.graphql.pos import mutations
from app.graphql.pos.types import CreateSaleInput, SaleLineInputType, SalePaymentInputType
from app.models import Account, MembershipSubscription, Payment, Reservation, StandingBooking
from app.models.posModel import Sale
from app.security.hashing import hash_password
from tests.special_event_factories import make_account
from tests.test_day_passes import scenario
from tests.test_membership_coverage import group


@pytest.mark.parametrize("status", ["scheduled", "completed"])
async def test_staff_can_record_payment_after_class_and_pass_is_already_expired(db, status):
    sc = await scenario(db)
    sc.session.start_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sc.session.end_at = sc.session.start_at + timedelta(hours=1)
    sc.session.status = status
    await db.flush()
    sale = await create_sale(db, line_items=[SaleLineInput(
        "membership_renewal", member_id=sc.person.id, plan_id=sc.plan.id, session_id=sc.session.id,
    )], tenders=[SalePaymentInput("card", 50)], commit=False)
    sub = await db.get(MembershipSubscription, sale.line_items[0].subscription_id)
    assert sub.status == "expired"
    assert sub.start_at == sc.session.start_at and sub.end_at == sc.session.end_at
    reservation = await db.scalar(select(Reservation).where(Reservation.subscription_id == sub.id))
    assert reservation.session_id == sc.session.id
    assert (await db.get(Payment, sale.line_items[0].payment_id)).status == "COMPLETED"


@pytest.mark.parametrize("status", ["canceled", "displaced"])
async def test_staff_cannot_sell_canceled_or_displaced_classes(db, status):
    sc = await scenario(db)
    sc.session.status = status
    await db.flush()
    with pytest.raises(ValueError, match="disponible"):
        await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id,
                                session_id=sc.session.id, allow_past_session=True)
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0


async def test_public_purchase_still_rejects_past_classes(db):
    sc = await scenario(db)
    sc.session.start_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sc.session.end_at = sc.session.start_at + timedelta(hours=1)
    await db.flush()
    with pytest.raises(ValueError, match="disponible"):
        await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id, session_id=sc.session.id)


async def test_chatbot_does_not_offer_payment_link_for_overlap_requiring_admin(db):
    from app.services.chatbot.tools import ChatbotContext, build_tools
    from tests.special_event_factories import make_subscription
    sc = await scenario(db)
    sub = await make_subscription(db, sc.person)
    sub.start_at, sub.end_at = sc.session.start_at, sc.session.end_at
    await db.flush()
    ctx = ChatbotContext(db, 463, sc.person.id, None)
    tool = next(t for t in build_tools(ctx) if t.name == "propose_day_pass")
    result = await tool.ainvoke({"plan_id": sc.plan.id, "session_id": sc.session.id})
    assert "recepción" in result and "no generes otro enlace" in result
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.parametrize("admin_active", [False, True])
async def test_password_from_inactive_admin_or_non_admin_cannot_authorize(db, admin_active):
    sc = await scenario(db)
    await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id, session_id=sc.session.id)
    if admin_active:
        account = Account(person_id=sc.person.id, username=f"member-{sc.person.id}",
                          password_hash=hash_password("correct-password"), is_active=True)
        db.add(account)
    else:
        account = await make_account(db)
        account.password_hash = hash_password("correct-password")
        account.is_active = False
    await db.flush()
    with pytest.raises(OverlapAuthorizationError) as error:
        await authorize_overlap(db, sc.person.id, sc.session.start_at, sc.session.end_at, "correct-password")
    assert error.value.code == "ADMIN_PASSWORD_INVALID"


async def test_backdated_fixed_renewal_includes_completed_class(db):
    from app.core.timezones import local_date, local_datetime
    from tests.special_event_factories import make_person, make_session
    sc = await group(db, days=7)
    day = local_date(datetime.now(timezone.utc)) - timedelta(days=2)
    template = sc.templates[day.weekday()]
    session = await make_session(db, template, day)
    session.status = "completed"
    person = await make_person(db, "Late weekly payment")
    await db.flush()
    sub, *_ = await renew_subscription_with_standing_booking(
        db, person.id, sc.plan.id, template_id=template.id,
        start_at=local_datetime(day, datetime.min.time()), allow_past_session=True, commit=False,
    )
    reservation = await db.scalar(select(Reservation).where(
        Reservation.person_id == person.id, Reservation.session_id == session.id,
    ))
    assert reservation is not None and reservation.subscription_id == sub.id


async def test_overlap_requires_real_admin_password_and_reuses_reservations(db):
    sc = await group(db)
    person, old, *_ = await create_member_enrollment_with_standing_booking(
        db, "Existing member", sc.plan.id, template_id=sc.templates[0].id, start_at=sc.start, commit=False,
    )
    admin = await make_account(db)
    admin.password_hash = hash_password("correct-password")
    await db.flush()
    args = dict(member_id=person.id, plan_id=sc.plan.id, template_id=sc.templates[0].id,
                start_at=sc.start + timedelta(days=1), commit=False)
    for password, code in [(None, "MEMBERSHIP_OVERLAP"), ("wrong", "ADMIN_PASSWORD_INVALID")]:
        with pytest.raises(OverlapAuthorizationError) as error:
            await renew_subscription_with_standing_booking(db, **args, admin_password=password)
        assert error.value.code == code
    assert await db.scalar(select(func.count()).select_from(MembershipSubscription)) == 1
    new, _, _, _, stats = await renew_subscription_with_standing_booking(db, **args, admin_password="correct-password")
    assert new.id != old.id and old.status == "active"
    assert stats["overlap_authorized_by"] == admin.id and stats["skipped_existing"] > 0
    rows = (await db.execute(select(Reservation.session_id).where(Reservation.person_id == person.id))).scalars().all()
    assert len(rows) == len(set(rows))
    assert await db.scalar(select(func.count()).select_from(StandingBooking).where(StandingBooking.subscription_id == new.id)) == 5


async def test_existing_place_can_be_paid_even_when_class_is_full(db):
    sc = await scenario(db, bikes=1)
    first = await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id, session_id=sc.session.id)
    first.reservation.status = "checked_in"
    first.reservation.checkin_at = datetime.now(timezone.utc)
    admin = await make_account(db)
    admin.password_hash = hash_password("correct-password")
    await db.flush()
    rows = await get_available_sessions(db, start_date=sc.session.start_at, end_date=sc.session.end_at, person_id=sc.person.id)
    assert rows[0].available_spots == 0 and rows[0].has_reservation
    with pytest.raises(OverlapAuthorizationError):
        await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id, session_id=sc.session.id)
    second = await purchase_day_pass(db, plan_id=sc.plan.id, person_id=sc.person.id, session_id=sc.session.id,
                                     admin_password="correct-password")
    assert second.reservation.id == first.reservation.id
    assert second.reservation.seat_id == first.reservation.seat_id
    assert second.reservation.status == "checked_in" and second.reservation.checkin_at is not None
    assert await db.scalar(select(func.count()).select_from(Reservation)) == 1


async def test_pos_rolls_back_before_admin_prompt_and_records_approval_on_retry(db, monkeypatch):
    sc = await group(db)
    person, old, *_ = await create_member_enrollment_with_standing_booking(
        db, "POS existing", sc.plan.id, template_id=sc.templates[0].id, start_at=sc.start, commit=False,
    )
    admin = await make_account(db)
    admin.password_hash = hash_password("correct-password")
    member_id, admin_id, plan_id, template_id = person.id, admin.id, sc.plan.id, sc.templates[0].id
    await db.commit()
    async def permitted(*_):
        return None
    monkeypatch.setattr(mutations, "require_capability", permitted)
    monkeypatch.setattr(mutations, "_schedule_sale_renewal_confirmations", lambda sale: None)
    info = SimpleNamespace(context=SimpleNamespace(db=db, account_id=admin_id))
    data = CreateSaleInput(person_id=member_id, line_items=[SaleLineInputType(
        line_type="membership_renewal", plan_id=plan_id, member_id=member_id,
        template_id=template_id, start_at=sc.start + timedelta(days=1),
    )], payments=[SalePaymentInputType(method="card", amount=600)])
    result = await mutations.PosMutation().create_sale(info, data)
    assert not result.success and result.error_code == "MEMBERSHIP_OVERLAP"
    assert await db.scalar(select(func.count()).select_from(Sale)) == 0
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1
    data.admin_password = "correct-password"
    result = await mutations.PosMutation().create_sale(info, data)
    assert result.success
    sale = await get_sale(db, result.sale.id)
    assert sale.line_items[0].meta["overlap_authorized_by"] == admin_id
    assert "correct-password" not in str(sale.line_items[0].meta)
