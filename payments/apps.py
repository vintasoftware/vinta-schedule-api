from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "payments"

    def ready(self) -> None:
        # Import the notification contexts module so @register_context decorators
        # run and register functions with vintasend's Contexts singleton on app load.
        import payments.notification_contexts  # noqa: F401
