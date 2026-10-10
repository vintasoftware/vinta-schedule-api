from collections import Counter

from dependency_injector import providers

from di_core.base import BaseContainer
from di_core.containers import AppContainer


# Read from the real composition, so a container added to ``AppContainer`` is checked too.
DOMAIN_CONTAINERS = [
    container
    for container in AppContainer.__mro__
    if issubclass(container, BaseContainer) and container not in (AppContainer, BaseContainer)
]


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

    stripe_payment = container.stripe_payment_gateway()
    stripe_subscription = container.stripe_subscription_gateway()
    mercadopago_payment = container.payment_gateway()
    mercadopago_subscription = container.subscription_gateway()

    assert (stripe_payment.api_key, stripe_payment.webhook_secret) == ("sk", "wh")
    assert (stripe_subscription.api_key, stripe_subscription.webhook_secret) == ("sk", "wh")
    assert (mercadopago_payment.access_token, mercadopago_payment.webhook_secret) == ("at", "mw")
    assert (
        mercadopago_subscription.access_token,
        mercadopago_subscription.webhook_secret,
    ) == ("at", "mw")


def test_no_provider_name_is_declared_in_more_than_one_domain_container() -> None:
    declared = Counter(
        name
        for container in DOMAIN_CONTAINERS
        for name in container.cls_providers
        if name != "config"
    )

    assert {name: count for name, count in declared.items() if count > 1} == {}


def test_app_container_declares_no_provider_of_its_own() -> None:
    assert set(AppContainer.cls_providers) == set()


def test_each_domain_container_lives_in_its_app_containers_module() -> None:
    assert len(DOMAIN_CONTAINERS) == 8
    assert all(container.__module__.endswith(".containers") for container in DOMAIN_CONTAINERS)
