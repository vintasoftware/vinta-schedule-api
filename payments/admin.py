"""This project's registration of ``vinta_billing``'s admin.

Importing ``vinta_billing.admin`` is what registers every billing ``ModelAdmin``
with the default admin site, so ``django.contrib.admin.autodiscover``'s import of
this module keeps having the same effect it had before the engine moved. The
star import below preserves that, and every name the old module exported.

One class is re-registered rather than taken as shipped.
``vinta_billing.admin.BillingProfileAdmin.save_model`` routes a
``payment_provider`` edit through ``SubscriptionService.set_payment_provider``
(so a staff repoint is audited like every other billing business write) and
takes that service as a plain ``subscription_service=None`` argument, raising
``RuntimeError`` when it is missing. Nothing in the package ever supplies it:
0.4.0 removed ``dependency_injector`` in favour of falling back to
``vinta_billing.services.container``, and this one call site was left without
the fallback -- so on the package's own wiring the edit always raises. Reported
upstream; when a release closes it, this subclass can go.

Supplying it is this project's job either way. The plan's "DI ownership: the
host's" decision keeps ``di_core.containers`` in charge of constructing every
billing service, and ``container.wire(packages=INTERNAL_INSTALLED_APPS)``
already covers this module -- which is exactly how the host's own admin got the
service before the move. The ``@inject`` below is that same wiring, restored.
"""

from typing import TYPE_CHECKING, Annotated, Any

from django.contrib import admin
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject
from vinta_billing.admin import *  # noqa: F403
from vinta_billing.admin import BillingProfileAdmin as PackageBillingProfileAdmin
from vinta_billing.models import BillingProfile


if TYPE_CHECKING:
    from vinta_billing.services.subscription_service import SubscriptionService


admin.site.unregister(BillingProfile)


@admin.register(BillingProfile)
class BillingProfileAdmin(PackageBillingProfileAdmin):
    """The package's billing-profile admin, wired to this project's container."""

    @inject
    def save_model(
        self,
        request: HttpRequest,
        obj: BillingProfile,
        form: Any,
        change: bool,
        subscription_service: Annotated[
            "SubscriptionService", Provide["subscription_service"]
        ] = None,  # type: ignore[assignment]
    ) -> None:
        super().save_model(request, obj, form, change, subscription_service=subscription_service)
