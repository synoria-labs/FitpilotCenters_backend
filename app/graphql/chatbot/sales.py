"""Staff-only visibility and resolution of paid orders; no customer identity arguments."""
from datetime import date, datetime
from typing import Optional, List
import strawberry
from strawberry.types import Info
from sqlalchemy import select
from app.crud.permissions import MANAGE_SUBSCRIPTIONS
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.models.chatbotSalesModel import MembershipPurchaseOrder as Order


@strawberry.type
class MembershipPurchaseOrderType:
    id: int
    conversation_id: int
    status: str
    participant_name: str
    plan_name: str
    amount: float
    start_date: date
    payment_id: Optional[int]
    subscription_id: Optional[int]
    approved_at: Optional[datetime]
    review_reason: Optional[str]
    action_type: str
    external_reference: str
    provider_payment_id: Optional[str]

    @classmethod
    def from_model(cls, order, names=None, payments=None):
        return cls(id=order.id, conversation_id=order.conversation_id, status=order.status,
            participant_name=order.payload.get("full_name") or (names or {}).get(order.member_id, "Socio registrado"), plan_name=order.facts["plan_name"],
            amount=float(order.amount), start_date=order.start_date, payment_id=order.payment_id,
            subscription_id=order.subscription_id, approved_at=order.approved_at, review_reason=order.review_reason,
            action_type=order.action_type, external_reference=order.external_reference,
            provider_payment_id=(payments or {}).get(order.payment_id) or order.facts.get("payment_review", {}).get("id"))


@strawberry.type
class PurchaseStartOption:
    session_id: int
    label: str


@strawberry.type
class ConfirmPurchaseStartResult:
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


@strawberry.type
class MembershipSalesQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def membership_purchase_start_options(self, info: Info, order_id: int, start_date: date) -> List[PurchaseStartOption]:
        if await require_capability(info, MANAGE_SUBSCRIPTIONS):
            return []
        from app.models import ClassSession
        from app.core.timezones import local_datetime
        from datetime import time, timedelta
        from app.crud.reservationsCrud import get_available_sessions
        from app.services.chatbot.timefmt import fmt_dt
        order = await info.context.db.get(Order, order_id)
        original = await info.context.db.get(ClassSession, order.payload.get("session_id")) if order and order.payload.get("session_id") else None
        if original is None:
            return []
        start = local_datetime(start_date, time.min)
        sessions = await get_available_sessions(info.context.db, start_date=start, end_date=start + timedelta(days=1))
        candidates = []
        for session in sessions:
            row = await info.context.db.get(ClassSession, session.id)
            if row and row.venue_id == original.venue_id and row.class_type_id == original.class_type_id and session.available_spots > 0:
                candidates.append(PurchaseStartOption(session_id=session.id, label=fmt_dt(row.start_at)))
        return candidates

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def membership_purchase_orders(self, info: Info, limit: int = 100) -> List[MembershipPurchaseOrderType]:
        if await require_capability(info, MANAGE_SUBSCRIPTIONS):
            return []
        orders = (await info.context.db.scalars(select(Order).where(
            Order.status.in_(("paid", "paid_awaiting_start", "needs_review"))).order_by(Order.id.desc()).limit(max(1, min(limit, 200))))).all()
        from app.models import People, Payment
        ids = [o.member_id for o in orders if o.member_id]
        names = dict((await info.context.db.execute(select(People.id, People.full_name).where(People.id.in_(ids)))).all()) if ids else {}
        payment_ids = [o.payment_id for o in orders if o.payment_id]
        payments = dict((await info.context.db.execute(select(Payment.id, Payment.provider_payment_id).where(Payment.id.in_(payment_ids)))).all()) if payment_ids else {}
        return [MembershipPurchaseOrderType.from_model(order, names, payments) for order in orders]


@strawberry.type
class MembershipSalesMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def confirm_membership_purchase_start(self, info: Info, order_id: int, start_date: date,
                                                session_id: Optional[int] = None) -> ConfirmPurchaseStartResult:
        if error := await require_capability(info, MANAGE_SUBSCRIPTIONS):
            return ConfirmPurchaseStartResult(success=False, error=error)
        from app.services.chatbot.sales import confirm_start
        try:
            message = await confirm_start(info.context.db, order_id, start_date, session_id=session_id,
                                          actor_id=info.context.user.id)
            return ConfirmPurchaseStartResult(success=True, message=message)
        except ValueError as exc:
            await info.context.db.rollback()
            return ConfirmPurchaseStartResult(success=False, error=str(exc))
