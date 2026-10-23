"""End-to-end through the GraphQL schema: the admin flow the desktop screen drives."""
from __future__ import annotations

from types import SimpleNamespace

from app.graphql.schema import schema
from tests.special_event_factories import make_account, regular_class_scenario

_EVENT_FIELDS = """
    event {
        id status localDate localStartTime localEndTime scheduleLabel price capacity
        sold held available salesOpen publishBlockers paymentsReady activeTakeovers
        pendingFollowUps seats { seatId label status }
    }
    displacements { personName followUpStatus reservationStatus seatLabel planName reason }
    takeovers { status appliedByName sessionLabel }
"""


def _ctx(db, account, *, role="admin"):
    return SimpleNamespace(
        db=db,
        user=SimpleNamespace(roles=[SimpleNamespace(role=SimpleNamespace(code=role), role_id=None)]),
        account_id=account.id if account else None,
    )


async def _run(db, ctx, query, **variables):
    result = await schema.execute(query, variable_values=variables, context_value=ctx)
    assert result.errors is None, result.errors
    return result.data


async def test_admin_creates_takes_over_and_publishes_through_graphql(db):
    sc = await regular_class_scenario(db, booked=2)
    admin = await make_account(db)
    ctx = _ctx(db, admin)

    created = await _run(
        db, ctx,
        f"""
        mutation($input: SpecialEventInput!) {{
            createSpecialEvent(input: $input) {{ success error message detail {{ {_EVENT_FIELDS} }} }}
        }}
        """,
        input={
            "name": "Halloween (gql)",
            "venueId": sc["venue"].id,
            "classTypeId": sc["ctype"].id,
            "localDate": sc["day"].isoformat(),
            "startTime": "19:00",
            "endTime": "21:00",
            "price": 500.0,
            "capacity": 14,
        },
    )
    payload = created["createSpecialEvent"]
    assert payload["success"], payload["error"]
    event = payload["detail"]["event"]
    assert (event["status"], event["localStartTime"], event["localEndTime"]) == ("draft", "19:00", "21:00")
    assert len(event["seats"]) == 14 and event["available"] == 14
    assert any("sin sustituir" in b for b in event["publishBlockers"])
    event_id = int(event["id"])

    preview = (
        await _run(
            db, ctx,
            """query($id: Int!) { specialEventTakeoverPreview(eventId: $id) {
                pendingSessionIds affectedClients summary blockers
                sessions { label clients { personName seatLabel planName } } } }""",
            id=event_id,
        )
    )["specialEventTakeoverPreview"]
    assert preview["affectedClients"] == 2 and preview["blockers"] == []
    assert preview["sessions"][0]["label"].endswith("de 19:00 a 20:00")

    applied = (
        await _run(
            db, ctx,
            f"""mutation($id: Int!, $ids: [Int!]!) {{
                applySpecialEventTakeover(eventId: $id, sessionIds: $ids) {{
                    success error message detail {{ {_EVENT_FIELDS} }} }} }}""",
            id=event_id, ids=preview["pendingSessionIds"],
        )
    )["applySpecialEventTakeover"]
    assert applied["success"] and "2 cliente(s)" in applied["message"]
    assert applied["detail"]["takeovers"][0]["appliedByName"] == "Admin Test"
    assert {d["followUpStatus"] for d in applied["detail"]["displacements"]} == {"pending"}

    published = (
        await _run(
            db, ctx,
            f"""mutation($id: Int!) {{ publishSpecialEvent(id: $id) {{
                success error detail {{ {_EVENT_FIELDS} }} }} }}""",
            id=event_id,
        )
    )["publishSpecialEvent"]
    assert published["success"], published["error"]
    assert published["detail"]["event"]["status"] == "published"

    # The weekly class grid shows the displaced class and who was booked, not an empty class.
    week = (
        await _run(
            db, ctx,
            """query($s: Date!, $e: Date!, $v: Int) { weekSessionsWithSeats(startDate: $s, endDate: $e, venueId: $v) {
                id status displacedByEvent seats { label status occupant { fullName } } } }""",
            s=sc["day"].isoformat(), e=sc["day"].isoformat(), v=sc["venue"].id,
        )
    )["weekSessionsWithSeats"]
    assert week[0]["status"] == "displaced" and week[0]["displacedByEvent"] == "Halloween (gql)"
    assert {s["status"] for s in week[0]["seats"]} == {"displaced"}  # nothing bookable
    booked = [s["occupant"]["fullName"] for s in week[0]["seats"] if s["occupant"]]
    assert booked == ["Socio 1", "Socio 2"]  # who was booked stays visible


async def test_staff_without_the_capability_cannot_manage_events(db):
    sc = await regular_class_scenario(db, booked=0)
    ctx = _ctx(db, None, role="staff")
    data = await _run(
        db, ctx,
        """mutation($input: SpecialEventInput!) { createSpecialEvent(input: $input) { success error } }""",
        input={
            "name": "No autorizado", "venueId": sc["venue"].id, "localDate": sc["day"].isoformat(),
            "startTime": "19:00", "price": 500.0,
        },
    )
    assert data["createSpecialEvent"]["success"] is False
    assert "permiso" in data["createSpecialEvent"]["error"]
    listing = await _run(db, ctx, "query { specialEvents { id } }")
    assert listing["specialEvents"] == []


async def test_form_options_report_payment_readiness(db, monkeypatch):
    from app.core.mercadopago_config import MercadoPagoConfig

    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "")
    admin = await make_account(db)
    await regular_class_scenario(db, booked=0)
    options = (
        await _run(
            db, _ctx(db, admin),
            "query { specialEventFormOptions { defaultTimezone paymentsReady paymentsWarning venues { id seats { label } } } }",
        )
    )["specialEventFormOptions"]
    assert options["defaultTimezone"] == "America/Mexico_City"
    assert options["paymentsReady"] is False and "Mercado Pago no está configurado" in options["paymentsWarning"]
    assert any(len(v["seats"]) == 14 for v in options["venues"])
