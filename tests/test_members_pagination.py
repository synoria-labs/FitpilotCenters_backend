from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import uuid

from sqlalchemy import select

from app.graphql.schema import schema
from app.models import MembershipPlan, MembershipSubscription, People, PersonRole, Role


async def _member_role(db) -> Role:
    role = (await db.execute(select(Role).where(Role.code == "member"))).scalar_one_or_none()
    if role is not None:
        return role

    role = Role(code="member", description="Socio")
    db.add(role)
    await db.flush()
    return role


async def _plan(db, token: str) -> MembershipPlan:
    plan = MembershipPlan(
        name=f"{token} Plan",
        description="Pagination test plan",
        price=Decimal("100.00"),
        duration_value=1,
        duration_unit="month",
        class_limit=None,
        plan_type="flexible",
        fixed_time_slot=False,
        is_active=True,
    )
    db.add(plan)
    await db.flush()
    return plan


async def _make_member(
    db,
    *,
    role: Role,
    plan: MembershipPlan,
    token: str,
    index: int,
    name: str | None = None,
    end_days: int = 30,
    now: datetime | None = None,
    end_at: datetime | None = None,
) -> People:
    now = now or datetime.now(timezone.utc)
    subscription_end = end_at or now + timedelta(days=end_days)
    person = People(
        full_name=name or f"{token} Member {index:03d}",
        email=f"{token.lower()}-{index}@example.com",
        phone_number=f"555000{index:04d}",
    )
    db.add(person)
    await db.flush()

    db.add(PersonRole(person_id=person.id, role_id=role.id, created_at=now))
    db.add(
        MembershipSubscription(
            person_id=person.id,
            plan_id=plan.id,
            start_at=now - timedelta(days=1),
            end_at=subscription_end,
            status="active",
        )
    )
    await db.flush()
    return person


async def _members_page(db, *, limit: int, offset: int, search: str):
    result = await schema.execute(
        """
        query MembersPage($limit: Int!, $offset: Int!, $search: String) {
            membersPage(limit: $limit, offset: $offset, search: $search) {
                total
                items {
                    id
                    fullName
                    activeMembership {
                        status
                        remainingDays
                    }
                }
            }
        }
        """,
        variable_values={"limit": limit, "offset": offset, "search": search},
        context_value=SimpleNamespace(db=db, user=object()),
    )
    assert result.errors is None
    return result.data["membersPage"]


async def test_members_page_returns_items_total_limit_offset_and_search(db):
    token = f"PageSearch{uuid.uuid4().hex[:8]}"
    role = await _member_role(db)
    plan = await _plan(db, token)

    await _make_member(db, role=role, plan=plan, token=token, index=1, end_days=30)
    middle = await _make_member(db, role=role, plan=plan, token=token, index=2, end_days=20)
    await _make_member(db, role=role, plan=plan, token=token, index=3, end_days=10)

    page = await _members_page(db, limit=1, offset=1, search=token)

    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == middle.id
    assert page["items"][0]["fullName"] == middle.full_name


async def test_members_page_order_is_stable_after_first_100_rows(db):
    token = f"PageTie{uuid.uuid4().hex[:8]}"
    role = await _member_role(db)
    plan = await _plan(db, token)
    fixed_now = datetime.now(timezone.utc)
    fixed_end = fixed_now + timedelta(days=30)

    created = [
        await _make_member(
            db,
            role=role,
            plan=plan,
            token=token,
            index=index,
            name=f"{token} Same Name",
            now=fixed_now,
            end_at=fixed_end,
        )
        for index in range(105)
    ]

    first_page = await _members_page(db, limit=100, offset=0, search=token)
    second_page = await _members_page(db, limit=100, offset=100, search=token)

    expected_ids = [person.id for person in sorted(created, key=lambda person: person.id)]
    actual_ids = [item["id"] for item in first_page["items"] + second_page["items"]]

    assert first_page["total"] == 105
    assert second_page["total"] == 105
    assert len(first_page["items"]) == 100
    assert len(second_page["items"]) == 5
    assert actual_ids == expected_ids
