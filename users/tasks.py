from django.core import management

from vinta_schedule_api import celery_app


@celery_app.task
def clearsessions():
    management.call_command("clearsessions")
