from django.apps import AppConfig
from django.conf import settings


class DICoreConfig(AppConfig):
    name = "di_core"

    def ready(self) -> None:
        # Late, and it has to be: `di_core.containers` imports every service in
        # the project, and those import models. `apps.py` module scope runs while
        # `django.setup()` is still populating the app registry, so hoisting this
        # raises `AppRegistryNotReady`. `ready()` is also the only place the
        # wiring below can happen -- it needs `settings` and the full app list.
        from di_core import containers

        container = containers.AppContainer()
        container.config.from_dict(settings.__dict__["_wrapped"].__dict__)

        container.wire(
            packages=getattr(settings, "INTERNAL_INSTALLED_APPS", []),
        )

        containers.container = container
