from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "payments"

    def ready(self) -> None:
        # Import the notification contexts module so @register_context decorators
        # run and register functions with vintasend's Contexts singleton on app load.
        #
        # Late, and it has to be: the import *is* the registration side effect, and
        # `ready()` is where Django guarantees it runs. At `apps.py` module scope it
        # would instead run while `django.setup()` is still populating the app
        # registry, which fails outright the moment a context reaches a model --
        # see `users/apps.py`, whose contexts module does.
        import payments.notification_contexts  # noqa: F401

        # This one *is* the mechanism, like `notification_contexts` above: the
        # import is what connects `payments/seams/audit.py`'s receiver to
        # `vinta_billing.signals.payment_provider_repointed`. Without it the
        # provider-repoint audit entry -- which `SubscriptionService` wrote
        # inline before the engine moved to the package -- is silently not
        # written.
        import payments.seams.audit  # noqa: F401

        # Same shape, different reason: this one is belt-and-braces, not the
        # mechanism. `payments.seams.resources` is *already* imported at every
        # process start -- `di_core.apps.DICoreConfig.ready()` calls
        # `container.wire(packages=INTERNAL_INSTALLED_APPS)`, which walks every
        # submodule under `payments` -- so the resource registry is populated
        # before this line runs. It is stated here anyway because `container.wire`
        # exists to find `@inject` call sites, not to import registries: nothing
        # in its contract promises it keeps walking every submodule, and if it
        # stops, an unpopulated registry fails as an empty limit table rather
        # than an ImportError. `Registry.register` treats an identical repeat
        # registration as a no-op, which is what makes saying it twice safe.
        import payments.seams.resources  # noqa: F401

        # Same mechanism again, for the other half of what the host's own
        # services did inline before the move: `payments/seams/resync.py`
        # receives `vinta_billing.signals.billing_restriction_lifted` and
        # resumes calendar sync for the pooled subtree. Without the import the
        # resync silently never happens, and an organization that has paid is
        # left with calendars that never catch up.
        import payments.seams.resync  # noqa: F401
