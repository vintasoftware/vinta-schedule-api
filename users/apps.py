from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = "users"

    def ready(self):
        # Import signal handlers to ensure they are registered
        import users.notification_contexts  # noqa: F401
