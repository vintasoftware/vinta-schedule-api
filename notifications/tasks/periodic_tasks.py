from vintasend.tasks.periodic_tasks import periodic_send_pending_notifications

from vinta_schedule_api.celery import app


# Register under the app namespace so the task name matches the celerybeat schedule
# entry ("notifications.tasks.periodic_send_pending_notifications_task"). The factory
# (periodic_send_pending_notifications_task_factory) registers it under the vendored
# module path instead, which beat cannot resolve -> "Received unregistered task".
periodic_send_pending_notifications_task = app.task(
    name="notifications.tasks.periodic_send_pending_notifications_task"
)(periodic_send_pending_notifications)
