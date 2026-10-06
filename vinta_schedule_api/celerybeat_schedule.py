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
    # (see `vinta_billing.jobs.METERING_SWEEP_WINDOW`) so consecutive runs overlap by
    # design: a run that never happened is made up for by the next one, because
    # re-metering an already-metered stretch inserts nothing. The cadence is
    # therefore a freshness knob, not a correctness one -- usage shows up in the
    # usage API within a quarter of an hour of happening.
    "meter_event_occurrences": {
        "schedule": crontab(minute="*/15"),
        "task": "payments.tasks.meter_event_occurrences",
    },
    # Grace/dunning sweep. Hourly rather than daily so a subscription whose grace
    # window elapses moves to RESTRICTED promptly rather than sitting unresolved
    # for up to a day; `DunningService`'s own per-subscription check
    # (`Subscription.last_dunning_attempt_at`, ~20h) is what keeps the actual
    # charge retry / ladder email to roughly once a day despite the hourly beat.
    "process_dunning": {
        "schedule": crontab(minute=0),
        "task": "payments.tasks.process_dunning",
    },
    # Proactive approaching-limit / limit-reached warnings. Every 15 minutes,
    # same cadence as `meter_event_occurrences` -- usage that crosses a
    # threshold shows up as a warning within a quarter of an hour, and
    # `LimitWarningNotification`'s per-cycle unique constraint (not the beat
    # cadence) is what keeps a still-crossed threshold from re-notifying on
    # every tick.
    "check_approaching_limits": {
        "schedule": crontab(minute="*/15"),
        "task": "payments.tasks.check_approaching_limits",
    },
    # Cycle close. Hourly so a subscription whose period ends is settled
    # (accrued overage charged, period rolled, postpaid counter reset) within the
    # hour rather than up to a day late. Idempotent per period: the rolled
    # `current_period_start` is the durable "already closed" marker and the overage
    # charge carries a `(subscription, period_start)` idempotency key, so an extra
    # tick over an already-closed subscription is a no-op and never double-charges.
    # Overage settles monthly even for annually-billed plans (the metering period is
    # monthly regardless of `billing_interval`).
    "close_billing_periods": {
        "schedule": crontab(minute=30),
        "task": "payments.tasks.close_billing_periods",
    },
    # Room and location resync for organizations with the
    # `resource_calendar_provider_sync` flag on: imports provider-side room
    # changes, provider-created rooms and provider deletions. Off the hour so it
    # does not stack on `process_dunning`. A missed tick is made up by the next:
    # each run compares the provider with the last-sync snapshot, not with the
    # previous run.
    "resync_provider_rooms": {
        "schedule": crontab(minute=45),
        "task": "calendar_integration.tasks.room_resync_tasks.resync_rooms_for_flagged_organizations_task",
    },
    # Synced-room create idempotency keys live 24 hours. Daily is enough: an expired
    # key that is still in the table is already ignored (and replaced) by the create.
    "purge_expired_resource_calendar_create_requests": {
        "schedule": crontab(hour=4, minute=15),
        "task": (
            "calendar_integration.tasks.room_create_request_tasks."
            "purge_expired_resource_calendar_create_requests_task"
        ),
    },
    # Microsoft room event subscriptions last just under 3 days. Renewing every 12
    # hours whatever expires within 24 hours renews each one at least once in time.
    # Only flag-on organizations (`resource_calendar_provider_sync`) are touched.
    "renew_microsoft_room_subscriptions": {
        "schedule": crontab(minute=15, hour="*/12"),
        "task": (
            "calendar_integration.tasks.room_event_sync_tasks."
            "renew_microsoft_room_subscriptions_task"
        ),
    },
    # Daily fallback for room notifications Graph never delivered: re-subscribe any
    # room without a live subscription and run its delta sync.
    "sweep_microsoft_room_events": {
        "schedule": crontab(minute=45, hour=4),
        "task": "calendar_integration.tasks.room_event_sync_tasks.sweep_microsoft_room_events_task",
    },
}
