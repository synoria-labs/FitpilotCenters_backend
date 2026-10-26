"""Administrator approval for a purchase that overlaps a member's paid coverage."""
import asyncio
import logging

from sqlalchemy import select

from app.models import Account, ClassSession, MembershipSubscription, People, PersonRole, Reservation, Role
from app.security.hashing import verify_password

logger = logging.getLogger(__name__)


class OverlapAuthorizationError(ValueError):
    def __init__(self, message=None, *, invalid_password=False):
        self.code = "ADMIN_PASSWORD_INVALID" if invalid_password else "MEMBERSHIP_OVERLAP"
        super().__init__(message or (
            "La contraseña de administrador es incorrecta." if invalid_password else
            "Las fechas seleccionadas se solapan con una membresía o reserva del socio. "
            "Se requiere contraseña de administrador para continuar."
        ))


async def authorize_overlap(db, person_id, start_at, end_at, admin_password=None):
    """Return the approving account ID, or None when no approval is necessary.

    Callers serialize the purchase before checking. Passwords are checked here,
    never trusted as a boolean from the desktop, persisted, or included in logs.
    """
    subscription = await db.scalar(select(MembershipSubscription.id).where(
        MembershipSubscription.person_id == person_id,
        MembershipSubscription.status.in_(("active", "expired")),
        MembershipSubscription.start_at < end_at,
        MembershipSubscription.end_at > start_at,
    ).limit(1))
    reservation = await db.scalar(select(Reservation.id).join(ClassSession).where(
        Reservation.person_id == person_id,
        Reservation.status.in_(("reserved", "checked_in", "no_show")),
        ClassSession.start_at < end_at,
        ClassSession.end_at > start_at,
    ).limit(1))
    if subscription is None and reservation is None:
        return None
    if not admin_password:
        raise OverlapAuthorizationError()

    accounts = (await db.execute(select(Account).join(People).join(
        PersonRole, PersonRole.person_id == People.id,
    ).join(Role, Role.id == PersonRole.role_id).where(
        Account.is_active.is_(True), People.deleted_at.is_(None), Role.code == "admin",
    ).order_by(Account.id))).scalars().all()
    for account in accounts:
        try:
            valid = await asyncio.to_thread(verify_password, admin_password, account.password_hash)
        except (ValueError, TypeError):
            valid = False
        if valid:
            logger.info("Membership overlap authorized: member=%s admin_account=%s", person_id, account.id)
            return account.id
    raise OverlapAuthorizationError(invalid_password=True)
