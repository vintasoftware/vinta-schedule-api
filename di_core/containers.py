from audit_integration.containers import AuditContainer
from calendar_integration.containers import CalendarContainer
from legal.containers import LegalContainer
from notifications.containers import NotificationsContainer
from organizations.containers import OrganizationsContainer
from payments.containers import BillingContainer
from public_api.containers import PublicApiContainer
from webhooks.containers import WebhooksContainer


class AppContainer(
    OrganizationsContainer,
    CalendarContainer,
    PublicApiContainer,
    WebhooksContainer,
    LegalContainer,
    BillingContainer,
    NotificationsContainer,
    AuditContainer,
):
    """Composition of every domain container; it declares no provider of its own.

    Add a provider to the container of the app that owns it (``<app>/containers.py``).
    """


container: AppContainer | None = None  # set during app startup


def get_container() -> AppContainer:
    """The wired container, or a clear error if the app has not started yet.

    ``container`` is ``AppContainer | None`` because ``DICoreConfig.ready()`` is what
    assigns it (see ``di_core/apps.py``), so reading the global directly is only
    type-correct after narrowing. Most call sites never had to: they sit in functions
    with no annotations, whose bodies mypy skips by default. The ones that *are*
    annotated -- test fixtures declaring a return type -- had no way to say "startup has
    run" other than an assert each.

    Prefer this over importing the global in any annotated code.
    """
    if container is None:
        raise RuntimeError(
            "The DI container is not wired yet. It is set by DICoreConfig.ready(); "
            "this ran before django.setup() completed."
        )
    return container
