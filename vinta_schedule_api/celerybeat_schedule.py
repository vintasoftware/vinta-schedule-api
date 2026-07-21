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
    # Post-paid usage metering. Runs far more often than its six-hour sweep window
    # (see `payments.tasks.METERING_SWEEP_WINDOW`) so consecutive runs overlap by
    # design: a run that never happened is made up for by the next one, because
    # re-metering an already-metered stretch inserts nothing. The cadence is
    # therefore a freshness knob, not a correctness one -- usage shows up in the
    # usage API within a quarter of an hour of happening.
    "meter_event_occurrences": {
        "schedule": crontab(minute="*/15"),
        "task": "payments.tasks.meter_event_occurrences",
    },
    # Grace/dunning sweep (Phase 10). Hourly rather than daily so a subscription
    # whose grace window elapses moves to RESTRICTED promptly rather than sitting
    # unresolved for up to a day; `DunningService`'s own per-subscription gate
    # (`Subscription.last_dunning_attempt_at`, ~20h) is what keeps the actual
    # charge retry / ladder email to roughly once a day despite the hourly beat.
    "process_dunning": {
        "schedule": crontab(minute=0),
        "task": "payments.tasks.process_dunning",
    },
}
