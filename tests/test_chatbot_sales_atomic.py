"""Separate database connections exercise real advisory locks and durable payment proof."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app.db.postgresql import SessionLocal
from app.models import ClassSession, ClassTemplate, ClassType, MembershipPlan, MembershipSubscription, Payment, People, PersonRole, Reservation, Seat, StandingBooking, Venue
from app.models.chatbotSalesModel import MembershipPurchaseOrder as Order
from app.services.chatbot import sales
from app.services.chatbot.tools import ChatbotContext
from tests.special_event_factories import make_person
from tests.test_chatbot_sales_v2 import approval
from tests.test_membership_coverage import group


@pytest.mark.parametrize("same_order", [True, False])
async def test_parallel_webhooks_apply_once_and_never_sell_the_last_bike_twice(monkeypatch, same_order):
    refund = AsyncMock(return_value={"status": "approved"})
    monkeypatch.setattr(sales.mp, "refund_payment", refund)
    async with SessionLocal() as setup:
        sc = await group(setup)
        people = [await make_person(setup, "Anonymous buyer") for _ in range(2)]
        person_ids = [p.id for p in people]
        for seat in sc.seats[1:]:
            seat.is_active = False
        for template in sc.templates:
            template.default_capacity = 1
        venue_id, type_id = sc.templates[0].venue_id, sc.templates[0].class_type_id
        template_ids, plan_id = [t.id for t in sc.templates], sc.plan.id
        day = sc.day + timedelta(days=1)
        orders = []
        for index, person in enumerate(people[:1] if same_order else people):
            ctx = ChatbotContext(setup, 91000 + index, person.id, None, sales_v2_enabled=True,
                user_text=f"Quiero empezar el {day.isoformat()}", message_id=1)
            await sales.propose(ctx, plan_id=plan_id, template_id=template_ids[index], start_date=day.isoformat())
            order = await sales.active_order(setup, ctx.conversation_id)
            order.status = "awaiting_payment"
            orders.append((order.id, order.external_reference, approval(order, day) | {"transaction_amount": float(order.amount)}))
        await setup.commit()

    async def process(item):
        async with SessionLocal() as connection:
            return await sales.approved_payment(connection, f"parallel-sale-{item[0]}", item[2])

    try:
        inputs = orders * 2 if same_order else orders
        results = await asyncio.wait_for(asyncio.gather(*(process(item) for item in inputs)), timeout=15)
        async with SessionLocal() as check:
            assert await check.scalar(select(func.count()).select_from(MembershipSubscription).where(MembershipSubscription.person_id.in_(person_ids))) == 1
            assert await check.scalar(select(func.count()).select_from(Payment).where(Payment.external_reference.in_([o[1] for o in orders]))) == len(orders)
            assert await check.scalar(select(func.count()).select_from(Reservation).where(Reservation.person_id.in_(person_ids))) == 5
            if same_order:
                assert sum(r["text"] is not None for r in results) == 1
                refund.assert_not_awaited()
            else:
                assert refund.await_count == 1
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(delete(Order).where(Order.id.in_([o[0] for o in orders])))
            await cleanup.execute(delete(Reservation).where(Reservation.person_id.in_(person_ids)))
            await cleanup.execute(delete(StandingBooking).where(StandingBooking.person_id.in_(person_ids)))
            await cleanup.execute(delete(Payment).where(Payment.external_reference.in_([o[1] for o in orders])))
            await cleanup.execute(delete(MembershipSubscription).where(MembershipSubscription.person_id.in_(person_ids)))
            await cleanup.execute(delete(PersonRole).where(PersonRole.person_id.in_(person_ids)))
            await cleanup.execute(delete(People).where(People.id.in_(person_ids)))
            await cleanup.execute(delete(ClassSession).where(ClassSession.template_id.in_(template_ids)))
            await cleanup.execute(delete(ClassTemplate).where(ClassTemplate.id.in_(template_ids)))
            await cleanup.execute(delete(Seat).where(Seat.venue_id == venue_id))
            await cleanup.execute(delete(Venue).where(Venue.id == venue_id))
            await cleanup.execute(delete(ClassType).where(ClassType.id == type_id))
            await cleanup.execute(delete(MembershipPlan).where(MembershipPlan.id == plan_id))
            await cleanup.commit()
