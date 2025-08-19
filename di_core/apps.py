from django.apps import AppConfig
from django.conf import settings


class DICoreConfig(AppConfig):
    name = "di_core"

    def ready(self) -> None:
        from di_core import containers

        container = containers.AppContainer()
        container.config.from_dict(settings.__dict__["_wrapped"].__dict__)

        container.wire(
            packages=getattr(settings, "INTERNAL_INSTALLED_APPS", []),
        )

        containers.container = container
