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

    _APSCHEDULER_AVAILABLE = True
except Exception:  # noqa: BLE001
    AsyncIOScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    _APSCHEDULER_AVAILABLE = False

_TIMEZONE = "America/Mexico_City"


def _sweep_hour() -> int:
    try:
        hour = int(os.getenv("NOTIFICATION_SWEEP_HOUR", "9"))
    except ValueError:
        hour = 9
    return min(max(hour, 0), 23)


async def _run_sweeps_job() -> None:
    try:
        stats = await run_all_sweeps()
        logger.info("Notification sweep completed: %s", stats)
    except Exception:  # noqa: BLE001
        logger.exception("Notification sweep job failed")


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
        self._scheduler.start()
        logger.info("Notification scheduler started (daily at %02d:00 %s)", hour, _TIMEZONE)

    def stop(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._scheduler = None
            logger.info("Notification scheduler stopped")


scheduler = NotificationScheduler()
