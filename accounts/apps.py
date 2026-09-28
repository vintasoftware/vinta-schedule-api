from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self) -> None:
        # Importing the module connects its receiver to allauth's
        # `social_account_added`. The import is here and not at the top of the file
        # because the module imports models, and models cannot be imported while
        # `django.setup()` is still loading the apps.
        import accounts.signals  # noqa: F401
