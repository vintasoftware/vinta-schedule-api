"""``BillingError.code`` / ``as_error_body()`` -- the base contract every billing
error rendered by ``common.exception_handlers.vinta_exception_handler`` carries
(see ``payments/exceptions.py``'s ``BillingError`` docstring).

Two things matter here:

1. Every subclass the handler actually renders must override the base
   ``code = "billing_error"`` sentinel with its own stable, snake_case
   discriminator -- a subclass left on the default is a subclass the handler
   would render with an unusable, indistinguishable code.
2. ``OverLimitError`` is frozen (see the plan's Guiding Decisions): it keeps its
   own six-key ``as_error_body()`` override, byte-identical to before this
   promotion. The GraphQL error extension (``public_api/extensions.py``) and
   ``public_api/middlewares.py`` both consume that exact body -- a regression
   here would silently break both.
"""

from payments.exceptions import (
    AddOnNotPurchasableError,
    BillingError,
    CollectionNotSupportedError,
    NoOutstandingBalanceError,
    OverLimitError,
    PaymentProviderNotConfiguredError,
    PaymentTokenRequiredError,
    UnconfirmedPlanChangeError,
)


class TestBillingErrorBaseContract:
    def test_base_class_default_code_is_the_sentinel(self):
        """The base sentinel itself -- pinned so a change here is deliberate."""
        assert BillingError.code == "billing_error"

    def test_base_as_error_body_returns_the_two_key_shared_contract(self):
        error = BillingError("something went wrong")

        assert error.as_error_body() == {
            "code": "billing_error",
            "detail": "something went wrong",
        }


class TestEverySubclassRenderedByTheHandlerHasANonDefaultCode:
    """Every ``BillingError`` subclass ``common.exception_handlers
    .vinta_exception_handler`` renders must override the base sentinel with its
    own code -- a subclass left on ``"billing_error"`` would be indistinguishable
    from any other unhandled billing error to a client branching on ``code``.
    """

    def test_payment_token_required_error(self):
        error = PaymentTokenRequiredError(organization_id=1)

        assert error.code == "payment_token_required"
        assert error.code != BillingError.code

    def test_add_on_not_purchasable_error(self):
        error = AddOnNotPurchasableError(resource_key="resource_calendars")

        assert error.code == "add_on_not_purchasable"
        assert error.code != BillingError.code

    def test_unconfirmed_plan_change_error(self):
        error = UnconfirmedPlanChangeError(organization_id=1)

        assert error.code == "unconfirmed_plan_change"
        assert error.code != BillingError.code

    def test_payment_provider_not_configured_error(self):
        error = PaymentProviderNotConfiguredError(provider="stripe")

        assert error.code == "payment_provider_not_configured"
        assert error.code != BillingError.code

    def test_over_limit_error(self):
        error = OverLimitError(
            resource_key="organization_members",
            current_usage=1,
            limit=1,
            remedy="purchase_add_on",
        )

        assert error.code == "limit_exceeded"
        assert error.code != BillingError.code

    def test_no_outstanding_balance_error(self):
        error = NoOutstandingBalanceError(subscription_id=1)

        assert error.code == "no_outstanding_balance"
        assert error.code != BillingError.code

    def test_collection_not_supported_error(self):
        error = CollectionNotSupportedError(subscription_id=1, message="not supported")

        assert error.code == "collection_not_supported"
        assert error.code != BillingError.code


class TestNewSubclassesInheritTheBaseErrorBody:
    """A subclass with no ``as_error_body()`` override -- everything except
    ``OverLimitError`` -- must render through the inherited two-key contract."""

    def test_payment_token_required_error_renders_the_shared_two_key_body(self):
        error = PaymentTokenRequiredError(organization_id=42)

        assert error.as_error_body() == {
            "code": "payment_token_required",
            "detail": str(error),
        }

    def test_add_on_not_purchasable_error_renders_the_shared_two_key_body(self):
        error = AddOnNotPurchasableError(resource_key="resource_calendars")

        assert error.as_error_body() == {
            "code": "add_on_not_purchasable",
            "detail": str(error),
        }

    def test_unconfirmed_plan_change_error_renders_the_shared_two_key_body(self):
        error = UnconfirmedPlanChangeError(organization_id=42)

        assert error.as_error_body() == {
            "code": "unconfirmed_plan_change",
            "detail": str(error),
        }

    def test_payment_provider_not_configured_error_renders_the_shared_two_key_body(self):
        error = PaymentProviderNotConfiguredError(provider="stripe")

        assert error.as_error_body() == {
            "code": "payment_provider_not_configured",
            "detail": str(error),
        }

    def test_no_outstanding_balance_error_renders_the_shared_two_key_body(self):
        error = NoOutstandingBalanceError(subscription_id=42)

        assert error.as_error_body() == {
            "code": "no_outstanding_balance",
            "detail": str(error),
        }

    def test_collection_not_supported_error_renders_the_shared_two_key_body(self):
        error = CollectionNotSupportedError(subscription_id=42, message="not supported")

        assert error.as_error_body() == {
            "code": "collection_not_supported",
            "detail": str(error),
        }


class TestOverLimitErrorIsFrozen:
    """The single worst outcome of this phase would be ``OverLimitError``
    silently inheriting the two-key base body instead of keeping its own
    six-key override -- three separate surfaces (the DRF handler, the GraphQL
    error extension, ``public_api/middlewares.py``) consume the exact dict
    below. Asserted against a literal expected dict, not re-derived from the
    same attributes the implementation reads, so a change to the body would
    actually turn this test red.
    """

    def test_as_error_body_returns_the_exact_pre_existing_six_key_dict(self):
        error = OverLimitError(
            resource_key="organization_members",
            current_usage=10,
            limit=10,
            remedy="purchase_add_on",
        )

        assert error.as_error_body() == {
            "detail": "Organization is at its limit for organization members.",
            "code": "limit_exceeded",
            "resource": "organization_members",
            "current_usage": 10,
            "limit": 10,
            "remedy": "purchase_add_on",
        }
