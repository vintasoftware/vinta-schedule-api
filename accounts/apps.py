from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self) -> None:
        from accounts import signals

        signals.connect()
