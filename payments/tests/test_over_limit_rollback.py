"""``OverLimitError`` must roll the request transaction back, not commit it.

``common.exception_handlers.vinta_exception_handler`` returns a ``Response`` for
``OverLimitError``, which *swallows* the exception. Under ``ATOMIC_REQUESTS = True``
(production) a swallowed exception means the request transaction **commits** — so
anything a guarded service wrote before it reached the limit check would persist
while the client is told 402. The limit check runs on ``accept_invitation`` (after
a ``membership.is_active = True`` save), ``invite_user_to_organization`` (after an
``OrganizationInvitation`` ``get_or_create``), and ``reactivate``, and the audit
service writes on all three — every one of those is a row that would survive a
"rejected" request.

Exercised **through a real request**, not by calling the handler directly: the
handler in isolation cannot observe transaction state, and a direct-call test
passes identically whether or not ``set_rollback()`` is there. That is precisely
the failure mode this file exists to catch.
"""

import contextvars
from unittest import mock

from django.db import connection
from django.urls import path

import pytest
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from vinta_billing.constants import LimitRemedy
from vinta_billing.exceptions import OverLimitError, PaymentProviderNotConfiguredError

from calendar_integration.models import CalendarGroup
from organizations.models import Organization
from payments.seams.resource_keys import CALENDAR_GROUPS


#: The organization the two views below write against.
#:
#: A ``ContextVar`` rather than a class attribute on one of the views: class state
#: is module-global and survives a mid-test failure, so a test that blew up before
#: its teardown would leave a stale organization id set for every test after it —
#: and the other view had to reach across into a sibling view class to read it.
#: The fixture resets this token in a ``finally``, so the reset happens even then.
current_organization_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_organization_id", default=None
)


class WriteThenExceedLimitView(APIView):
    """Stands in for a guarded service method.

    Writes a row and *then* raises ``OverLimitError``, which is the real ordering:
    ``invite_user_to_organization`` creates the invitation and audit rows before
    the guard rejects, and ``accept_invitation`` flips ``is_active`` first.
    """

    authentication_classes = ()
    permission_classes = ()

    def post(self, request, *args, **kwargs):
        CalendarGroup.objects.create(
            organization_id=current_organization_id.get(),
            name="written-before-the-guard",
        )
        raise OverLimitError(
            resource_key=CALENDAR_GROUPS,
            current_usage=1,
            limit=1,
            remedy=LimitRemedy.PURCHASE_ADD_ON,
        )


class WriteOnlyView(APIView):
    """Control: same write, no exception. Proves the write itself does persist, so
    a passing rollback assertion cannot be an artifact of the write never landing.
    """

    authentication_classes = ()
    permission_classes = ()

    def post(self, request, *args, **kwargs):
        CalendarGroup.objects.create(
            organization_id=current_organization_id.get(),
            name="written-and-kept",
        )
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class WriteThenUnconfiguredProviderView(APIView):
    """Stands in for a billing write that resolves an adapter *after* writing.

    Mirrors the real ordering on the money paths that make this reachable:
    ``purchase_add_on`` creates the ``SubscriptionAddOn`` row and
    ``request_plan_change`` moves ``Subscription.plan`` before either drives the
    provider, so both have already written by the time adapter resolution can
    raise ``PaymentProviderNotConfiguredError``.
    """

    authentication_classes = ()
    permission_classes = ()

    def post(self, request, *args, **kwargs):
        CalendarGroup.objects.create(
            organization_id=current_organization_id.get(),
            name="written-before-the-provider-call",
        )
        raise PaymentProviderNotConfiguredError("stripe")


urlpatterns = [
    path("over-limit/", WriteThenExceedLimitView.as_view()),
    path("write-only/", WriteOnlyView.as_view()),
    path("unconfigured-provider/", WriteThenUnconfiguredProviderView.as_view()),
]


