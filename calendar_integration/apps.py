from django.apps import AppConfig


class CalendarIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "calendar_integration"
    verbose_name = "Calendar Integration"

    def ready(self) -> None:
        # Import the notification contexts module so @register_context decorators
        # run and register functions with vintasend's Contexts singleton on app load.
        import calendar_integration.notification_contexts  # noqa: F401
