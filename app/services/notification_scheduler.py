"""Daily scheduler for renewal-reminder and expired-membership notifications.

A single in-process ``AsyncIOScheduler`` (APScheduler) runs ``run_all_sweeps`` once a day at
``NOTIFICATION_SWEEP_HOUR`` (local time, America/Mexico_City). Duplicate sends are impossible
even if several app workers each start a scheduler, because every dispatch claims a unique
``dedup_key`` in ``notification_log`` before sending.

The scheduler is started/stopped from the FastAPI lifespan (see ``app/main.py``). If APScheduler
is not installed the app still boots — scheduling is simply disabled and the manual
``runNotificationSweep`` mutation can be used instead.
"""
import logging
import os

from app.services.notification_service import run_all_sweeps

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    _APSCHEDULER_AVAILABLE = True
except Exception:  # noqa: BLE001
    AsyncIOScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    _APSCHEDULER_AVAILABLE = False

_TIMEZONE = "America/Mexico_City"


def _sweep_hour() -> int:
    try:
        hour = int(os.getenv("NOTIFICATION_SWEEP_HOUR", "9"))
    except ValueError:
        hour = 9
    return min(max(hour, 0), 23)


def _campaign_interval_minutes() -> int:
    try:
        minutes = int(os.getenv("CAMPAIGN_SWEEP_INTERVAL_MIN", "5"))
    except ValueError:
        minutes = 5
    return min(max(minutes, 1), 60)


async def _run_sweeps_job() -> None:
    try:
        stats = await run_all_sweeps()
        logger.info("Notification sweep completed: %s", stats)
    except Exception:  # noqa: BLE001
        logger.exception("Notification sweep job failed")


async def _run_campaign_sweep_job() -> None:
    """Dispatch scheduled campaigns whose send time has arrived."""
    try:
        from app.services.campaign_service import run_campaign_sweep

        stats = await run_campaign_sweep()
        if stats.get("campaigns"):
            logger.info("Campaign sweep completed: %s", stats)
    except Exception:  # noqa: BLE001
        logger.exception("Campaign sweep job failed")


async def _run_conversion_sweep_job() -> None:
    """Attribute payment conversions to recipients still inside their window."""
    try:
        from app.services.campaign_service import run_conversion_sweep

        stats = await run_conversion_sweep()
        logger.info("Campaign conversion sweep completed: %s", stats)
    except Exception:  # noqa: BLE001
        logger.exception("Campaign conversion sweep job failed")


class NotificationScheduler:
    def __init__(self) -> None:
        self._scheduler = None

    def start(self) -> None:
        if not _APSCHEDULER_AVAILABLE:
            logger.warning(
                "APScheduler not installed; notification reminders will not run automatically. "
                "Install 'apscheduler' or trigger runNotificationSweep manually."
            )
            return
        if self._scheduler is not None:
            return
        hour = _sweep_hour()
        self._scheduler = AsyncIOScheduler(timezone=_TIMEZONE)
        self._scheduler.add_job(
            _run_sweeps_job,
            trigger=CronTrigger(hour=hour, minute=0, timezone=_TIMEZONE),
            id="notification_daily_sweep",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        # Campaigns piggyback the same scheduler: a frequent sweep dispatches scheduled
        # broadcasts, and a daily sweep attributes payment conversions.
        interval = _campaign_interval_minutes()
        self._scheduler.add_job(
            _run_campaign_sweep_job,
            trigger=IntervalTrigger(minutes=interval, timezone=_TIMEZONE),
            id="campaign_dispatch_sweep",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            _run_conversion_sweep_job,
            trigger=CronTrigger(hour=hour, minute=15, timezone=_TIMEZONE),
            id="campaign_conversion_sweep",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        logger.info(
            "Notification scheduler started (daily at %02d:00 %s; campaign sweep every %dm)",
            hour, _TIMEZONE, interval,
        )

    def stop(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._scheduler = None
            logger.info("Notification scheduler stopped")


scheduler = NotificationScheduler()
