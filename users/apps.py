from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = "users"

    def ready(self):
        # Import the notification contexts module so @register_context decorators
        # run and register functions with vintasend's Contexts singleton on app load.
        #
        # Late, and it has to be: `users.notification_contexts` imports
        # `users.models`, and `apps.py` module scope runs while `django.setup()`
        # is still populating the app registry -- hoisting this raises
        # `AppRegistryNotReady: Apps aren't loaded yet.` `ready()` is the first
        # point at which a model import is legal.
        import users.notification_contexts  # noqa: F401
