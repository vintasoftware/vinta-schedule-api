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
    leaf_value = providers.Object("leaf")
    greeting = providers.Factory(
        lambda prefix: f"{prefix}-hello", prefix=BaseContainer.config.prefix
    )


class Left(Leaf):
    left = providers.Factory(lambda v: f"left:{v}", v=Leaf.leaf_value)


class Right(Leaf):
    right = providers.Factory(lambda v: f"right:{v}", v=Leaf.leaf_value)


class Bottom(Left, Right):
    """Diamond: Left and Right share the Leaf base."""

    both = providers.Factory(lambda a, b: (a, b), a=Left.left, b=Right.right)


class Aliased(Leaf):
    # An alias line: the name is re-bound to the very same provider object.
    leaf_value = Leaf.leaf_value
    consumer = providers.Factory(lambda v: f"consumed:{v}", v=leaf_value)


def test_app_container_provider_names_are_unchanged() -> None:
    # ``__self__`` is bookkeeping dependency_injector adds to every container.
    assert set(AppContainer.providers) - {"__self__"} == EXPECTED_APP_CONTAINER_PROVIDERS


def test_diamond_inheritance_exposes_every_provider_once() -> None:
    assert {"leaf_value", "greeting", "left", "right", "both", "config"} <= set(Bottom.providers)

    container = Bottom()

    assert container.both() == ("left:leaf", "right:leaf")


def test_subclass_body_can_reference_upstream_provider() -> None:
    assert Left().left() == "left:leaf"


def test_base_container_config_reference_resolves_from_dict() -> None:
    container = Bottom()
    container.config.from_dict({"prefix": "hi"})

    assert container.greeting() == "hi-hello"


def test_config_is_declared_once_across_the_hierarchy() -> None:
    assert Bottom.config is BaseContainer.config
    assert AppContainer.config is BaseContainer.config


def test_alias_line_keeps_a_single_provider_object() -> None:
    assert Aliased.leaf_value is Leaf.leaf_value

    container = Aliased()

    assert container.leaf_value() == "leaf"
    assert container.consumer() == "consumed:leaf"