@pytest.fixture
def atomic_requests():
    """Turn on ``ATOMIC_REQUESTS`` for the duration of the test.

    Production-only setting (``vinta_schedule_api/settings/production.py``), and
    ``override_settings(DATABASES=...)`` would tear down the connection pytest-django
    wraps the test in. Patching the live connection's ``settings_dict`` is what
    Django's request handler actually reads, per request, in ``make_view_atomic``.

    Inside pytest-django's own test transaction this makes the request an atomic
    *savepoint*, so a rollback is still directly observable as the row vanishing.
    """
    with mock.patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}):
        yield


@pytest.fixture
def test_urlconf(settings):
    """Route the two views above without touching the project's real urlconf."""
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def organization():
    org = Organization.objects.create(name="rollback-test-org")
    token = current_organization_id.set(org.pk)
    try:
        yield org
    finally:
        current_organization_id.reset(token)


@pytest.mark.django_db
@pytest.mark.usefixtures("test_urlconf", "atomic_requests")
class TestOverLimitErrorRollsBackTheRequestTransaction:
    def test_the_control_write_persists_without_the_exception(self, anonymous_client, organization):
        """Guards the guard: if this fails, the assertions below prove nothing."""
        response = anonymous_client.post("/write-only/")

        assert response.status_code == status.HTTP_201_CREATED
        assert (
            CalendarGroup.objects.filter_by_organization(organization.pk)
            .filter(
                name="written-and-kept",
            )
            .count()
            == 1
        )

    def test_nothing_written_before_the_guard_survives_the_402(
        self, anonymous_client, organization
    ):
        """Without ``set_rollback()`` in the handler this row commits and the count
        is 1, while the client is handed a 402 saying the write did not happen."""
        response = anonymous_client.post("/over-limit/")

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert (
            CalendarGroup.objects.filter_by_organization(organization.pk)
            .filter(
                name="written-before-the-guard",
            )
            .count()
            == 0
        ), (
            "The row written before the over-limit guard was committed. The exception "
            "handler swallowed OverLimitError without calling set_rollback(), so "
            "ATOMIC_REQUESTS committed the request transaction."
        )

    def test_the_402_body_is_still_the_shared_contract(self, anonymous_client, organization):
        """Rolling back must not change what the client receives."""
        response = anonymous_client.post("/over-limit/")

        assert response.json() == {
            "detail": "Organization is at its limit for calendar groups.",
            "code": "limit_exceeded",
            "resource": "calendar_groups",
            "current_usage": 1,
            "limit": 1,
            "remedy": "purchase_add_on",
        }


@pytest.mark.django_db
@pytest.mark.usefixtures("test_urlconf", "atomic_requests")
class TestUnconfiguredProviderRollsBackTheRequestTransaction:
    """The same ``set_rollback()`` contract, for the 503 branch added alongside
    the 402 one.

    It matters more here, not less: the paths that raise this write local rows
    representing *paid* state -- a ``SubscriptionAddOn`` recording capacity, a
    ``Subscription.plan`` move onto a tier nobody was charged for, a ``Refund``
    and its status update. Committing any of those while telling the client the
    provider could not be reached is how an organization ends up with capacity it
    never paid for.
    """

    def test_nothing_written_before_the_provider_call_survives_the_503(
        self, anonymous_client, organization
    ):
        response = anonymous_client.post("/unconfigured-provider/")

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert (
            CalendarGroup.objects.filter_by_organization(organization.pk)
            .filter(
                name="written-before-the-provider-call",
            )
            .count()
            == 0
        ), (
            "The row written before the provider call was committed. The exception "
            "handler swallowed PaymentProviderNotConfiguredError without calling "
            "set_rollback(), so ATOMIC_REQUESTS committed the request transaction."
        )

    def test_the_503_body_carries_the_errors_message(self, anonymous_client, organization):
        """``PaymentProviderNotConfiguredError`` now renders through the shared
        ``BillingError.as_error_body()`` contract, so the body gains a stable
        ``code`` alongside the existing ``detail`` message."""
        response = anonymous_client.post("/unconfigured-provider/")

        assert response.json() == {
            "code": "payment_provider_not_configured",
            "detail": "Payment provider 'stripe' is not configured in this deployment",
        }
