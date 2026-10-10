from dependency_injector import providers

from di_core.base import BaseContainer
from di_core.containers import AppContainer


# Captured from the unmodified ``AppContainer`` before the composition split.
EXPECTED_APP_CONTAINER_PROVIDERS = {
    "appointment_type_service",
    "audit_additional_repositories",
    "audit_repository",
    "audit_service",
    "bookable_slots_service",
    "booking_policy_permission_service",
    "booking_policy_service",
    "calendar_permission_service",
    "calendar_service",
    "calendar_side_effects_service",
    "config",
    "consent_service",
    "cycle_close_service",
    "dunning_service",
    "entitlement_service",
    "external_client_identifier_service",
    "external_event_change_request_service",
    "metering_service",
    "notification_service",
    "organization_service",
    "payment_gateway",
    "payment_provider_registry",
    "payment_provider_resolver",
    "payment_service",
    "public_api_auth_service",
    "stripe_payment_gateway",
    "stripe_subscription_gateway",
    "subscription_gateway",
    "subscription_plan_factory",
    "subscription_provider_registry",
    "subscription_service",
    "usage_warning_service",
    "webhook_calendar_side_effects_service",
    "webhook_membership_side_effects_service",
    "webhook_service",
}


class Leaf(BaseContainer):
    shared = providers.Singleton(object)


class Left(Leaf):
    left = providers.Factory(lambda shared: shared, shared=Leaf.shared)
    greeting = providers.Factory(
        lambda prefix: f"{prefix}-hello", prefix=BaseContainer.config.prefix
    )


class Right(Leaf):
    right = providers.Factory(lambda shared: shared, shared=Leaf.shared)


class Bottom(Left, Right):
    """Diamond: Left and Right share the Leaf base."""


class Aliased(Leaf):
    # An alias line: the name is re-bound to the very same provider object.
    shared = Leaf.shared
    consumer = providers.Factory(lambda shared: shared, shared=shared)


def test_app_container_provider_names_are_unchanged() -> None:
    # ``__self__`` is bookkeeping dependency_injector adds to every container.
    assert set(AppContainer.providers) - {"__self__"} == EXPECTED_APP_CONTAINER_PROVIDERS


def test_diamond_inheritance_shares_one_upstream_provider() -> None:
    assert Bottom.shared is Leaf.shared

    container = Bottom()

    assert container.left() is container.right() is container.shared()


def test_subclass_body_can_reference_upstream_provider() -> None:
    container = Left()

    assert container.left() is container.shared()


def test_base_container_config_reference_resolves_from_dict() -> None:
    container = Bottom()
    container.config.from_dict({"prefix": "hi"})

    assert container.greeting() == "hi-hello"


def test_config_is_declared_once_across_the_hierarchy() -> None:
    assert Bottom.config is BaseContainer.config
    assert AppContainer.config is BaseContainer.config


def test_alias_line_keeps_a_single_provider_object() -> None:
    assert Aliased.shared is Leaf.shared

    container = Aliased()

    assert container.consumer() is container.shared()


def test_app_container_gateways_resolve_credentials_from_base_config() -> None:
    container = AppContainer()
    container.config.from_dict(
        {
            "STRIPE_SECRET_KEY": "sk",
            "STRIPE_WEBHOOK_SECRET": "wh",
            "MERCADOPAGO_ACCESS_TOKEN": "at",
            "MERCADOPAGO_WEBHOOK_SECRET": "mw",
        }
    )

    assert container.stripe_payment_gateway().is_configured is True
    assert container.stripe_subscription_gateway().is_configured is True
    assert container.payment_gateway().is_configured is True
    assert container.subscription_gateway().is_configured is True
