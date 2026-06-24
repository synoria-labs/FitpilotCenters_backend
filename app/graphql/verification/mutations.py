import strawberry
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.core.verification_config import VerificationConfig, step_up_enabled
from app.crud.usersCrud import get_user_by_account_id
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.verification.types import StepUpRequestResponse, StepUpVerifyResponse
from app.services import verification_client as vc

CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"
SUPPORTED_CHANNELS = (CHANNEL_SMS, CHANNEL_EMAIL)


def _mask(destination: str, channel: str) -> str:
    """Mask a phone/email for display (no full PII back to the client)."""
    if not destination:
        return ""
    if channel == CHANNEL_EMAIL and "@" in destination:
        name, _, domain = destination.partition("@")
        head = name[:2]
        return f"{head}{'*' * max(1, len(name) - 2)}@{domain}"
    # phone: keep last 4
    tail = destination[-4:]
    return f"{'*' * max(1, len(destination) - 4)}{tail}"


@strawberry.type
class StepUpMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def request_step_up_verification(self, info: Info, channel: str) -> StepUpRequestResponse:
        """Send a step-up code to the authenticated user's own phone/email."""
        if not step_up_enabled():
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=None,
                masked_destination=None, next_cooldown_seconds=None,
                message="La verificación de 2 pasos no está disponible",
            )

        db: AsyncSession = info.context.db
        account_id = getattr(info.context, "account_id", None)
        if not account_id:
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=None,
                masked_destination=None, next_cooldown_seconds=None,
                message="Acceso no autorizado",
            )

        channel = (channel or "").strip().lower()
        if channel not in SUPPORTED_CHANNELS:
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=None,
                masked_destination=None, next_cooldown_seconds=None,
                message="Canal inválido (usa 'sms' o 'email')",
            )

        if not VerificationConfig.channel_allowed(channel):
            allowed = ", ".join(VerificationConfig.ALLOWED_CHANNELS) or "ninguno"
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=channel,
                masked_destination=None, next_cooldown_seconds=None,
                message=f"Canal no disponible; usa {allowed}",
            )

        account = await get_user_by_account_id(db, account_id)
        person = account.person if account else None
        destination = None
        if person is not None:
            destination = person.phone_number if channel == CHANNEL_SMS else person.email
        if not destination:
            label = "teléfono" if channel == CHANNEL_SMS else "correo"
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=channel,
                masked_destination=None, next_cooldown_seconds=None,
                message=f"Tu cuenta no tiene {label} registrado",
            )

        try:
            res = await vc.request_verification(
                channel, destination, vc.PURPOSE_STEP_UP,
                client_session_id=getattr(info.context, "session_id", None),
            )
        except vc.VerificationServiceError as exc:
            return StepUpRequestResponse(
                success=False, verification_id=None, channel=channel,
                masked_destination=None, next_cooldown_seconds=None,
                message=f"No se pudo enviar el código: {exc}",
            )

        return StepUpRequestResponse(
            success=True,
            verification_id=str(res.get("verificationId") or res.get("verification_id") or ""),
            channel=channel,
            masked_destination=res.get("maskedDestination") or _mask(destination, channel),
            next_cooldown_seconds=res.get("nextCooldownSeconds"),
            message="Código enviado",
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def verify_step_up(self, info: Info, verification_id: str, code: str) -> StepUpVerifyResponse:
        """Check the code; on success return a single-use proof for a sensitive action."""
        if not step_up_enabled():
            return StepUpVerifyResponse(success=False, proof=None, message="La verificación de 2 pasos no está disponible")

        if not (verification_id or "").strip() or not (code or "").strip():
            return StepUpVerifyResponse(success=False, proof=None, message="Código o verificación inválidos")

        try:
            res = await vc.check_verification(verification_id, code)
        except vc.VerificationServiceError as exc:
            return StepUpVerifyResponse(success=False, proof=None, message=f"No se pudo verificar: {exc}")

        proof = res.get("proof")
        if not proof:
            return StepUpVerifyResponse(success=False, proof=None, message="Código incorrecto o expirado")

        return StepUpVerifyResponse(success=True, proof=proof, message="Verificación exitosa")
