"""Version-two calendar periods; existing subscriptions retain their stored boundaries."""
from datetime import datetime, time, timedelta, timezone
from dateutil.relativedelta import relativedelta
from app.core.timezones import local_datetime, to_local


def period(plan, start_date):
    start = local_datetime(start_date, time.min)
    if plan.duration_unit == "month":
        end_day = start_date + relativedelta(months=plan.duration_value)
    elif plan.duration_unit == "year":
        end_day = start_date + relativedelta(years=plan.duration_value)
    else:
        end_day = start_date + timedelta(days=plan.duration_value * (7 if plan.duration_unit == "week" else 1))
    return start.astimezone(timezone.utc), local_datetime(end_day, time.min).astimezone(timezone.utc)


def last_day(end):
    return to_local(end - timedelta(microseconds=1)).date()
