"""The shared over-limit error contract, on the properties the package's own
``tests/test_exceptions.py`` does not pin.

``TestOverLimitErrorBody``'s 402-rendering counterpart moved to
``common/tests/test_exception_handlers.py`` (Phase 2 of the billing migration
retargeted that class there, since ``vinta_exception_handler`` -- the thing
under test -- is host code, not this module's concern). What is left here is
the error's own shape and class hierarchy: the six-key body's ``detail``
override (the package's own frozen-body test does not cover a custom
``detail``), and the two ``ValueError``-lineage guarantees that protect
specific host call sites in ``public_api/mutations.py`` and
``calendar_integration/views.py`` from a wrapper idiom that would flatten a
billing error to a plain message.
"""

import pytest

from payments.billing_constants import LimitedResource, LimitRemedy
from payments.exceptions import (
    BillingError,
    InvalidLimitCheckResultError,
    MissingBillingProfileError,
    OverLimitError,
    PaymentError,
)
from payments.services.billing_dataclasses import LimitCheckResult


def build_error():
    return OverLimitError(
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        current_usage=10,
        limit=10,
        remedy=LimitRemedy.PURCHASE_ADD_ON,
    )


class TestOverLimitErrorBody:
    def test_detail_can_be_overridden(self):
        error = OverLimitError(
            resource_key=LimitedResource.RESOURCE_CALENDARS,
            current_usage=3,
            limit=3,
            remedy=LimitRemedy.UPGRADE_PLAN,
            detail="Custom message.",
        )

        assert error.as_error_body()["detail"] == "Custom message."


def test_over_limit_error_is_a_billing_error():
    """Keeps it catchable alongside the rest of the payments exception tree."""
    with pytest.raises(BillingError):
        raise build_error()


def test_over_limit_error_is_not_a_value_error():
    """``public_api/mutations.py`` (:1319, :1379) and ``calendar_integration/views.py``
    (:833, :1392, :1501, :1632, :1697, :1741, :1861) wrap service calls in
    ``except ValueError as e: raise ...(str(e))``, which flattens an exception to
    its message. Several of those call sites touch resources that are
    ``LimitedResource`` members -- ``webhook_subscriptions`` among them -- so the
    moment a later change guards one, a ``ValueError`` lineage would silently drop
    ``code`` / ``resource`` / ``current_usage`` / ``limit`` / ``remedy`` and break
    the byte-identical-across-surfaces contract this error exists to provide.

    Written as an explicit non-membership assertion rather than a comment on
    ``OverLimitError``'s bases, because a future refactor that reparents it onto
    ``PaymentError`` (which *is* a ``ValueError``) would otherwise pass silently.
    """
    assert not isinstance(build_error(), ValueError)

    # And the shape that actually bites: the wrapper idiom used at those call sites
    # must not swallow it.
    caught_as_value_error = False
    try:
        raise build_error()
    except ValueError:
        caught_as_value_error = True
    except OverLimitError:
        pass
    assert caught_as_value_error is False


def test_payment_error_keeps_its_value_error_lineage():
    """The rest of the tree is unchanged: existing ``except ValueError`` handlers
    around payment-gateway calls must keep working."""
    assert issubclass(PaymentError, ValueError)
    assert issubclass(MissingBillingProfileError, ValueError)


class TestFromCheckResultInvariantViolations:
    """``from_check_result`` raising a broken-rule signal must not be a bare
    ``ValueError`` -- ``PaymentError`` is itself a ``ValueError``, so an upstream
    ``except ValueError`` wrapper (the same idiom
    ``test_over_limit_error_is_not_a_value_error`` protects ``OverLimitError``
    itself against) would flatten a genuine programming-error rule violation into a
    user-facing validation message."""

    def _allowed_result(self):
        return LimitCheckResult(
            allowed=True,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            current_usage=None,
            ceiling=None,
        )

    def _incomplete_blocked_result(self):
        return LimitCheckResult(
            allowed=False,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            current_usage=None,
            ceiling=None,
            remedy=None,
        )

    def test_called_on_an_allowed_result_raises_a_billing_error_not_a_bare_value_error(self):
        with pytest.raises(InvalidLimitCheckResultError) as exc_info:
            OverLimitError.from_check_result(self._allowed_result())

        assert isinstance(exc_info.value, BillingError)
        assert not isinstance(exc_info.value, ValueError)

    def test_called_on_an_incomplete_blocked_result_raises_a_billing_error_not_a_bare_value_error(
        self,
    ):
        with pytest.raises(InvalidLimitCheckResultError) as exc_info:
            OverLimitError.from_check_result(self._incomplete_blocked_result())

        assert isinstance(exc_info.value, BillingError)
        assert not isinstance(exc_info.value, ValueError)
