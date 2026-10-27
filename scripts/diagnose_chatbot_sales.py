"""Read-only historical diagnosis; never repairs or queues messages. Run as a module."""
import asyncio
import json
import re
from sqlalchemy import text
from app.db.postgresql import async_session_factory
from app.services.chatbot.verification import requested_day
from app.core.timezones import local_date


async def diagnose():
    async with async_session_factory() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        total = await db.scalar(text("SELECT count(*) FROM app.payments WHERE status='COMPLETED' AND comment ILIKE '%bot%'"))
        plans = (await db.execute(text("""SELECT id, name, plan_type FROM app.membership_plans
            WHERE is_active=true AND fixed_time_slot=true AND (name ILIKE '%ilimitad%' OR name ILIKE '%acceso libre%')"""))).mappings().all()
        purchases = (await db.execute(text("""SELECT p.id, p.paid_at, s.start_at, a.conversation_id
            FROM app.payments p JOIN app.membership_subscriptions s ON s.id=p.subscription_id
            JOIN app.chatbot_pending_action a ON a.external_reference=p.external_reference
            WHERE p.status='COMPLETED' AND p.comment ILIKE '%bot%' ORDER BY p.paid_at DESC LIMIT 200"""))).mappings().all()
        candidates = []
        for purchase in purchases:
            messages = (await db.execute(text("""SELECT text_content,timestamp FROM app.messages
                WHERE conversation_id=:conversation AND direction='inbound'
                AND timestamp BETWEEN (:paid_at AT TIME ZONE 'UTC') - interval '24 hours' AND (:paid_at AT TIME ZONE 'UTC')
                ORDER BY timestamp DESC LIMIT 20"""), {"conversation": purchase["conversation_id"], "paid_at": purchase["paid_at"]})).mappings().all()
            for message in messages:
                raw = message["text_content"] or ""
                if re.search(r"inici|empez|a partir", raw, re.I):
                    try:
                        chosen = requested_day(raw, message["timestamp"])
                    except ValueError:
                        chosen = None
                    if chosen and chosen != local_date(purchase["start_at"]):
                        candidates.append(dict(payment_id=purchase["id"], conversation_id=purchase["conversation_id"],
                            requested_start=chosen.isoformat(), actual_start=local_date(purchase["start_at"]).isoformat(),
                            disposition="Revisión administrativa; sin ajustes automáticos"))
                    break
        return {"mode": "read_only", "completed_bot_payments": total, "purchases_reviewed": len(purchases),
                "scope": "Up to 200 purchases with an exact surviving pending-action reference; not a complete historical reconstruction",
                "possible_start_mismatches": candidates,
                "plan_configuration_warnings": [dict(p) for p in plans]}


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print(json.dumps(asyncio.run(diagnose()), ensure_ascii=False, default=str))
