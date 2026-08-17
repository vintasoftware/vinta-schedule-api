from django.apps import AppConfig


class CalendarIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "calendar_integration"
    verbose_name = "Calendar Integration"

    def ready(self) -> None:
        # Import the notification contexts module so @register_context decorators
        # run and register functions with vintasend's Contexts singleton on app load.
        #
        # Late, and it has to be: the import *is* the registration side effect, and
        # `ready()` is where Django guarantees it runs. At `apps.py` module scope it
        # would instead run while `django.setup()` is still populating the app
        # registry, which fails outright the moment a context reaches a model --
        # see `users/apps.py`, whose contexts module does.
        import calendar_integration.notification_contexts  # noqa: F401
