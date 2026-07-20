"""The shared over-limit error contract.

Every guarded surface raises ``OverLimitError`` and every surface must render it
identically — a client that handles the REST body must handle the GraphQL one
without a second code path. These tests pin the body shape and the 402 status so
a future surface cannot quietly drift from it.
"""

import pytest
from rest_framework import status
from rest_framework.exceptions import NotFound

from common.exception_handlers import vinta_exception_handler
from payments.billing_constants import LimitedResource, LimitRemedy
from payments.exceptions import OverLimitError


def build_error():
    return OverLimitError(
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        current_usage=10,
        limit=10,
        remedy=LimitRemedy.PURCHASE_ADD_ON,
    )


class TestOverLimitErrorBody:
    def test_body_matches_the_documented_contract(self):
        assert build_error().as_error_body() == {
            "detail": "Organization is at its limit for organization members.",
            "code": "limit_exceeded",
            "resource": "organization_members",
            "current_usage": 10,
            "limit": 10,
            "remedy": "purchase_add_on",
        }

    def test_detail_can_be_overridden(self):
        error = OverLimitError(
            resource_key=LimitedResource.RESOURCE_CALENDARS,
            current_usage=3,
            limit=3,
            remedy=LimitRemedy.UPGRADE_PLAN,
            detail="Custom message.",
        )

        assert error.as_error_body()["detail"] == "Custom message."

    def test_unknown_resource_key_degrades_to_the_raw_key(self):
        """Building an error must never itself raise — an unrecognized key falls
        back to the key rather than blowing up inside an error path."""
        error = OverLimitError(
            resource_key="not_a_real_resource",
            current_usage=1,
            limit=1,
            remedy=LimitRemedy.UPGRADE_PLAN,
        )

        assert error.as_error_body()["detail"] == (
            "Organization is at its limit for not_a_real_resource."
        )


class TestExceptionHandler:
    def test_over_limit_error_renders_as_402_with_the_contract_body(self):
        """402 rather than 403 so a client can tell "not allowed" from "out of
        capacity" without parsing the message."""
        response = vinta_exception_handler(build_error(), {})

        assert response is not None
        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.data == build_error().as_error_body()

    def test_other_exceptions_still_use_drfs_default_rendering(self):
        """The handler is project-wide; adding it must not change any existing
        error's rendering."""
        response = vinta_exception_handler(NotFound(), {})

        assert response is not None
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unhandled_exception_is_left_to_django(self):
        assert vinta_exception_handler(ValueError("boom"), {}) is None


def test_over_limit_error_is_a_payment_error():
    """Keeps it catchable alongside the rest of the payments exception tree."""
    from payments.exceptions import PaymentError

    with pytest.raises(PaymentError):
        raise build_error()
