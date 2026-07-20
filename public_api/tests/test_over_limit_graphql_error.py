"""``raise_over_limit_graphql_error`` — the GraphQL rendering of ``OverLimitError``.

Mirrors ``payments/tests/test_over_limit_rollback.py``'s reasoning for REST,
applied to GraphQL: graphql-core catches every resolver exception internally
and always returns a normal 200 response with the error embedded in
``errors``, so — unlike an unhandled exception in a plain Django view — the
request transaction never sees anything to propagate and roll back on its own.
Without ``set_rollback()`` in ``raise_over_limit_graphql_error``, a write a
guarded resolver made before it hit the limit check would commit under
``ATOMIC_REQUESTS`` while the client is told the request was rejected.

Exercised through a real request against a throwaway schema/urlconf, not by
calling the helper directly — a direct-call test cannot observe transaction
state and would pass identically whether or not ``set_rollback()`` is there.
"""

import contextvars
from unittest import mock

from django.db import connection
from django.urls import path

import pytest
import strawberry
from strawberry.django.views import GraphQLView

from calendar_integration.models import CalendarGroup
from organizations.models import Organization
from payments.billing_constants import LimitedResource, LimitRemedy
from payments.exceptions import OverLimitError
from public_api.extensions import raise_over_limit_graphql_error


#: Mirrors payments/tests/test_over_limit_rollback.py's use of a ContextVar
#: instead of class state, for the same reason: module-global class state
#: would survive a mid-test failure and leak into later tests.
current_organization_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_organization_id", default=None
)


@strawberry.type
class Query:
    @strawberry.field
    def ping(self) -> bool:
        return True


@strawberry.type
class Mutation:
    @strawberry.mutation
    def write_then_exceed_limit(self) -> bool:
        """Stands in for a guarded GraphQL resolver: writes a row and *then*
        raises, mirroring the real ordering risk this helper exists for."""
        CalendarGroup.objects.create(
            organization_id=current_organization_id.get(),
            name="written-before-the-graphql-guard",
        )
        raise_over_limit_graphql_error(
            OverLimitError(
                resource_key=LimitedResource.CALENDAR_GROUPS,
                current_usage=1,
                limit=1,
                remedy=LimitRemedy.PURCHASE_ADD_ON,
            )
        )

    @strawberry.mutation
    def write_only(self) -> bool:
        """Control: same write, no exception. Proves the write itself does
        persist, so a passing rollback assertion cannot be an artifact of the
        write never landing in the first place."""
        CalendarGroup.objects.create(
            organization_id=current_organization_id.get(),
            name="written-and-kept",
        )
        return True


test_schema = strawberry.Schema(query=Query, mutation=Mutation)

urlpatterns = [
    path("graphql-over-limit-test/", GraphQLView.as_view(schema=test_schema)),
]


@pytest.fixture
def atomic_requests():
    """See payments/tests/test_over_limit_rollback.py's identical fixture for
    why this patches the live connection rather than using
    ``override_settings(DATABASES=...)``."""
    with mock.patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}):
        yield


@pytest.fixture
def test_urlconf(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def organization():
    org = Organization.objects.create(name="graphql-rollback-test-org")
    token = current_organization_id.set(org.pk)
    try:
        yield org
    finally:
        current_organization_id.reset(token)


@pytest.mark.django_db
@pytest.mark.usefixtures("test_urlconf", "atomic_requests")
class TestOverLimitGraphQLErrorRollsBackTheRequestTransaction:
    def test_the_control_write_persists_without_the_exception(self, anonymous_client, organization):
        """Guards the guard: if this fails, the assertion below proves nothing."""
        response = anonymous_client.post(
            "/graphql-over-limit-test/",
            data={"query": "mutation { writeOnly }"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert "errors" not in response.json()
        assert (
            CalendarGroup.objects.filter(
                organization_id=organization.pk, name="written-and-kept"
            ).count()
            == 1
        )

    def test_nothing_written_before_the_guard_survives_the_response(
        self, anonymous_client, organization
    ):
        """Without ``set_rollback()`` this row commits and the count is 1, while
        the client is handed a structured over-limit error saying the write did
        not happen."""
        response = anonymous_client.post(
            "/graphql-over-limit-test/",
            data={"query": "mutation { writeThenExceedLimit }"},
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["errors"]) == 1
        assert (
            CalendarGroup.objects.filter(
                organization_id=organization.pk, name="written-before-the-graphql-guard"
            ).count()
            == 0
        ), (
            "The row written before the over-limit guard was committed. "
            "raise_over_limit_graphql_error swallowed the exception into a normal 200 "
            "response without calling set_rollback(), so ATOMIC_REQUESTS "
            "committed the request transaction."
        )

    def test_the_error_extensions_are_still_the_shared_contract(
        self, anonymous_client, organization
    ):
        """Rolling back must not change what the client receives."""
        response = anonymous_client.post(
            "/graphql-over-limit-test/",
            data={"query": "mutation { writeThenExceedLimit }"},
            content_type="application/json",
        )

        data = response.json()
        assert data["errors"][0]["extensions"] == {
            "detail": "Organization is at its limit for calendar groups.",
            "code": "limit_exceeded",
            "resource": "calendar_groups",
            "current_usage": 1,
            "limit": 1,
            "remedy": "purchase_add_on",
        }
