"""Celery application.

Two queues by design:

* ``publishing`` — long-running, rate-limited work that talks to Meta.
* ``default``    — everything else (webhook fan-out, notifications, sync).

Separating them means a backlog of publishes can never delay a webhook-driven
notification, and each queue can be scaled independently.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "instamind",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # publishing must not be hoarded by one worker
    task_default_queue="default",
    task_routes={
        "instamind.publishing.*": {"queue": "publishing"},
        "instamind.webhooks.*": {"queue": "default"},
    },
    # A publish must not run forever; Meta's own timeouts are much shorter.
    task_soft_time_limit=600,
    task_time_limit=900,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "publish-due-jobs": {
            "task": "instamind.publishing.process_due_jobs",
            "schedule": 60.0,
        },
        "refresh-meta-tokens": {
            "task": "instamind.instagram.refresh_tokens",
            "schedule": crontab(hour=3, minute=0),
        },
        "purge-expired-media": {
            "task": "instamind.storage.purge_expired_media",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)
