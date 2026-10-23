"""Read-only, conversation-bound purchase facts for payment screenshot support."""
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.mercadopago_config import mercadopago_config
from app.models import ClassSession, Payment
from app.models.chatbotModel import ChatbotPendingAction
from app.services import mercadopago_service
from app.services.chatbot.timefmt import fmt_dt

logger = logging.getLogger(__name__)
PROCESSING_STATUSES = {"approved", "pending", "in_process", "authorized", "in_mediation", "needs_review"}


async def purchase_status(ctx):
    pending = await ctx.db.scalar(select(ChatbotPendingAction).where(
        ChatbotPendingAction.conversation_id == ctx.conversation_id,
    ).execution_options(populate_existing=True))
    if pending is None:
        return "No hay una compra registrada en esta conversación. La captura por sí sola no demuestra un cobro ni una reserva."
    lines = [f"Compra de esta conversación, creada {fmt_dt(pending.created_at)}: estado local {pending.status}."]
    recorded = []
    if pending.external_reference:
        recorded = list((await ctx.db.execute(select(Payment.status).where(
            Payment.external_reference == pending.external_reference,
        ))).scalars().all())
    lines.append("Pagos registrados localmente: " + (", ".join(recorded) if recorded else "ninguno; esto no descarta un cargo pendiente del proveedor") + ".")
    statuses = []
    if pending.external_reference and mercadopago_config.is_configured():
        try:
            payments = await mercadopago_service.search_payments(pending.external_reference)
            statuses = [str(p.get("status")) for p in payments if p.get("status")]
            lines.append("Mercado Pago consultado ahora: " + (", ".join(statuses) if statuses else "sin pagos encontrados") + ".")
        except Exception as exc:  # noqa: BLE001 - still report the local facts
            logger.info("chatbot_payment_lookup conversation=%s error=%s", ctx.conversation_id, type(exc).__name__)
            lines.append("No fue posible consultar Mercado Pago ahora; no deduzcas si cobró a partir de la imagen.")
    elif pending.external_reference:
        lines.append("Mercado Pago no está disponible para consultar el estado externo.")
    protected = next((s for s in statuses if s == "approved"), None)
    protected = protected or next((s for s in statuses if s in PROCESSING_STATUSES), None)
    if not protected and "COMPLETED" in recorded:
        protected = "needs_review" if statuses else "approved"
    ctx.checked_payment_reference = pending.external_reference
    ctx.provider_payment_status = protected
    if protected:
        lines.append("No generes otro enlace ni solicites pagar de nuevo: hay un pago aprobado/en proceso o que requiere revisión. La reserva debe verificarse por separado.")
    session_id = (pending.payload or {}).get("session_id")
    if session_id:
        session = await ctx.db.get(ClassSession, int(session_id))
        if session is not None:
            lines.append(f"La compra corresponde a la clase del {fmt_dt(session.start_at)} ({session.status}).")
            if session.start_at <= datetime.now(timezone.utc) or session.status != "scheduled":
                lines.append("Esa clase ya no está disponible; no reenvíes su enlace ni la confirmes para hoy.")
    if pending.expires_at and pending.expires_at <= datetime.now(timezone.utc):
        lines.append("La propuesta está vencida; no reutilices su enlace. Verifica primero cargos y la nueva fecha solicitada.")
    lines.append("Una captura de error no confirma rechazo del cobro. Ninguna captura acredita pago ni confirma asistencia.")
    return "\n".join(lines)
