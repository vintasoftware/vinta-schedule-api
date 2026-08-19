"""The transitional re-export shims left behind by
``payments/migrations/0024_move_billing_to_vinta_billing.py``.

Every module named here is a one-line star re-export of its ``vinta_billing``
counterpart, kept so the ~40 consumer modules outside ``payments/`` can be
retargeted app by app in later phases instead of all at once. Phase 6 deletes
them. Until then these tests hold two properties:

1. **The shim is the same object, not a copy.** ``payments.models.Subscription``
   must *be* ``vinta_billing.models.Subscription`` -- an ``isinstance`` check or
   a queryset built through one and filtered through the other has to agree, and
   two same-named classes would pass a shallower test while failing that.
2. **``payments`` registers no models.** Django binds a model to the app that
   owns the module it is *defined* in, so re-exporting one does not register a
   second ``payments.Subscription``. If that ever stopped holding,
   ``makemigrations`` would start generating a migration for a table that does
   not exist.

Two shims are deliberately *not* pure re-exports, and are tested for what they
keep rather than only for what they forward:
``payments.billing_constants.LimitedResource`` / ``Entitlement`` (which became
registry keys, so the package has no equivalent) and
``payments.exceptions.InapplicableInvitationExclusionError`` (which guards a
concept this product has and a billing library cannot).
"""

import importlib

from django.apps import apps

import pytest


#: ``(shim module, package module, one name that must be identical)``. One
#: representative name per module is enough for identity -- the shims are star
#: imports, so a name that resolves at all resolves to the package's object --
#: but the full set is checked below via ``dir()``.
SHIMS = [
    ("payments.models", "vinta_billing.models", "Subscription"),
    ("payments.constants", "vinta_billing.constants", "PaymentProviders"),
    ("payments.exceptions", "vinta_billing.exceptions", "OverLimitError"),
    ("payments.provider_slugs", "vinta_billing.provider_slugs", "PAYMENT_PROVIDER_SLUGS"),
    ("payments.entitlement_cache", "vinta_billing.entitlement_cache", "has_entitlement_cached"),
    ("payments.admin", "vinta_billing.admin", "SubscriptionAdmin"),
    (
        "payments.services.entitlement_service",
        "vinta_billing.services.entitlement_service",
        "EntitlementService",
    ),
    (
        "payments.services.subscription_service",
        "vinta_billing.services.subscription_service",
        "SubscriptionService",
    ),
    (
        "payments.services.payment_service",
        "vinta_billing.services.payment_service",
        "PaymentService",
    ),
    (
        "payments.services.metering_service",
        "vinta_billing.services.metering_service",
        "MeteringService",
    ),
    (
        "payments.services.cycle_close_service",
        "vinta_billing.services.cycle_close_service",
        "CycleCloseService",
    ),
    (
        "payments.services.dunning_service",
        "vinta_billing.services.dunning_service",
        "DunningService",
    ),
    (
        "payments.services.usage_warning_service",
        "vinta_billing.services.usage_warning_service",
        "UsageWarningService",
    ),
    (
        "payments.services.billing_dataclasses",
        "vinta_billing.services.billing_dataclasses",
        "LimitCheckResult",
    ),
    ("payments.services.dataclasses", "vinta_billing.services.dataclasses", "Payment"),
    (
        "payments.services.billing_state_machine",
        "vinta_billing.services.billing_state_machine",
        "transition_billing_state",
    ),
    (
        "payments.services.payment_provider_resolver",
        "vinta_billing.services.payment_provider_resolver",
        "PaymentProviderResolver",
    ),
    (
        "payments.services.provider_credentials",
        "vinta_billing.services.provider_credentials",
        "resolve_public_credentials",
    ),
    (
        "payments.services.stripe_signature",
        "vinta_billing.services.stripe_signature",
        "verify_stripe_event",
    ),
    (
        "payments.services.mercadopago_signature",
        "vinta_billing.services.mercadopago_signature",
        "verify_mercadopago_signature",
    ),
    (
        "payments.services.payment_adapters.base",
        "vinta_billing.services.payment_adapters.base",
        "BasePaymentAdapter",
    ),
    (
        "payments.services.payment_adapters.stripe_payment_adapter",
        "vinta_billing.services.payment_adapters.stripe_payment_adapter",
        "StripePaymentAdapter",
    ),
    (
        "payments.services.payment_adapters.mercadopago_payment_adapter",
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter",
        "MercadoPagoPaymentAdapter",
    ),
    (
        "payments.services.subscription_adapters.base",
        "vinta_billing.services.subscription_adapters.base",
        "BaseSubscriptionAdapter",
    ),
    (
        "payments.services.subscription_adapters.stripe_subscription_adapter",
        "vinta_billing.services.subscription_adapters.stripe_subscription_adapter",
        "StripeSubscriptionAdapter",
    ),
    (
        "payments.services.subscription_adapters.mercadopago_subscription_adapter",
        "vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter",
        "MercadoPagoSubscriptionAdapter",
    ),
    (
        "payments.services.subscription_plan_factory.base",
        "vinta_billing.services.subscription_plan_factory.base",
        "BaseSubscriptionPlanFactory",
    ),
    (
        "payments.services.subscription_plan_factory.billing_plan_factory",
        "vinta_billing.services.subscription_plan_factory.billing_plan_factory",
        "BillingPlanFactory",
    ),
]

