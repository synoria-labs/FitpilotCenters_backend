from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.crud.chatbotPendingCrud import upsert_pending
from app.services.chatbot import purchase_status as service
from app.services.chatbot.tools import ChatbotContext, build_tools
from app.services.chatbot.verification import finalize_reply
from tests.test_day_passes import scenario


async def pending_case(db):
    sc = await scenario(db)
    await upsert_pending(db, conversation_id=601, action_type="buy_day_pass", member_id=sc.person.id,
        payload={"plan_id": sc.plan.id, "session_id": sc.session.id}, status="awaiting_payment",
        external_reference="chatbot-image-test", mp_init_point="https://www.mercadopago.com.mx/current-checkout")
    return sc, ChatbotContext(db, 601, sc.person.id, "test", user_text="Me aparece un error")


@pytest.mark.parametrize("status", ["approved", "pending", "in_process"])
async def test_screenshot_payment_lookup_blocks_another_link_while_payment_is_processing(db, monkeypatch, status):
    sc, ctx = await pending_case(db)
    monkeypatch.setattr(service.mercadopago_config, "is_configured", lambda: True)
    search = AsyncMock(return_value=[{"status": status, "payer": {"secret": "never forward this"}}])
    monkeypatch.setattr(service.mercadopago_service, "search_payments", search)
    tool = next(t for t in build_tools(ctx) if t.name == "get_my_purchase_status")
    facts = await tool.ainvoke({})
    assert status in facts and "never forward" not in facts
    search.assert_awaited_once_with("chatbot-image-test")
    assert ctx.provider_payment_status == status
    result = await finalize_reply(ctx, kind="purchase", text="Paga de nuevo")
    assert ctx.validated_reply is None and "No envíes otro enlace" in result


async def test_expired_image_checkout_is_not_reused_and_no_charge_is_inferred(db, monkeypatch):
    sc, ctx = await pending_case(db)
    pending = await db.scalar(select(service.ChatbotPendingAction).where(
        service.ChatbotPendingAction.conversation_id == 601))
    pending.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(service.mercadopago_config, "is_configured", lambda: True)
    monkeypatch.setattr(service.mercadopago_service, "search_payments", AsyncMock(return_value=[]))
    facts = await service.purchase_status(ctx)
    assert "vencida" in facts and "no confirma rechazo" in facts and "sin pagos encontrados" in facts
    assert "current-checkout" not in facts
    result = await finalize_reply(ctx, kind="information", text="Usa https://www.mercadopago.com.mx/old-link")
    assert ctx.validated_reply is None and "no reutilices" in result


async def test_payment_lookup_is_bound_to_current_conversation(db, monkeypatch):
    await pending_case(db)
    search = AsyncMock()
    monkeypatch.setattr(service.mercadopago_service, "search_payments", search)
    facts = await service.purchase_status(ChatbotContext(db, 602, None, None))
    assert "No hay una compra" in facts
    search.assert_not_awaited()


async def test_provider_unavailable_reports_uncertainty_without_inventing_a_charge(db, monkeypatch):
    _, ctx = await pending_case(db)
    monkeypatch.setattr(service.mercadopago_config, "is_configured", lambda: True)
    monkeypatch.setattr(service.mercadopago_service, "search_payments", AsyncMock(side_effect=RuntimeError("failed")))
    facts = await service.purchase_status(ctx)
    assert "No fue posible consultar" in facts and ctx.provider_payment_status is None


async def test_processing_checkout_cannot_be_replaced_by_a_new_preference(db, monkeypatch):
    from app.services.chatbot import tools as agent_tools
    sc, ctx = await pending_case(db)
    ctx.require_mp_payment = True
    ctx.checked_payment_reference = "chatbot-image-test"
    ctx.provider_payment_status = "pending"
    create = AsyncMock()
    monkeypatch.setattr(agent_tools.mercadopago_service, "create_preference", create)
    propose = next(t for t in build_tools(ctx) if t.name == "propose_day_pass")
    result = await propose.ainvoke({"plan_id": sc.plan.id, "session_id": sc.session.id})
    assert "No generes otro enlace" in result
    create.assert_not_awaited()
    pending = await db.scalar(select(service.ChatbotPendingAction).where(
        service.ChatbotPendingAction.conversation_id == ctx.conversation_id))
    assert pending.external_reference == "chatbot-image-test"
