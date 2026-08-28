"""Tests for the marketing campaigns feature.

Three concerns, all of which were unguarded before:

* **Authorization** — campaign reads expose member PII (recipient phone numbers, audience
  name samples). Every gated resolver must return an empty result, not data, when the
  caller lacks ``send_campaigns``.
* **Quiet hours** — a deferral must be rescheduled for the next instant a marketing message
  is actually allowed. Rescheduling for "now" makes every sweep re-attempt the whole
  audience until dawn.
* **Reply attribution** — Meta status callbacks stop at ``read``; ``replied`` can only come
  from inbound traffic, and must be forward-only and time-bounded.

The DB-backed dispatch invariants live at the bottom and use the rolled-back ``db`` fixture.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sqlalchemy.dialects import postgresql

from app.crud import campaignsCrud as crud
from app.graphql.campaigns.queries import CampaignsQuery
from app.services import attendance_profile_service as attendance
from app.services import segmentation_service as seg
from app.services import whatsapp_outbound as outbound
from app.services import campaign_service as dispatch
from app.services import fitness_estimation_service as estimation
from app.services.campaign_service import (
    CAMPAIGN_VARIABLES,
    allowed_variables_for,
    apply_favorite_class_variables,
    apply_inactivity_variables,
    variable_samples,
)

TZ = ZoneInfo("America/Mexico_City")


def _info():
    return SimpleNamespace(context=SimpleNamespace(db=object()))


# ---------------------------------------------------------------------------
# Authorization: campaign reads must not leak member PII
# ---------------------------------------------------------------------------
@pytest.fixture
def denied(monkeypatch):
    """Make every capability check fail, as it does for a caller without send_campaigns."""

    async def _deny(_info, _capability):
        return "No tienes permiso para esta accion."

    monkeypatch.setattr("app.graphql.campaigns.queries.require_capability", _deny)


async def test_campaigns_list_denied_without_capability(denied):
    assert await CampaignsQuery().campaigns(_info()) == []


async def test_campaign_detail_denied_without_capability(denied):
    assert await CampaignsQuery().campaign(_info(), id=1) is None


async def test_recipients_denied_without_capability(denied):
    """The recipient ledger carries phone_e164 / wa_id — the most sensitive read here."""
    assert await CampaignsQuery().campaign_recipients(_info(), campaign_id=1) == []


async def test_metrics_denied_without_capability(denied):
    metrics = await CampaignsQuery().campaign_metrics(_info(), campaign_id=1)
    assert metrics.targeted == 0
    assert metrics.sent == 0
    assert metrics.revenue_recovered == 0.0


async def test_audience_preview_denied_without_capability(denied):
    """previewAudience returns member names; an ungated preview is a directory dump."""
    preview = await CampaignsQuery().preview_audience(_info(), audience_spec={})
    assert preview.count == 0
    assert preview.sample == []


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "now,expected",
    [
        # Inside quiet hours (21:00-09:00) -> wait for the boundary.
        (datetime(2026, 8, 22, 22, 30, tzinfo=TZ), datetime(2026, 8, 23, 9, 0, tzinfo=TZ)),
        (datetime(2026, 8, 22, 3, 0, tzinfo=TZ), datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
        (datetime(2026, 8, 22, 8, 59, tzinfo=TZ), datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
        # Outside -> unchanged, send right away.
        (datetime(2026, 8, 22, 9, 0, tzinfo=TZ), datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
        (datetime(2026, 8, 22, 14, 0, tzinfo=TZ), datetime(2026, 8, 22, 14, 0, tzinfo=TZ)),
        (datetime(2026, 8, 22, 20, 59, tzinfo=TZ), datetime(2026, 8, 22, 20, 59, tzinfo=TZ)),
    ],
)
def test_next_allowed_send_at(now, expected):
    assert outbound.next_allowed_send_at(now) == expected


def test_next_allowed_send_at_never_goes_backwards():
    """The whole point: a deferral must move forward, never reschedule for 'now'."""
    inside = datetime(2026, 8, 22, 23, 45, tzinfo=TZ)
    assert outbound.next_allowed_send_at(inside) > inside


def test_quiet_hours_disabled_passes_through(monkeypatch):
    # The helpers read the singleton lazily, so patching it here reaches them.
    from app.core.outbound_config import outbound_config

    monkeypatch.setattr(outbound_config, "QUIET_HOURS_START", 0)
    monkeypatch.setattr(outbound_config, "QUIET_HOURS_END", 0)
    now = datetime(2026, 8, 22, 3, 0, tzinfo=TZ)
    assert outbound._in_quiet_hours(now) is False
    assert outbound.next_allowed_send_at(now) == now


# ---------------------------------------------------------------------------
# Reply attribution
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return [] if self._row is None else [self._row]


class _FakeSession:
    """Minimal AsyncSession stand-in: records statements, returns a canned row."""

    def __init__(self, row=None):
        self.row = row
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _FakeResult(self.row)

    async def flush(self):
        return None


async def test_inbound_reply_marks_recipient_replied():
    sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    recipient = SimpleNamespace(
        status="delivered", replied_at=None, sent_at=sent_at, updated_at=None
    )
    db = _FakeSession(recipient)
    stamp = datetime.now(timezone.utc)

    assert await crud.apply_inbound_reply(db, wa_id="+52 871 970 8890", timestamp=stamp) is True
    assert recipient.status == "replied"
    assert recipient.replied_at == stamp


async def test_inbound_reply_keeps_first_reply_timestamp():
    first = datetime.now(timezone.utc) - timedelta(hours=1)
    recipient = SimpleNamespace(status="read", replied_at=first, sent_at=first, updated_at=None)
    db = _FakeSession(recipient)

    await crud.apply_inbound_reply(
        db, wa_id="5218719708890", timestamp=datetime.now(timezone.utc)
    )

    assert recipient.replied_at == first  # forward-only: the first reply is the attribution


async def test_inbound_reply_from_unknown_number_is_a_noop():
    db = _FakeSession(None)
    result = await crud.apply_inbound_reply(
        db, wa_id="5219999999999", timestamp=datetime.now(timezone.utc)
    )
    assert result is False


async def test_inbound_reply_ignores_blank_number():
    """No digits -> never run the query (it would match recipients with an empty wa_id)."""
    db = _FakeSession(None)
    result = await crud.apply_inbound_reply(
        db, wa_id="   ", timestamp=datetime.now(timezone.utc)
    )
    assert result is False
    assert db.statements == []


# ---------------------------------------------------------------------------
# Stale-run reclaim (SQL shape)
# ---------------------------------------------------------------------------
async def test_due_query_reclaims_abandoned_sending_runs():
    """A campaign stuck in 'sending' after a restart must be picked up by the sweep."""
    db = _FakeSession(None)

    await crud.campaigns_due_for_send(db, datetime.now(timezone.utc))
    sql = str(db.statements[0]).lower()

    assert "heartbeat_at" in sql  # the staleness signal
    assert "status" in sql
    assert crud.STALE_RUN_MINUTES > 0


# ---------------------------------------------------------------------------
# Dispatch invariants (DB-backed)
# ---------------------------------------------------------------------------
async def test_claim_batch_takes_each_recipient_exactly_once(db):
    """The claim is the whole idempotency story: a recipient may be taken once."""
    campaign = await crud.create_campaign(
        db, name="test-claim", objective="win_back", commit=False
    )
    variant = await crud.ensure_default_variant(db, campaign.id, commit=False)
    for n in range(3):
        await crud.insert_recipient(
            db,
            campaign_id=campaign.id,
            dedup_key=f"campaign:{campaign.id}:claim-{n}",
            variant_id=variant.id,
            phone_e164="5218719708890",
            wa_id="5218719708890",
        )

    first = await crud.claim_recipient_batch(db, campaign.id, 10)
    assert len(first) == 3
    # Everyone is now 'sending'; a second pass must come back empty, not re-claim them.
    assert await crud.claim_recipient_batch(db, campaign.id, 10) == []


async def test_claim_batch_respects_its_limit(db):
    campaign = await crud.create_campaign(
        db, name="test-limit", objective="win_back", commit=False
    )
    for n in range(5):
        await crud.insert_recipient(
            db,
            campaign_id=campaign.id,
            dedup_key=f"campaign:{campaign.id}:limit-{n}",
            phone_e164="5218719708890",
        )

    assert len(await crud.claim_recipient_batch(db, campaign.id, 2)) == 2
    assert len(await crud.claim_recipient_batch(db, campaign.id, 10)) == 3


async def test_claim_batch_skips_recipients_that_are_not_due_yet(db):
    """A deferred recipient must wait, not be re-sent on the next tick."""
    campaign = await crud.create_campaign(
        db, name="test-due", objective="win_back", commit=False
    )
    due_id = await crud.insert_recipient(
        db, campaign_id=campaign.id, dedup_key=f"campaign:{campaign.id}:due",
        phone_e164="5218719708890",
    )
    waiting_id = await crud.insert_recipient(
        db, campaign_id=campaign.id, dedup_key=f"campaign:{campaign.id}:waiting",
        phone_e164="5218719708891",
    )
    waiting = await crud.get_recipient_model(db, waiting_id)
    await crud.defer_recipient(
        db,
        waiting,
        send_after=datetime.now(timezone.utc) + timedelta(hours=8),
        reason="quiet_hours",
        commit=False,
    )

    claimed = await crud.claim_recipient_batch(db, campaign.id, 10)

    assert claimed == [due_id]
    # A deferral is a wait, not a failure — the metrics must not report it as one.
    assert waiting.status == "pending"
    assert waiting.skip_reason == "quiet_hours"


async def test_next_send_after_reports_the_earliest_wait(db):
    """Lets a campaign reschedule for the exact instant work resumes."""
    campaign = await crud.create_campaign(
        db, name="test-next", objective="win_back", commit=False
    )
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    later = datetime.now(timezone.utc) + timedelta(hours=9)
    for label, when in (("soon", soon), ("later", later)):
        rid = await crud.insert_recipient(
            db, campaign_id=campaign.id, dedup_key=f"campaign:{campaign.id}:{label}",
            phone_e164="5218719708890",
        )
        await crud.defer_recipient(
            db, await crud.get_recipient_model(db, rid), send_after=when, commit=False
        )

    earliest = await crud.next_send_after(db, campaign.id)
    assert abs((earliest - soon).total_seconds()) < 1


async def test_insert_recipient_is_idempotent(db):
    """Re-building an audience must not duplicate anyone (dedup_key ON CONFLICT)."""
    campaign = await crud.create_campaign(
        db, name="test-dedup", objective="win_back", commit=False
    )
    key = f"campaign:{campaign.id}:dedup"
    first = await crud.insert_recipient(
        db, campaign_id=campaign.id, dedup_key=key, phone_e164="5218719708890"
    )
    second = await crud.insert_recipient(
        db, campaign_id=campaign.id, dedup_key=key, phone_e164="5218719708890"
    )

    assert first is not None
    assert second is None  # already in the snapshot


# ---------------------------------------------------------------------------
# Class affinity: "which classes does this member actually book?"
# ---------------------------------------------------------------------------
def _audience_sql(*predicates) -> str:
    """Compile an audience spec to SQL. No database involved — shape only."""
    spec = {"base": "members", "predicates": list(predicates)}
    return str(
        seg.build_member_id_query(spec).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_grouped_schedules_compete_as_one_block():
    """The whole point of the grid picker.

    Selecting Monday, Wednesday and Friday at 08:00 must put those three bookings in ONE
    bucket. Ranking them individually asks "is their single most-booked class in the list?",
    which drops a member who spreads their habit across the week — the exact case the grid is
    for.
    """
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "favorite",
            "groups": [{"class_type_id": 7, "template_ids": [11, 14, 19]}],
        }
    )
    assert "template_id IN (11, 14, 19)) THEN 'selected'" in sql
    assert "GROUP BY app.reservations.person_id, CASE" in sql   # bucketed, not per class
    assert "bucket = 'selected'" in sql
    assert "rn = 1" in sql


def test_bookings_outside_the_selection_stay_in_their_own_activity_bucket():
    """So a mostly-19:00 Spinning member does not match a selection of just 08:00."""
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "favorite",
            "groups": [{"class_type_id": 7, "template_ids": [11]}],
        }
    )
    assert "ELSE 'type:' || CAST(app.class_sessions.class_type_id AS VARCHAR)" in sql


def test_whole_activity_group_matches_by_class_type():
    """No narrowing means the whole activity — including ad-hoc sessions with no template,
    which a list of template ids could never reach."""
    sql = _audience_sql(
        {"type": "class_affinity", "mode": "favorite", "groups": [{"class_type_id": 7}]}
    )
    assert "class_type_id = 7) THEN 'selected'" in sql
    assert "template_id IN" not in sql


def test_several_activities_share_one_selected_bucket():
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "favorite",
            "groups": [{"class_type_id": 7}, {"class_type_id": 9, "template_ids": [31]}],
        }
    )
    assert "class_type_id = 7 OR app.class_sessions.template_id IN (31)" in sql
    assert sql.count("'selected'") >= 2   # one CASE in the select list, one in GROUP BY


def test_class_affinity_weights_attendance_above_intent():
    """Showing up must outrank merely booking, or a no-show decides the segment."""
    sql = _audience_sql(
        {"type": "class_affinity", "mode": "favorite", "groups": [{"class_type_id": 7}]}
    )
    assert "WHEN (app.reservations.status = 'checked_in') THEN 3 ELSE 1" in sql


def test_class_affinity_ignores_cancellations_and_no_shows():
    sql = _audience_sql(
        {"type": "class_affinity", "mode": "favorite", "groups": [{"class_type_id": 7}]}
    )
    assert "status IN ('checked_in', 'reserved')" in sql
    assert "no_show" not in sql
    assert "canceled" not in sql


def test_attended_mode_requires_a_minimum_number_of_bookings():
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "attended",
            "groups": [{"class_type_id": 7, "template_ids": [11, 14]}],
            "min_reservations": 3,
        }
    )
    assert "HAVING count(*) >= 3" in sql
    assert "row_number()" not in sql.lower()   # attended is not a ranking


def test_empty_selection_is_not_a_filter():
    """Selecting no classes must widen to 'everyone', never narrow to 'nobody'."""
    assert "selected" not in _audience_sql({"type": "class_affinity", "groups": []})


def test_class_affinity_combines_with_membership_predicates():
    """The real win-back case: expired members whose habit is Spinning at 08:00."""
    sql = _audience_sql(
        {"type": "membership_status", "in": ["expired"]},
        {"type": "membership_end_at", "op": "between", "days_from_now": [-90, -7]},
        {
            "type": "class_affinity",
            "mode": "favorite",
            "groups": [{"class_type_id": 7, "template_ids": [11, 14, 19]}],
        },
    )
    assert "bucket = 'selected'" in sql
    assert "membership_subscriptions" in sql


def test_legacy_level_and_in_spec_still_compiles():
    """A campaign saved before the grid picker must keep working."""
    by_type = _audience_sql(
        {"type": "class_affinity", "level": "class_type", "mode": "favorite", "in": [3, 7]}
    )
    assert "class_type_id = 3 OR app.class_sessions.class_type_id = 7" in by_type

    by_template = _audience_sql(
        {
            "type": "class_affinity",
            "level": "class_template",
            "mode": "favorite",
            "in": [11, 14],
        }
    )
    assert "template_id IN (11, 14)" in by_template


# ---------------------------------------------------------------------------
# class_affinity + standing bookings (SQL shape)
# ---------------------------------------------------------------------------
def test_favorite_mode_also_matches_an_active_standing_booking():
    """A standing booking counts even when it is not the statistically dominant bucket —
    it is an explicit commitment, not an inference."""
    sql = _audience_sql(
        {"type": "class_affinity", "mode": "favorite", "groups": [{"class_type_id": 7}]}
    )
    assert "app.standing_bookings.status = 'active'" in sql
    assert "app.class_templates.class_type_id = 7" in sql
    assert "bucket = 'selected'" in sql  # the statistical path is still there too


def test_attended_mode_also_matches_an_active_standing_booking():
    """A brand new standing booking, with no sessions materialized yet, still counts."""
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "attended",
            "groups": [{"class_type_id": 7}],
            "min_reservations": 2,
        }
    )
    assert "app.standing_bookings.status = 'active'" in sql
    assert "HAVING count(*) >= 2" in sql  # the attended-count path is still there too


def test_standing_booking_condition_respects_template_narrowing():
    """Selecting specific horarios must narrow the standing-booking match the same way."""
    sql = _audience_sql(
        {
            "type": "class_affinity",
            "mode": "favorite",
            "groups": [{"class_type_id": 7, "template_ids": [11, 14]}],
        }
    )
    assert "app.class_templates.id IN (11, 14)" in sql
    assert "app.class_templates.class_type_id = 7" not in sql


@pytest.mark.parametrize(
    "predicate,message",
    [
        ({"type": "class_affinity", "mode": "loyal", "groups": [{"class_type_id": 1}]}, "Modo"),
        ({"type": "class_affinity", "groups": "spinning"}, "lista"),
        ({"type": "class_affinity", "groups": [{"template_ids": []}]}, "class_type_id"),
        ({"type": "class_affinity", "groups": [{"class_type_id": "x"}]}, "numericos"),
        (
            {"type": "class_affinity", "groups": [{"class_type_id": 1}], "lookback_days": 0},
            "lookback_days",
        ),
        (
            {
                "type": "class_affinity",
                "mode": "attended",
                "groups": [{"class_type_id": 1}],
                "min_reservations": 0,
            },
            "min_reservations",
        ),
        ({"type": "class_affinity", "level": "instructor", "in": [1]}, "Nivel"),
    ],
)
def test_class_affinity_rejects_malformed_specs(predicate, message):
    """An explicit 0 must raise, not silently fall back to the default."""
    with pytest.raises(seg.SegmentationError) as exc:
        _audience_sql(predicate)
    assert message in str(exc.value)


# ---------------------------------------------------------------------------
# Class-affinity template variables
# ---------------------------------------------------------------------------
def test_campaign_variables_are_allowed_alongside_member_variables():
    allowed = allowed_variables_for("win_back")
    assert set(CAMPAIGN_VARIABLES) <= allowed
    assert "member_first_name" in allowed  # the shared catalog is not replaced


def test_favorite_class_variables_resolve_from_the_snapshot():
    favorite = attendance.FavoriteClass(
        class_type_id=3,
        class_type_name="Spinning",
        class_template_id=11,
        weekday=1,
        start_time_local=time(7, 0),
    )
    context = apply_favorite_class_variables({}, favorite)
    assert context["favorite_class_name"] == "Spinning"
    assert context["favorite_class_day"] == "lunes"
    assert context["favorite_class_time"] == "7:00 a. m."
    assert context["favorite_class_schedule"] == "lunes a las 7:00 a. m."


def test_member_without_history_gets_empty_strings_not_a_placeholder():
    """Inventing a class name would put a falsehood in a marketing message."""
    context = apply_favorite_class_variables({}, None)
    assert context == {
        "favorite_class_name": "",
        "favorite_class_day": "",
        "favorite_class_time": "",
        "favorite_class_schedule": "",
    }


# ---------------------------------------------------------------------------
# Derived variables: inactivity days + motivational kcal/kg-fat translation
# ---------------------------------------------------------------------------
def _spin_profile(**overrides):
    """A gym like Love Fitness: Monday-Friday, one-hour spinning."""
    config = estimation.EstimationConfig(**overrides)
    schedule = estimation.GymSchedule(
        open_weekdays=frozenset({1, 2, 3, 4, 5}),
        duration_by_template={11: 60},
        duration_by_class_type={1: 60},
        met_by_class_type={1: 8.5},
        mean_duration_min=60,
    )
    return estimation.EstimationProfile(config=config, schedule=schedule)


def test_inactivity_variables_computed_from_lapsed_subscription():
    subscription = SimpleNamespace(end_at=datetime.now(timezone.utc) - timedelta(days=84))
    context = apply_inactivity_variables(
        {},
        subscription,
        profile=_spin_profile(),
        favorite=SimpleNamespace(class_type_id=1, class_template_id=11),
        sessions_per_week=3.0,
    )

    assert context["days_inactive"] == "84"
    # 12 weeks x 3 sessions x (8.5-1) MET x 70 kg x 1 h = 18,900 kcal.
    assert context["kcal_not_burned"] == "18,900"
    # 225 kcal/day removed -> 10.1 kg steady state, ~15% of it reached in 84 days.
    assert context["kg_fat_equivalent"] == "1.5"
    assert context["kcal_window_label"] == "los últimos 3 meses"


def test_the_old_constant_would_have_sent_an_impossible_number():
    """Regression guard for the bug this replaced.

    ``days_inactive * 900`` told the median lapsed member (349 days out) that they had
    accumulated 40.8 kg of fat. Whatever the configuration, a win-back message has to stay
    in a range a person can believe, or it costs the gym the number it was sent from.
    """
    subscription = SimpleNamespace(end_at=datetime.now(timezone.utc) - timedelta(days=349))
    context = apply_inactivity_variables(
        {},
        subscription,
        profile=_spin_profile(),
        favorite=SimpleNamespace(class_type_id=1, class_template_id=11),
        sessions_per_week=2.7,
    )

    assert context["days_inactive"] == "349"
    assert float(context["kg_fat_equivalent"]) < 5.0
    assert 349 * 900 / 7700 > 40  # what the old formula produced


def test_estimate_never_counts_more_sessions_than_the_gym_is_open():
    """A member cannot miss six classes a week at a gym that opens five days.

    Cadence is measured per *active* week, so a burst of bookings in one week can exceed
    what the schedule offers; the schedule is the ceiling.
    """
    result = estimation.estimate_inactivity(
        _spin_profile(), days_inactive=70, sessions_per_week=9.0, class_type_id=1
    )
    assert result.sessions_per_week == 5.0

    seven_days = estimation.EstimationProfile(
        config=estimation.EstimationConfig(),
        schedule=estimation.GymSchedule(
            open_weekdays=frozenset(range(7)),
            met_by_class_type={1: 8.5},
            mean_duration_min=60,
        ),
    )
    assert (
        estimation.estimate_inactivity(
            seven_days, days_inactive=70, sessions_per_week=9.0, class_type_id=1
        ).sessions_per_week
        == 7.0
    )


def test_weekend_gym_earns_a_bigger_estimate_than_a_weekday_one():
    """The whole point of deriving the schedule: a gym open seven days a week has more
    classes to miss, and nobody had to configure that."""
    weekday = estimation.estimate_inactivity(
        _spin_profile(), days_inactive=84, sessions_per_week=6.0, class_type_id=1
    )
    weekend_profile = estimation.EstimationProfile(
        config=estimation.EstimationConfig(),
        schedule=estimation.GymSchedule(
            open_weekdays=frozenset(range(7)),
            met_by_class_type={1: 8.5},
            mean_duration_min=60,
        ),
    )
    weekend = estimation.estimate_inactivity(
        weekend_profile, days_inactive=84, sessions_per_week=6.0, class_type_id=1
    )
    assert weekend.kcal > weekday.kcal


def test_class_duration_scales_the_estimate():
    """A 30-minute express class is worth half an hour-long one, read from the same
    schedule rows the session generator already uses."""
    profile = _spin_profile()
    hour = estimation.estimate_inactivity(
        profile, days_inactive=84, sessions_per_week=3.0, class_type_id=1, class_template_id=11
    )
    half_profile = estimation.EstimationProfile(
        config=profile.config,
        schedule=estimation.GymSchedule(
            open_weekdays=frozenset({1, 2, 3, 4, 5}),
            duration_by_template={12: 30},
            met_by_class_type={1: 8.5},
            mean_duration_min=30,
        ),
    )
    half = estimation.estimate_inactivity(
        half_profile, days_inactive=84, sessions_per_week=3.0, class_type_id=1,
        class_template_id=12,
    )
    assert half.kcal == pytest.approx(hour.kcal / 2, rel=0.01)


def test_intensity_comes_from_the_activity():
    """Yoga and spinning must not be quoted the same calories."""
    profile = estimation.EstimationProfile(
        config=estimation.EstimationConfig(),
        schedule=estimation.GymSchedule(
            open_weekdays=frozenset({1, 2, 3, 4, 5}),
            met_by_class_type={1: 8.5, 2: 3.0},
            mean_duration_min=60,
        ),
    )
    spin = estimation.estimate_inactivity(
        profile, days_inactive=84, sessions_per_week=3.0, class_type_id=1
    )
    yoga = estimation.estimate_inactivity(
        profile, days_inactive=84, sessions_per_week=3.0, class_type_id=2
    )
    assert spin.kcal > yoga.kcal * 3


def test_the_horizon_rails_the_calorie_total_without_shaping_the_normal_case():
    """The rail is protection against a pathological outlier, not the mechanism.

    Set where it bites the normal case, it reproduces the bug it was meant to fix at a new
    offset: everyone past it receives an identical figure. The default sits past the oldest
    member in the real audience (714 days), so it only engages for someone who left years ago.
    """
    profile = _spin_profile()
    oldest_real_member = estimation.estimate_inactivity(
        profile, days_inactive=714, sessions_per_week=2.5, class_type_id=1
    )
    assert oldest_real_member.horizon_reached is False

    ancient = estimation.estimate_inactivity(
        profile, days_inactive=3650, sessions_per_week=2.5, class_type_id=1
    )
    assert ancient.horizon_reached is True
    assert ancient.weeks_counted == 104.0


def test_a_longer_absence_always_says_more():
    """The failure that made the first fix wrong: a fixed horizon flattened everyone past it,
    so a two-year lapse read exactly like a three-month one — in a campaign whose entire
    purpose is to be more urgent the longer someone has been gone."""
    profile = _spin_profile()
    figures = [
        estimation.estimate_inactivity(
            profile, days_inactive=days, sessions_per_week=2.5, class_type_id=1
        ).kg_fat
        for days in (84, 122, 349, 463, 673, 714)
    ]
    assert figures == sorted(figures)
    assert len(set(figures)) == len(figures)  # strictly increasing, never a plateau


def test_kilograms_saturate_instead_of_accumulating():
    """Bodies compensate: appetite and non-exercise activity adapt, and a heavier body costs
    more to maintain. Twice the absence is therefore well under twice the weight, and no
    absence ever passes the steady state — which is what removes the need for a ceiling."""
    profile = _spin_profile()
    one_year = estimation.estimate_inactivity(
        profile, days_inactive=365, sessions_per_week=2.5, class_type_id=1
    )
    two_years = estimation.estimate_inactivity(
        profile, days_inactive=730, sessions_per_week=2.5, class_type_id=1
    )
    assert two_years.kg_fat > one_year.kg_fat
    assert two_years.kg_fat < one_year.kg_fat * 2
    assert two_years.kg_fat < two_years.kg_steady_state
    # Half the steady state after roughly one half-life.
    assert one_year.kg_fat == pytest.approx(one_year.kg_steady_state / 2, rel=0.02)


def test_no_absence_however_long_exceeds_the_steady_state():
    profile = _spin_profile()
    for days in (365, 730, 3650, 36500):
        result = estimation.estimate_inactivity(
            profile, days_inactive=days, sessions_per_week=2.5, class_type_id=1
        )
        assert result.kg_fat < result.kg_steady_state


def test_turning_off_adaptation_returns_to_the_linear_rule():
    """The escape hatch for a gym that wants the familiar 7700 kcal/kg — which overpredicts
    long-run weight change roughly twofold, hence the default being off."""
    linear = estimation.estimate_inactivity(
        _spin_profile(metabolic_adaptation=False),
        days_inactive=349,
        sessions_per_week=2.5,
        class_type_id=1,
    )
    saturating = estimation.estimate_inactivity(
        _spin_profile(), days_inactive=349, sessions_per_week=2.5, class_type_id=1
    )
    assert linear.kg_fat == pytest.approx(linear.kcal / 7700, rel=0.01)
    assert linear.kg_steady_state == 0.0
    assert linear.kg_fat > saturating.kg_fat * 1.8


def test_calories_stay_linear_because_they_actually_are():
    """The distinction the first fix missed: energy not spent really is additive, so doubling
    the absence doubles the calories even though it does not double the kilograms."""
    profile = _spin_profile()
    one = estimation.estimate_inactivity(
        profile, days_inactive=182, sessions_per_week=2.5, class_type_id=1
    )
    two = estimation.estimate_inactivity(
        profile, days_inactive=364, sessions_per_week=2.5, class_type_id=1
    )
    assert two.kcal == pytest.approx(one.kcal * 2, rel=0.01)


def test_members_without_history_fall_back_to_the_configured_default():
    """About a third of the lapsed audience never booked a class. They get the configured
    assumption, flagged as such, not a number invented from nothing."""
    result = estimation.estimate_inactivity(
        _spin_profile(), days_inactive=84, sessions_per_week=None, class_type_id=1
    )
    assert result.cadence_from_history is False
    assert result.sessions_per_week == 2.5


def test_estimate_is_net_of_resting_metabolism():
    """The claim is the extra burn over sitting still, not the gross total."""
    net = estimation.estimate_inactivity(
        _spin_profile(), days_inactive=84, sessions_per_week=3.0, class_type_id=1
    )
    gross = estimation.estimate_inactivity(
        _spin_profile(net_of_resting=False),
        days_inactive=84,
        sessions_per_week=3.0,
        class_type_id=1,
    )
    assert net.kcal_per_session == pytest.approx(7.5 * 70, rel=0.01)
    assert gross.kcal_per_session == pytest.approx(8.5 * 70, rel=0.01)


def test_wizard_samples_are_produced_by_the_engine():
    """The preview has to show what the send will show.

    The catalog used to carry hand-written samples ("8,700" kcal) that the formula could
    not produce for any input, so the operator approved one message and members received
    another roughly 36x larger.
    """
    profile = _spin_profile()
    samples = variable_samples(profile)
    expected = estimation.estimate_inactivity(profile, days_inactive=365)
    assert samples["kcal_not_burned"] == estimation.format_kcal(expected.kcal)
    assert samples["kg_fat_equivalent"] == estimation.format_kg_fat(expected.kg_fat)
    assert samples["kcal_window_label"] == "los últimos 12 meses"


def test_the_sample_previews_a_typical_member_not_the_horizon():
    """Quoting the rail would preview the most extreme member the system can produce; the
    operator needs to see what the bulk of the audience will actually receive."""
    samples = variable_samples(_spin_profile())
    assert samples["days_inactive"] == "365"
    assert float(samples["kg_fat_equivalent"]) < 5.0


def test_days_since_last_class_is_only_filled_from_real_attendance():
    """"Hace X días que no te vemos" is an attendance claim, so a member who never booked
    gets a blank rather than the membership figure wearing an attendance label."""
    subscription = SimpleNamespace(end_at=datetime.now(timezone.utc) - timedelta(days=349))
    never_booked = apply_inactivity_variables({}, subscription, profile=_spin_profile())
    assert never_booked["days_inactive"] == "349"
    assert never_booked["days_since_last_class"] == ""

    booked = apply_inactivity_variables(
        {}, subscription, profile=_spin_profile(), days_since_last_class=185
    )
    assert booked["days_since_last_class"] == "185"


def test_inactivity_variables_are_empty_without_a_lapsed_subscription():
    """No subscription, or one that hasn't actually expired, is not 'inactive' — never invent
    a number just because a placeholder wants one."""
    blank = {
        "days_inactive": "",
        "days_since_last_class": "",
        "kcal_not_burned": "",
        "kg_fat_equivalent": "",
        "kcal_window_label": "",
    }
    assert apply_inactivity_variables({}, None) == blank

    still_active = SimpleNamespace(end_at=datetime.now(timezone.utc) + timedelta(days=10))
    assert apply_inactivity_variables({}, still_active) == blank


def test_inactivity_variables_use_naive_end_at_as_utc():
    """Membership dates come back naive from some code paths; must not crash comparing to
    an aware 'now'."""
    naive_end_at = datetime.utcnow() - timedelta(days=10)
    subscription = SimpleNamespace(end_at=naive_end_at)
    context = apply_inactivity_variables({}, subscription)
    assert context["days_inactive"] == "10"


def test_favorite_class_schedule_falls_back_to_whichever_half_is_known():
    only_day = attendance.FavoriteClass(weekday=1)
    assert only_day.schedule_text == "lunes"

    only_time = attendance.FavoriteClass(start_time_local=time(7, 0))
    assert only_time.schedule_text == "7:00 a. m."

    neither = attendance.FavoriteClass()
    assert neither.schedule_text == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        (time(7, 0), "7:00 a. m."),
        (time(0, 30), "12:30 a. m."),   # midnight is 12, not 0
        (time(12, 0), "12:00 p. m."),   # noon is p.m., not a.m.
        (time(19, 45), "7:45 p. m."),
        (None, ""),
    ],
)
def test_class_time_formatting(value, expected):
    assert attendance.format_time(value) == expected


def test_weekday_names_start_on_sunday():
    """class_templates.weekday is 0=Sunday (see classModel), not 0=Monday."""
    assert attendance.weekday_name(0) == "domingo"
    assert attendance.weekday_name(6) == "sábado"
    assert attendance.weekday_name(None) == ""


# ---------------------------------------------------------------------------
# Dispatcher: throttle, batch sizing, metrics maths
# ---------------------------------------------------------------------------
async def test_rate_gate_spaces_sends_without_serialising_them():
    """The throttle must pace *starts*, not make each send wait for the previous one.

    A sleep between sends would make every message pay the previous one's network latency;
    handing out timed slots keeps the rate exact while letting sends overlap.
    """
    gate = dispatch._RateGate(600)  # 600/min -> 0.1s between slots
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(*(gate.wait() for _ in range(4)))
    elapsed = loop.time() - start

    # Four slots at 0.1s: the last starts ~0.3s in. Generous upper bound for slow CI.
    assert 0.25 <= elapsed < 2.0


async def test_rate_gate_does_not_delay_the_first_send():
    gate = dispatch._RateGate(60)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await gate.wait()
    assert loop.time() - start < 0.1


def test_batch_size_is_capped_by_what_the_throttle_can_deliver():
    """A slice should take about one sweep tick, not monopolise the scheduler for an hour."""
    slow = dispatch._batch_size(throttle_per_minute=6)
    fast = dispatch._batch_size(throttle_per_minute=10_000)

    assert slow < fast          # a slow campaign claims less work per slice
    assert slow >= 1            # ...but never zero, or nothing would ever send
    assert fast <= 2000         # and never unbounded


def test_concurrency_stays_within_the_connection_pool():
    """Each in-flight send holds a session; the engine pool is 5 + 10 overflow."""
    assert 1 <= dispatch._concurrency() <= 10


def test_metrics_treat_later_statuses_as_having_passed_through_earlier_ones():
    """A 'read' recipient was also delivered and sent; funnels must not shrink downstream."""
    metrics = dispatch._metrics_from(
        {"sent": 10, "delivered": 30, "read": 40, "replied": 5, "pending": 7}, 12, None
    )

    assert metrics["sent"] == 85        # sent + delivered + read + replied
    assert metrics["delivered"] == 75   # delivered + read + replied
    assert metrics["read"] == 45        # read + replied
    assert metrics["delivered"] <= metrics["sent"]
    assert metrics["read"] <= metrics["delivered"]


def test_metrics_never_divide_by_zero():
    metrics = dispatch._metrics_from({}, 0, None)
    assert metrics["conversion_rate"] == 0.0
    assert metrics["targeted"] == 0


def test_deferred_recipients_are_not_counted_as_failures():
    """A quiet-hours wait is not a failure; reporting it as one misleads the operator."""
    metrics = dispatch._metrics_from({"pending": 40, "sent": 10}, 0, None)
    assert metrics["failed"] == 0
    assert metrics["pending"] == 40
