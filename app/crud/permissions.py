"""Capability-based authorization helpers.

Authorization is role-based with an optional per-role capability grant layer.
The ``admin`` role is an implicit super-user: it always has every capability and
its grants cannot be revoked. Other roles gain a capability only when an explicit
row exists in ``role_capabilities``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import People, Role, RoleCapability

# --- Capability registry (used by the settings UI to render the matrix) ----
MANAGE_MEMBERSHIP_PLANS = "manage_membership_plans"
MANAGE_OWNER_AGENT = "manage_owner_agent"
MANAGE_USERS = "manage_users"
# POS / caja capabilities
OPERATE_POS = "operate_pos"  # run the checkout (create sales)
MANAGE_CASH_SESSION = "manage_cash_session"  # open/close caja, corte, cash movements
VIEW_POS_REPORTS = "view_pos_reports"  # read sales + cash-session reports
MANAGE_PRODUCTS = "manage_products"  # CRUD of the product catalog / inventory
ALL_CAPABILITIES: List[str] = [
    MANAGE_MEMBERSHIP_PLANS,
    MANAGE_OWNER_AGENT,
    MANAGE_USERS,
    OPERATE_POS,
    MANAGE_CASH_SESSION,
    VIEW_POS_REPORTS,
    MANAGE_PRODUCTS,
]

ADMIN_ROLE_CODE = "admin"


def _person_role_codes(person: Optional[People]) -> Set[str]:
    if not person or not getattr(person, "roles", None):
        return set()
    return {pr.role.code for pr in person.roles if pr.role}


def _person_role_ids(person: Optional[People]) -> Set[int]:
    if not person or not getattr(person, "roles", None):
        return set()
    return {pr.role_id for pr in person.roles if pr.role_id is not None}


async def get_capabilities_for_person(db: AsyncSession, person: Optional[People]) -> Set[str]:
    """Effective capability set for a person. Admin implies all capabilities."""
    role_codes = _person_role_codes(person)
    if ADMIN_ROLE_CODE in role_codes:
        return set(ALL_CAPABILITIES)

    role_ids = _person_role_ids(person)
    if not role_ids:
        return set()

    result = await db.execute(
        select(RoleCapability.capability).where(RoleCapability.role_id.in_(role_ids))
    )
    return set(result.scalars().all())


async def person_can(db: AsyncSession, person: Optional[People], capability: str) -> bool:
    """Authoritative check: does this person have the given capability?"""
    if person is None:
        return False
    if ADMIN_ROLE_CODE in _person_role_codes(person):
        return True
    caps = await get_capabilities_for_person(db, person)
    return capability in caps


async def get_capabilities_for_person_id(db: AsyncSession, person_id: Any) -> Set[str]:
    """Load the person (with roles) and return their effective capabilities."""
    from app.crud.usersCrud import get_person_by_id

    person = await get_person_by_id(db, person_id)
    return await get_capabilities_for_person(db, person)


async def list_role_capabilities(db: AsyncSession) -> List[Dict[str, Any]]:
    """Return every role with its granted capabilities (admin shown with all)."""
    roles = (await db.execute(select(Role).order_by(Role.code))).scalars().all()
    grants = (await db.execute(select(RoleCapability))).scalars().all()

    by_role: Dict[int, Set[str]] = {}
    for grant in grants:
        by_role.setdefault(grant.role_id, set()).add(grant.capability)

    out: List[Dict[str, Any]] = []
    for role in roles:
        if role.code == ADMIN_ROLE_CODE:
            caps = set(ALL_CAPABILITIES)
            locked = True
        else:
            caps = by_role.get(role.id, set())
            locked = False
        out.append(
            {
                "role_code": role.code,
                "role_description": role.description,
                "capabilities": sorted(caps),
                "locked": locked,
            }
        )
    return out


async def grant_role_capability(db: AsyncSession, role_code: str, capability: str) -> bool:
    """Grant a capability to a role (idempotent)."""
    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"Capacidad desconocida: {capability}")

    role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
    if not role:
        raise ValueError(f"Rol desconocido: {role_code}")

    existing = (
        await db.execute(
            select(RoleCapability).where(
                RoleCapability.role_id == role.id,
                RoleCapability.capability == capability,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(RoleCapability(role_id=role.id, capability=capability))
        await db.commit()
    return True


async def revoke_role_capability(db: AsyncSession, role_code: str, capability: str) -> bool:
    """Revoke a capability from a role. The admin role cannot be revoked."""
    if role_code == ADMIN_ROLE_CODE:
        raise ValueError("No se pueden revocar capacidades al rol admin")

    role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
    if not role:
        raise ValueError(f"Rol desconocido: {role_code}")

    await db.execute(
        delete(RoleCapability).where(
            RoleCapability.role_id == role.id,
            RoleCapability.capability == capability,
        )
    )
    await db.commit()
    return True
