"""Live model comparison with anonymous cases and memory-only tools. No operational DB or payments."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import date, datetime, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import time
from functools import wraps

# Never inherit a deployment's database, even when this script is run inside its container.
driver = "asyncpg" if importlib.util.find_spec("asyncpg") else "psycopg"
os.environ["DATABASE_URL"] = f"postgresql+{driver}://unused:unused@127.0.0.1:1/evaluation_only"
os.environ.setdefault("SECRET_KEY_ACCESS_TOKEN", "evaluation-only-access-secret-at-least-32-characters")
os.environ.setdefault("SECRET_KEY_REFRESH_TOKEN", "evaluation-only-refresh-secret-at-least-32-characters")

from langchain_core.tools import StructuredTool
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.services.chatbot.agent import run_agent
from app.services.chatbot.agent import build_llm
from app.services.chatbot.tools import ChatbotContext, build_tools
from app.services.chatbot.verification import finalize_reply
from app.services.chatbot.intents import plain
from app.crud.memberships.calendar import period
from app.services.chatbot.sales import summary, stamp


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

CASES = [
    ("soraya_greeting", "Hola", "none"),
    ("karla_thanks", "Gracias", "confirmed"),
    ("yazmin_cash", "¿Podría pagar en efectivo?", "checkout"),
    ("ivonne_transfer", "¿Tendrán cuenta para hacer transferencia?", "checkout"),
    ("karla_start_missing", "Me interesa el semanal de las 9 am. ¿Cuándo empieza?", "none"),
    ("karla_chosen_start", "Quiero el semanal de las 9 am, empezar el 2026-10-27. Mi nombre es Cliente Prueba", "none"),
    ("karla_confirm", "Sí", "draft"),
    ("ana_luisa_month", "¿Mi mensual incluye solo los miércoles o todas las clases del horario?", "monthly"),
    ("late_payment", "Quiero comenzar el 2026-11-03", "paid_awaiting_start"),
    ("receipt_not_approval", "Envié un comprobante. ¿Ya quedó pagado?", "pending"),
]


def facts(day=date(2026, 10, 27), monthly=False):
    plan = SimpleNamespace(duration_unit="month" if monthly else "day", duration_value=1 if monthly else 7)
    start, end = period(plan, day)
    return dict(plan_id=3 if monthly else 4, plan_name="Plan Mensual" if monthly else "Plan Semanal",
        amount="600" if monthly else "200", start_at=start.isoformat(), end_at=end.isoformat(),
        schedule="Lunes a Viernes a las 09:00", included_classes=23 if monthly else 5)


async def trial(model, case, repetition):
    name, question, initial = case
    order = SimpleNamespace(id=10, conversation_id=1, member_id=None, action_type="buy_package",
        status={"checkout": "awaiting_payment", "monthly": "draft", "pending": "awaiting_payment"}.get(initial, initial),
        facts=facts(monthly=initial == "monthly"), payload={"plan_id": 3 if initial == "monthly" else 4},
        start_date=date(2026, 10, 27), first_class_at=datetime(2026, 10, 27, 15, tzinfo=timezone.utc),
        external_reference="evaluation-order", mp_preference_id="evaluation", review_reason=None,
        mp_init_point="https://example.invalid/evaluation-checkout" if initial in ("checkout", "pending") else None,
        payment_id=1 if initial in ("paid_awaiting_start", "confirmed") else None)
    current = [None if initial == "none" else order]
    plan = SimpleNamespace(id=4, name="Plan Semanal", price=Decimal(200), is_active=True)
    async def get(cls, key, **kwargs):
        return current[0] if cls.__name__ == "MembershipPurchaseOrder" else plan
    order.payload["full_name"] = "Cliente Prueba"
    db = SimpleNamespace(get=get, scalar=AsyncMock(side_effect=lambda *a, **k: current[0]), commit=AsyncMock(), execute=AsyncMock(side_effect=AssertionError("Operational DB access forbidden")))
    ctx = ChatbotContext(db, 1, None, None, sales_v2_enabled=True, offer_in_person_registration=True,
                         user_text=question, message_id=1)
    if current[0]:
        ctx.order_id = 10
    calls = []

    async def propose_membership(plan_id: int, template_id: int = 6, full_name: str = "Cliente Prueba", start_date: str = ""):
        if not start_date:
            return "¿Qué día quieres comenzar? Confirma la fecha completa antes del pago."
        if start_date != "2026-10-27":
            return "La fecha no coincide con la elegida por el cliente."
        order.status, order.facts = "draft", facts(date.fromisoformat(start_date))
        current[0] = order
        ctx.order_id = 10
        ctx.reply_request = dict(kind="membership_information", membership_order_id=10)
        calls.append("propose_membership")
        return summary(order.facts) + "\n¿Confirmas estos datos?"

    async def confirm_action():
        if not current[0] or current[0].status != "draft" or plain(question) not in ("si", "confirmo", "perfecto"):
            return "Falta una propuesta confirmada por el cliente."
        order.status, order.mp_init_point = "awaiting_payment", "https://example.invalid/evaluation-checkout"
        ctx.order_id = 10
        ctx.reply_request = dict(kind="purchase", membership_order_id=10)
        calls.append("confirm_action")
        return "Enlace listo; usa respond_to_customer(kind=purchase)."

    async def confirm_membership_start(start_date: str, session_id: int = None):
        if initial != "paid_awaiting_start" or start_date != "2026-11-03":
            return "No hay un pago pendiente de esa fecha."
        order.start_date, order.facts, order.status = date.fromisoformat(start_date), facts(date.fromisoformat(start_date)), "confirmed"
        ctx.order_id = 10
        ctx.reply_request = dict(kind="purchase", membership_order_id=10)
        calls.append("confirm_membership_start")
        return "Pago ya recibido; registro aplicado.\n" + summary(order.facts)

    async def respond_to_customer(kind: str, text: str = "", session_id: int = None, reservation_id: int = None,
                                  requested_date: str = None, order_id: int = None, plan_id: int = None,
                                  template_id: int = None, event_id: int = None, membership_order_id: int = None):
        calls.append("respond_to_customer:" + kind)
        return await finalize_reply(ctx, kind=kind, text=text, session_id=session_id, reservation_id=reservation_id,
            requested_date=requested_date, order_id=order_id, plan_id=plan_id, template_id=template_id,
            event_id=event_id, membership_order_id=membership_order_id)

    async def read_catalog():
        return "Plan Semanal id=4 $200, 7 días; Plan Mensual id=3 $600, un mes. Horario fijo lunes a viernes, 9 am, template_id=6."

    async def coverage(plan_id: int, template_id: int = None, start_date: str = None):
        return summary(order.facts) if current[0] else "El inicio lo elige el cliente. Pregunta la fecha antes de calcular la vigencia."

    async def purchase_status():
        if current[0] and current[0].payment_id:
            return "Pago COMPLETED acreditado. Falta confirmar inicio: usa confirm_membership_start sin generar otro cobro."
        ctx.checked_payment_reference, ctx.provider_payment_status = order.external_reference, "pending"
        return "Mercado Pago indica pending. No hay pago aprobado; no confirmes reservas ni pidas un segundo pago."

    replacements = {"propose_membership": propose_membership, "confirm_action": confirm_action,
        "confirm_membership_start": confirm_membership_start, "respond_to_customer": respond_to_customer,
        "get_membership_plans": read_catalog, "get_weekly_schedule": read_catalog,
        "get_membership_coverage": coverage, "get_my_purchase_status": purchase_status,
        "get_business_info": read_catalog}
    tools = []
    def audited(fn, name):
        @wraps(fn)
        async def invoke(*args, **kwargs):
            ctx.tools_used.append(name)
            return await fn(*args, **kwargs)
        return invoke
    for original in build_tools(ctx):
        if original.name in replacements:
            tools.append(StructuredTool.from_function(coroutine=audited(replacements[original.name], original.name), name=original.name, description=original.description))
    config = ChatbotConfigData(None, True, True, True, model,
        "Asistente de Love Fitness. Responde en español con claridad, sin repetir información. Usa datos de herramientas.",
        "Love Fitness", None, None, None, None, None, None, None, None, sales_v2_enabled=True, offer_in_person_registration=True)
    started = time.monotonic()
    error = None
    with ExitStack() as stack:
        def evaluation_llm(config):
            llm = build_llm(config)
            llm.max_retries = 0
            return llm
        stack.enter_context(patch("app.services.chatbot.agent.build_llm", side_effect=evaluation_llm))
        stack.enter_context(patch("app.services.chatbot.agent.fmt_now_es", return_value="lunes 26/10/2026 10:00"))
        stack.enter_context(patch("app.services.chatbot.verification.datetime", FixtureClock))
        stack.enter_context(patch("app.services.chatbot.sales.now", lambda: FixtureClock.now(timezone.utc)))
        stack.enter_context(patch("app.services.chatbot.sales.active_order", AsyncMock(side_effect=lambda *a, **k: current[0])))
        stack.enter_context(patch("app.crud.chatbotPendingCrud.get_active_pending", AsyncMock(return_value=None)))
        for operation in ("create_preference", "refund_payment", "expire_preference"):
            stack.enter_context(patch("app.services.mercadopago_service." + operation,
                AsyncMock(side_effect=AssertionError("Payment mutations forbidden in evaluation"))))
        stack.enter_context(patch("app.services.mercadopago_service.search_payments", AsyncMock(return_value=[{"status": "pending"}] if initial == "pending" else [])))
        try:
            await asyncio.wait_for(run_agent(config, tools, "Clases de spinning lunes a viernes a las 9 am.", None, [], question,
                pending_note=(f"Orden comercial #{order.id}, estado={order.status}. Nombre ya confirmado: Cliente Prueba. No lo pidas nuevamente.\n" + summary(order.facts)) if current[0] else None,
                telemetry=ctx.telemetry), timeout=120)
            if ctx.reply_request:
                await finalize_reply(ctx, **ctx.reply_request)
        except Exception as exc:
            error = type(exc).__name__
    reply = ctx.validated_reply or ""
    value = plain(reply)
    words = value.split()
    phrases = [tuple(words[i:i + 5]) for i in range(max(0, len(words) - 4))]
    checks = {
        "soraya_greeting": bool(reply) and "reserva" not in value,
        "karla_thanks": "nada" in value and "reserva" not in value,
        "yazmin_cash": "recepcion" in value,
        "ivonne_transfer": "transferencia" in value and bool(ctx.payment_link),
        "karla_start_missing": "dia" in value or "fecha" in value,
        "karla_chosen_start": "27/10/2026" in reply and "02/11/2026" in reply and "confirma" in value,
        "karla_confirm": bool(ctx.payment_link) and calls.count("confirm_action") == 1,
        "ana_luisa_month": "lunes a viernes" in value and "4 5" not in value,
        "late_payment": "03/11/2026" in reply and "confirm_membership_start" in calls,
        "receipt_not_approval": ("pendiente" in value or "proceso" in value) and "pago acreditado" not in value,
    }
    return dict(model=model, case=name, repetition=repetition, passed=bool(checks[name] and not error and len(reply.split()) <= 100),
        validation=ctx.verification_result, calls=calls, words=len(reply.split()), reply=reply,
        tools=ctx.tools_used,
        repeated_5gram_ratio=(len(phrases) - len(set(phrases))) / len(phrases) if phrases else 0,
        elapsed_ms=int((time.monotonic() - started) * 1000), usage=ctx.telemetry.get("usage"),
        effective_model=ctx.telemetry.get("effective_model"), error=error)


async def evaluate(args):
    results = []
    for model in args.models:
        for case in CASES:
            for repetition in range(1, args.repetitions + 1):
                print(json.dumps({"started": case[0], "model": model, "repetition": repetition}), flush=True)
                result = await trial(model, case, repetition)
                results.append(result)
                print(json.dumps({k: result[k] for k in ("model", "case", "repetition", "passed", "error")}), flush=True)
    report = {"operational_database": "disabled", "payment_mutations": "disabled", "results": results,
              "models": {m: {"passed": sum(r["passed"] for r in results if r["model"] == m),
                              "total": sum(r["model"] == m for r in results)} for m in args.models}}
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["models"]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=["claude-haiku-5-5", "claude-sonnet-5-5"])
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        parser.exit(2, "ANTHROPIC_API_KEY is required for a live comparison; no score was produced.\n")
    asyncio.run(evaluate(args))
