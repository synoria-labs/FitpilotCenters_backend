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
