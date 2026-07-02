"""Seed member/finance/messaging capabilities and default staff grants.

Backfills grants so that enforcing require_capability on the previously
IsAuthenticated-only resolvers (members, payments, subscriptions, dashboard,
chats, campaigns) does not lock out the roles that already use those screens:

- admin: seeded for display only (admin is an implicit super-user in code).
- staff (front desk): granted the day-to-day capabilities it already exercises
  (payments, subscriptions, viewing members and chats).

The two more sensitive capabilities — send_campaigns and view_finances — are
intentionally NOT granted to staff here; an admin can toggle them per role from
the in-app permissions matrix.

Revision ID: f8b1c2d3e4a5
Revises: b2c3d4e5f6a8
Create Date: 2026-07-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f8b1c2d3e4a5"
down_revision: Union[str, None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

NEW_CAPABILITIES = (
    "manage_payments",
    "manage_subscriptions",
    "send_campaigns",
    "view_members",
    "view_finances",
    "view_chats",
)

# Front-desk defaults so staff keeps working after gating (admin toggles the rest).
STAFF_DEFAULT_CAPABILITIES = (
    "manage_payments",
    "manage_subscriptions",
    "view_members",
    "view_chats",
)


def _seed(role_code: str, capabilities: Sequence[str]) -> None:
    values = ", ".join(f"('{cap}')" for cap in capabilities)
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.role_capabilities (role_id, capability)
        SELECT r.id, c.capability
        FROM {SCHEMA}.roles r
        CROSS JOIN (VALUES {values}) AS c(capability)
        WHERE r.code = '{role_code}'
        ON CONFLICT DO NOTHING
        """
    )


def upgrade() -> None:
    # Admin grants exist only so every capability shows in the settings matrix;
    # admin is an implicit super-user in code regardless of these rows.
    _seed("admin", NEW_CAPABILITIES)
    # Staff keeps the front-desk capabilities it already used pre-gating.
    _seed("staff", STAFF_DEFAULT_CAPABILITIES)


def downgrade() -> None:
    caps = ", ".join(f"'{cap}'" for cap in NEW_CAPABILITIES)
    op.execute(
        f"DELETE FROM {SCHEMA}.role_capabilities WHERE capability IN ({caps})"
    )
