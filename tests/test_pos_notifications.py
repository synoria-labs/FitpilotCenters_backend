from types import SimpleNamespace

import pytest

from app.graphql.pos import mutations as pos_mutations
from app.graphql.pos.mutations import PosMutation
from app.models.notificationModel import EVENT_RENEWAL_CONFIRMATION


class _FakeDb:
    async def rollback(self):
        return None


def _sale(*line_items):
    return SimpleNamespace(
        id=77,
        person_id=42,
        line_items=list(line_items),
    )


def _sale_input(line_type="membership_renewal"):
    return SimpleNamespace(
        line_type=line_type,
        description="",
        quantity=1,
        unit_price=200,
        discount=0,
        plan_id=4,
        member_id=42,
        full_name=None,
        email=None,
        phone_number=None,
        start_at=None,
        template_id=2,
        seat_id=9,
        product_id=None,
    )


def _payment_input():
    return SimpleNamespace(
        method="cash",
        amount=200,
        provider=None,
        provider_payment_id=None,
        external_reference=None,
    )


@pytest.mark.asyncio
async def test_pos_schedules_renewal_confirmation_after_sale_commit(monkeypatch):
    order = []
    scheduled = []
    sale = _sale(
        SimpleNamespace(
            line_type="membership_renewal",
            subscription_id=901,
            meta={"person_id": 42},
        ),
        SimpleNamespace(line_type="product", subscription_id=None, meta=None),
    )

    async def fake_require_capability(*_args, **_kwargs):
        return None

    async def fake_create_sale(*_args, **_kwargs):
        order.append("sale_committed")
        return sale

    def fake_dispatch(event_type, **kwargs):
        return event_type, kwargs

    def fake_create_task(awaitable):
        order.append("notification_scheduled")
        scheduled.append(awaitable)
        return SimpleNamespace()

    monkeypatch.setattr(pos_mutations, "require_capability", fake_require_capability)
    monkeypatch.setattr(pos_mutations, "create_sale", fake_create_sale)
    monkeypatch.setattr(pos_mutations, "dispatch_event_in_background", fake_dispatch)
    monkeypatch.setattr(pos_mutations.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(pos_mutations.SaleType, "from_model", lambda _sale: _sale)

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb(), account_id=8))
    input_data = SimpleNamespace(
        line_items=[_sale_input(), _sale_input("product")],
        payments=[_payment_input()],
        person_id=42,
        note=None,
    )

    result = await PosMutation().create_sale(info, input_data)

    assert result.success is True
    assert order == ["sale_committed", "notification_scheduled"]
    assert scheduled == [
        (
            EVENT_RENEWAL_CONFIRMATION,
            {"person_id": 42, "subscription_id": 901},
        )
    ]


def test_pos_renewal_notification_falls_back_to_sale_person(monkeypatch):
    scheduled = []
    sale = _sale(
        SimpleNamespace(
            line_type="membership_renewal",
            subscription_id=902,
            meta={},
        )
    )

    monkeypatch.setattr(
        pos_mutations,
        "dispatch_event_in_background",
        lambda event_type, **kwargs: (event_type, kwargs),
    )
    monkeypatch.setattr(
        pos_mutations.asyncio,
        "create_task",
        lambda awaitable: scheduled.append(awaitable),
    )

    pos_mutations._schedule_sale_renewal_confirmations(sale)

    assert scheduled == [
        (
            EVENT_RENEWAL_CONFIRMATION,
            {"person_id": 42, "subscription_id": 902},
        )
    ]


@pytest.mark.asyncio
async def test_pos_notification_scheduling_failure_does_not_fail_sale(monkeypatch):
    sale = _sale(
        SimpleNamespace(
            line_type="membership_renewal",
            subscription_id=903,
            meta={"person_id": 42},
        )
    )

    async def fake_require_capability(*_args, **_kwargs):
        return None

    async def fake_create_sale(*_args, **_kwargs):
        return sale

    monkeypatch.setattr(pos_mutations, "require_capability", fake_require_capability)
    monkeypatch.setattr(pos_mutations, "create_sale", fake_create_sale)
    monkeypatch.setattr(
        pos_mutations,
        "dispatch_event_in_background",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        pos_mutations.asyncio,
        "create_task",
        lambda _awaitable: (_ for _ in ()).throw(RuntimeError("scheduler stopped")),
    )
    monkeypatch.setattr(pos_mutations.SaleType, "from_model", lambda _sale: _sale)

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb(), account_id=8))
    input_data = SimpleNamespace(
        line_items=[_sale_input()],
        payments=[_payment_input()],
        person_id=42,
        note=None,
    )

    result = await PosMutation().create_sale(info, input_data)

    assert result.success is True
