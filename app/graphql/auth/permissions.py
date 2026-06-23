from typing import Optional

import strawberry
from strawberry.types import Info
from strawberry.permission import BasePermission
# from app.graphql.schema import Context

class IsAuthenticated(BasePermission):
    message = "Authentication required."

    def has_permission(self, source, info: Info, **kwargs):
        return bool(info.context.user)


def _context_role_codes(info: Info) -> set:
    """Role codes for the authenticated user loaded into the GraphQL context."""
    user = getattr(info.context, "user", None)
    if user is None or not getattr(user, "roles", None):
        return set()
    return {pr.role.code for pr in user.roles if pr.role}


async def require_admin(info: Info) -> Optional[str]:
    """Return an error message if the requester is not an admin, else None."""
    user = getattr(info.context, "user", None)
    if user is None:
        return "Acceso no autorizado"
    if "admin" not in _context_role_codes(info):
        return "Se requiere rol de administrador"
    return None


async def require_capability(info: Info, capability: str) -> Optional[str]:
    """Return an error message if the requester lacks ``capability``, else None.

    This is the authoritative check: it reads roles/grants from the database
    via the context user, so it does not trust any capability claim baked into
    the access token.
    """
    user = getattr(info.context, "user", None)
    if user is None:
        return "Acceso no autorizado"

    from app.crud.permissions import person_can

    db = info.context.db
    if not await person_can(db, user, capability):
        return "No tienes permiso para realizar esta accion"
    return None


async def require_step_up_proof(info: Info, proof: Optional[str]) -> Optional[str]:
    """Validate+consume a step-up (2-step) proof for a sensitive action.

    No-op when step-up is disabled (so callers behave as before until the shared
    verification service is live). When enabled, the proof is validated and
    single-use-consumed via the verification service, and bound to one of the
    authenticated account's own contacts (phone/email).
    """
    from app.core.verification_config import step_up_enabled, VerificationConfig

    if not step_up_enabled():
        return None

    user = getattr(info.context, "user", None)
    account_id = getattr(info.context, "account_id", None)
    if user is None or not account_id:
        return "Acceso no autorizado"
    if not proof:
        return "Se requiere verificación de 2 pasos"

    from app.services import verification_client as vc
    from app.crud.usersCrud import get_user_by_account_id

    result = await vc.consume_proof(proof, vc.PURPOSE_STEP_UP, VerificationConfig.AUDIENCE)
    if not result.get("valid"):
        return "Verificación de 2 pasos inválida o expirada"

    destination = result.get("destination")
    if destination:
        account = await get_user_by_account_id(info.context.db, account_id)
        person = account.person if account else None
        contacts = {
            c for c in (
                getattr(person, "phone_number", None),
                getattr(person, "email", None),
            ) if c
        }
        if destination not in contacts:
            return "La verificación no corresponde a tu cuenta"
    return None


async def require_any_capability(info: Info, capabilities) -> Optional[str]:
    """Return None if the requester holds ANY of ``capabilities``, else an error message.

    Authoritative DB-backed check (admin is an implicit super-user). Used where a
    screen is reachable by more than one role, e.g. the Caja tab (manage_cash_session)
    also needs to read its live corte (view_pos_reports).
    """
    user = getattr(info.context, "user", None)
    if user is None:
        return "Acceso no autorizado"

    from app.crud.permissions import person_can

    db = info.context.db
    for capability in capabilities:
        if await person_can(db, user, capability):
            return None
    return "No tienes permiso para realizar esta accion"
