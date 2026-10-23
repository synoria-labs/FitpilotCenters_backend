from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from typing import Optional

from app.models import MembershipPlan
from app.core.timezones import to_local


def _normalize_to_utc(dt: datetime) -> datetime:
    """Normalize aware instants to UTC; naive API values are interpreted as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_payment_amount(
    plan: MembershipPlan,
    payment_amount: Optional[Decimal | float],
) -> Decimal:
    """Resolve payment amount to a Decimal, falling back to plan price."""
    if payment_amount is not None:
        return payment_amount if isinstance(payment_amount, Decimal) else Decimal(str(payment_amount))

    return plan.price if isinstance(plan.price, Decimal) else Decimal(str(plan.price))


def _calculate_subscription_end(plan: MembershipPlan, start_at: datetime) -> datetime:
    """Calculate subscription end datetime based on plan duration."""
    start_at = to_local(start_at)
    if plan.duration_unit == "day":
        end_at = start_at + timedelta(days=plan.duration_value)
    elif plan.duration_unit == "week":
        # A complete seven-day interval: buying after today's class still covers next week's
        # occurrence, while buying before it never includes an eighth day.
        return (start_at + timedelta(weeks=plan.duration_value)).astimezone(timezone.utc)
    elif plan.duration_unit == "month":
        from dateutil.relativedelta import relativedelta

        end_at = start_at + relativedelta(months=plan.duration_value)
    else:
        # Fallback to days to avoid unexpected units
        end_at = start_at + timedelta(days=plan.duration_value)

    # Adjust so the membership ends at the end of the day (23:59:59).
    return end_at.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc)


def _align_date_to_weekday(start_date: date, template_weekday: Optional[int]) -> date:
    """Return the first date on or after start_date that matches template_weekday."""
    if template_weekday is None:
        return start_date

    normalized = template_weekday
    if normalized <= 0:
        normalized = 7 if normalized == 0 else ((normalized % 7) or 7)
    elif normalized > 7:
        normalized = ((normalized - 1) % 7) + 1

    base_weekday = start_date.isoweekday()
    delta = (normalized - base_weekday) % 7
    return start_date + timedelta(days=delta)


def _get_plan_window_override(plan: MembershipPlan) -> Optional[int]:
    """
    Check for optional override fields on the plan that define standing booking window in days.
    Supports future schema extensions without requiring code changes.
    """
    override_fields = (
        "standing_window_days",
        "standing_booking_window_days",
        "standing_materialization_days",
    )
    for field in override_fields:
        value = getattr(plan, field, None)
        if value is None:
            continue
        try:
            days = int(value)
        except (TypeError, ValueError):
            continue
        if days > 0:
            return days
    return None


def _calculate_window_end_for_plan(
    plan: MembershipPlan,
    window_start: date,
    subscription_end: date,
) -> date:
    """
    Determine the final date (inclusive) for standing booking creation/materialization.

    The logic prioritizes explicit overrides, otherwise derives the window from plan duration:
    - duration_unit == "day"  -> clamp to the provided number of days.
    - duration_unit == "week" -> clamp to duration_value * 7 days.
    - For other units (month, year, etc.) fall back to the subscription end date that already
      reflects the configured duration via `_calculate_subscription_end`.
    """
    override_days = _get_plan_window_override(plan)
    if override_days:
        return min(subscription_end, window_start + timedelta(days=override_days - 1))

    try:
        duration_value = int(plan.duration_value)
    except (TypeError, ValueError):
        duration_value = None

    if plan.duration_unit == "day" and duration_value and duration_value > 0:
        return min(subscription_end, window_start + timedelta(days=duration_value - 1))

    if plan.duration_unit == "week" and duration_value and duration_value > 0:
        # The last local date can contain a class before the exclusive end instant.
        # Callers also filter full start_at/end_at timestamps, not just these dates.
        return min(subscription_end, window_start + timedelta(days=duration_value * 7))

    # For month/year (or any other unit) rely on subscription_end which already honors duration.
    return subscription_end
