"""``OverLimitError`` must roll the request transaction back, not commit it.

BLOCKER 1, Phase 5 review. ``common.exception_handlers.vinta_exception_handler``
returns a ``Response`` for ``OverLimitError``, which *swallows* the exception.
Under ``ATOMIC_REQUESTS = True`` (production) a swallowed exception means the
request transaction **commits** — so anything a guarded service wrote before it
reached the limit check would persist while the client is told 402. Phase 6a
guards ``accept_invitation`` (after a ``membership.is_active = True`` save),
``invite_user_to_organization`` (after an ``OrganizationInvitation``
``get_or_create``), and ``reactivate``, and the audit service writes on all three
— every one of those is a row that would survive a "rejected" request.

Exercised **through a real request**, not by calling the handler directly: the
handler in isolation cannot observe transaction state, and a direct-call test
passes identically whether or not ``set_rollback()`` is there. That is precisely
the failure mode this file exists to catch.
"""

from unittest import mock

from django.db import connection
from django.urls import path

import pytest
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from calendar_integration.models import CalendarGroup
from organizations.models import Organization
from payments.billing_constants import LimitedResource, LimitRemedy
from payments.exceptions import OverLimitError


class WriteThenExceedLimitView(APIView):
    """Stands in for a Phase 6a/6b guarded service method.

    Writes a row and *then* raises ``OverLimitError``, which is the real ordering:
    ``invite_user_to_organization`` creates the invitation and audit rows before
    the guard rejects, and ``accept_invitation`` flips ``is_active`` first.
    """

    authentication_classes = ()
    permission_classes = ()

    organization_id: int | None = None

    def post(self, request, *args, **kwargs):
        CalendarGroup.objects.create(
            organization_id=WriteThenExceedLimitView.organization_id,
            name="written-before-the-guard",
        )
        raise OverLimitError(
            resource_key=LimitedResource.CALENDAR_GROUPS,
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
            organization_id=WriteThenExceedLimitView.organization_id,
            name="written-and-kept",
        )
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


urlpatterns = [
    path("over-limit/", WriteThenExceedLimitView.as_view()),
    path("write-only/", WriteOnlyView.as_view()),
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
    WriteThenExceedLimitView.organization_id = org.pk
    yield org
    WriteThenExceedLimitView.organization_id = None


@pytest.mark.django_db
@pytest.mark.usefixtures("test_urlconf", "atomic_requests")
class TestOverLimitErrorRollsBackTheRequestTransaction:
    def test_the_control_write_persists_without_the_exception(self, anonymous_client, organization):
        """Guards the guard: if this fails, the assertions below prove nothing."""
        response = anonymous_client.post("/write-only/")

        assert response.status_code == status.HTTP_201_CREATED
        assert (
            CalendarGroup.objects.filter(
                organization_id=organization.pk, name="written-and-kept"
            ).count()
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
            CalendarGroup.objects.filter(
                organization_id=organization.pk, name="written-before-the-guard"
            ).count()
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
