from celery.schedules import crontab  # type: ignore


CELERYBEAT_SCHEDULE = {
    # Internal tasks
    "clearsessions": {
        "schedule": crontab(hour=3, minute=0),
        "task": "users.tasks.clearsessions",
    },
    "send_pending_notifications": {
        "schedule": crontab(minute="*/5"),
        "task": "notifications.tasks.periodic_send_pending_notifications_task",
    },
}