#: The seven ``billing_constants`` classes that *did* move to the package.
RE_EXPORTED_BILLING_CONSTANTS = [
    "BillingState",
    "BillingInterval",
    "DocumentTypes",
    "ProviderWebhookRoute",
    "LimitKind",
    "LimitRemedy",
    "LimitWarningLevel",
]


@pytest.mark.parametrize(("shim_path", "package_path", "name"), SHIMS)
def test_the_shim_re_exports_the_package_object_itself(shim_path, package_path, name):
    shim = importlib.import_module(shim_path)
    package = importlib.import_module(package_path)

    assert getattr(shim, name) is getattr(package, name)


@pytest.mark.parametrize(("shim_path", "package_path", "name"), SHIMS)
def test_every_public_name_the_shim_exposes_is_the_packages_own(shim_path, package_path, name):
    """Not just the representative name: nothing may be *shadowed*.

    A star import re-exports whatever the package module holds, so a name that
    differs between the two would mean the shim grew a definition of its own --
    which is exactly the drift these shims exist to prevent.
    """
    shim = importlib.import_module(shim_path)
    package = importlib.import_module(package_path)

    shared = [
        attribute
        for attribute in dir(package)
        if not attribute.startswith("_") and hasattr(shim, attribute)
    ]
    assert shared, f"{shim_path} re-exports nothing from {package_path}"
    divergent = [
        attribute
        for attribute in shared
        if getattr(shim, attribute) is not getattr(package, attribute)
    ]
    assert divergent == []


def test_payments_registers_no_models():
    """The property the whole shim strategy rests on.

    Were a re-export enough to register ``payments.Subscription``,
    ``makemigrations`` would want a table for it -- and there is no such table
    after ``0024``.
    """
    assert list(apps.get_app_config("payments").get_models()) == []


def test_vinta_billing_owns_the_twenty_billing_models():
    """The other half of the same statement: the models are somewhere, and that
    somewhere is the package's app label."""
    model_names = {model.__name__ for model in apps.get_app_config("vinta_billing").get_models()}

    assert len(model_names) == 20
    assert {"Subscription", "BillingPlan", "BillingProfile", "MeteredOccurrence"} <= model_names


@pytest.mark.parametrize("name", RE_EXPORTED_BILLING_CONSTANTS)
def test_billing_constants_re_exports_the_seven_that_moved(name):
    import vinta_billing.constants

    from payments import billing_constants

    assert getattr(billing_constants, name) is getattr(vinta_billing.constants, name)


@pytest.mark.parametrize("name", ["LimitedResource", "Entitlement"])
def test_billing_constants_still_defines_the_two_the_package_has_no_equivalent_for(name):
    """``LimitedResource`` and ``Entitlement`` became registry keys, by design --
    a billing library cannot own the closed set of things *this* product sells.
    They stay defined in the host until Phase 6 retires their last consumer."""
    import vinta_billing.constants

    from payments import billing_constants

    assert hasattr(billing_constants, name)
    assert not hasattr(vinta_billing.constants, name)
    assert getattr(billing_constants, name).__module__ == "payments.billing_constants"


def test_the_two_host_enums_still_agree_with_the_registry_they_were_replaced_by():
    """The bridge between the enum and its replacement, for as long as both
    exist. A key registered in ``payments/seams/resources.py`` that drifted from
    the enum would fail here rather than in whichever consumer read the stale
    one."""
    from vinta_billing.registry import entitlements, resources

    from payments.billing_constants import Entitlement, LimitedResource

    assert sorted(resources.keys()) == sorted(LimitedResource.values)
    assert sorted(entitlements.keys()) == sorted(Entitlement.values)


def test_exceptions_keeps_the_one_the_package_has_no_home_for():
    import vinta_billing.exceptions

    from payments.exceptions import BillingError, InapplicableInvitationExclusionError

    assert not hasattr(vinta_billing.exceptions, "InapplicableInvitationExclusionError")
    assert issubclass(InapplicableInvitationExclusionError, BillingError)
    # `BillingError` itself is the package's, so a host exception hanging off it
    # is still caught by every `except BillingError` in the package's own views.
    assert BillingError is vinta_billing.exceptions.BillingError
