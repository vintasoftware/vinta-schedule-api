"""Integration tests for external client identifiers on the public GraphQL API.

Covers Phase 3 of the External Client Identifiers plan:
- ``scheduleEvent`` persists identifiers on the event and on external attendees, and
  reads them back in the same response.
- ``scheduleEvent`` that omits ``externalClientIdentifiers`` entirely behaves exactly
  like the pre-feature mutation (no identifier rows written) -- the no-op regression
  test standing in for a flag-off test.
- A duplicate ``(system, identifier)`` (already claimed by another record of the same
  type in the organization) is rejected and creates no event.
- ``calendarEvents`` filters by ``(system, identifier)``, requires both arguments
  together, and composes with owner-scope / organization scoping so a scoped token or
  a foreign organization never sees another owner's/org's event through a colliding
  identifier pair.
"""

import datetime
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
import strawberry
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    ExternalClientIdentifier,
    RecurrenceRule,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.mutations import ExternalClientIdentifierInput, _map_external_client_identifiers
from public_api.services import PublicAPIAuthService


def assert_graphql_success(response):
    assert response.status_code == 200, response.content.decode()
    data = response.json()
    if data.get("errors"):
        raise AssertionError(f"GraphQL errors: {data['errors']}")
    assert "data" in data and data["data"] is not None, data
    return data["data"]


def assert_graphql_error(response):
    assert response.status_code == 200, response.content.decode()
    data = response.json()
    assert data.get("errors"), f"Expected a GraphQL error, got: {data}"
    return data["errors"]


# ---------------------------------------------------------------------------
# _map_external_client_identifiers -- direct unit coverage of the UNSET -> None
# mapping, the single highest-risk detail in the plan.
# ---------------------------------------------------------------------------


class TestMapExternalClientIdentifiers:
    def test_unset_maps_to_none(self):
        """UNSET (omitted) must map to None -- "leave untouched" -- never to []."""
        assert _map_external_client_identifiers(strawberry.UNSET) is None

    def test_explicit_none_maps_to_empty_list(self):
        assert _map_external_client_identifiers(None) == []

    def test_explicit_empty_list_maps_to_empty_list(self):
        assert _map_external_client_identifiers([]) == []

    def test_explicit_values_map_through(self):
        result = _map_external_client_identifiers(
            [ExternalClientIdentifierInput(system="https://crm.example.com", identifier="deal-1")]
        )
        assert result is not None
        assert len(result) == 1
        assert result[0].system == "https://crm.example.com"
        assert result[0].identifier == "deal-1"


# ---------------------------------------------------------------------------
# scheduleEvent -- write + read-back, no-op regression, duplicate rejection
# ---------------------------------------------------------------------------

_SCHEDULE_EVENT_WITH_IDENTIFIERS = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
        externalClientIdentifiers {
            system
            identifier
        }
        externalAttendees {
            email
            externalClientIdentifiers {
                system
                identifier
            }
        }
    }
}
"""

_SCHEDULE_EVENT_PLAIN = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
    }
}
"""

_CALENDAR_EVENTS_BY_IDENTIFIER = """
query CalendarEvents(
    $system: String, $identifier: String
) {
    calendarEvents(
        externalClientIdentifierSystem: $system,
        externalClientIdentifierIdentifier: $identifier
    ) {
        id
        title
    }
}
"""

_CALENDAR_EVENTS_BY_CALENDAR = """
query CalendarEvents(
    $calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!,
    $system: String, $identifier: String
) {
    calendarEvents(
        calendarId: $calendarId,
        startDatetime: $startDatetime,
        endDatetime: $endDatetime,
        externalClientIdentifierSystem: $system,
        externalClientIdentifierIdentifier: $identifier
    ) {
        id
        title
        externalClientIdentifiers {
            system
            identifier
        }
    }
}
"""

_CALENDAR_EVENTS_BY_USER = """
query CalendarEvents(
    $userId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!,
    $system: String, $identifier: String
) {
    calendarEvents(
        userId: $userId,
        startDatetime: $startDatetime,
        endDatetime: $endDatetime,
        externalClientIdentifierSystem: $system,
        externalClientIdentifierIdentifier: $identifier
    ) {
        id
        title
        externalClientIdentifiers {
            system
            identifier
        }
    }
}
"""

# ``id`` is non-nullable on ``CalendarEventGraphQLType``, and generated recurring
# occurrences are in-memory copies with no real pk (``id is None``) -- a pre-existing,
# out-of-scope gap unrelated to this fix. The recurring-series tests below select
# ``title``/``startTime`` instead so they can assert on occurrences without tripping it.
_CALENDAR_EVENTS_BY_CALENDAR_NO_ID = """
query CalendarEvents(
    $calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!,
    $system: String, $identifier: String
) {
    calendarEvents(
        calendarId: $calendarId,
        startDatetime: $startDatetime,
        endDatetime: $endDatetime,
        externalClientIdentifierSystem: $system,
        externalClientIdentifierIdentifier: $identifier
    ) {
        title
        startTime
        externalClientIdentifiers {
            system
            identifier
        }
    }
}
"""

_CALENDAR_EVENTS_BY_USER_NO_ID = """
query CalendarEvents(
    $userId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!,
    $system: String, $identifier: String
) {
    calendarEvents(
        userId: $userId,
        startDatetime: $startDatetime,
        endDatetime: $endDatetime,
        externalClientIdentifierSystem: $system,
        externalClientIdentifierIdentifier: $identifier
    ) {
        title
        startTime
        externalClientIdentifiers {
            system
            identifier
        }
    }
}
"""


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestExternalClientIdentifiersGraphQL:
    """Covers scheduleEvent writes/reads and calendarEvents filtering.

    A single class so the write-side and read-side helpers (org/owner/calendar
    scaffolding, scoped vs org-wide clients) can be shared.
    """

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Scaffolding helpers -- mirror TestScopedTokenScheduleEvent /
    # TestOwnerScopedTokenReadEnforcement in the neighboring test files.
    # ------------------------------------------------------------------

    def _make_org(self):
        return baker.make(Organization, name=f"ExtClientId Org {uuid.uuid4().hex[:8]}")

    def _make_owner_with_calendar(self, org):
        unique = uuid.uuid4().hex[:8]
        user_model = get_user_model()
        owner = baker.make(user_model, email=f"owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        calendar = baker.make(
            Calendar,
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            manage_available_windows=True,
            provider=CalendarProvider.INTERNAL,
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=owner.id,
            organization=org,
        )
        return owner, membership, calendar

    def _make_scoped_system_user(self, org, membership, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_org_wide_system_user(self, org, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"org_wide_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _seed_window(self, org, system_user, calendar):
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        return calendar_service.create_available_time(
            calendar=calendar,
            start_time=datetime.datetime(2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 10, 1, 18, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _post_json(self, query, system_user, token, auth_service, variables):
        """Same as ``_post`` but serialized as raw JSON (used for the query-side tests,
        matching the neighboring ``test_queries.py`` convention)."""
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _event_input(self, org, calendar, **overrides):
        base = {
            "organizationId": org.id,
            "calendarId": calendar.id,
            "startTime": datetime.datetime(2026, 10, 1, 10, 0, 0, tzinfo=datetime.UTC).isoformat(),
            "endTime": datetime.datetime(2026, 10, 1, 11, 0, 0, tzinfo=datetime.UTC).isoformat(),
            "timezone": "UTC",
            "title": "Scheduled Visit",
        }
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # Write + read-back
    # ------------------------------------------------------------------

    def test_schedule_event_persists_and_reads_back_event_and_attendee_identifiers(
        self, mock_rate_limiter
    ):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_WITH_IDENTIFIERS,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "deal-1"}
                    ],
                    externalAttendees=[
                        {
                            "email": "guest@example.com",
                            "name": "Guest",
                            "externalClientIdentifiers": [
                                {"system": "https://crm.example.com", "identifier": "contact-1"}
                            ],
                        }
                    ],
                )
            },
        )

        data = assert_graphql_success(response)
        result = data["scheduleEvent"]
        assert result["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-1"}
        ]
        assert len(result["externalAttendees"]) == 1
        assert result["externalAttendees"][0]["email"] == "guest@example.com"
        assert result["externalAttendees"][0]["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-1"}
        ]

        # Persisted, not just echoed back.
        event = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert (
            ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(system="https://crm.example.com", identifier="deal-1")
            .filter(identified_key=event.pk)
            .exists()
        )

    def test_schedule_event_omitting_field_writes_no_identifier_rows(self, mock_rate_limiter):
        """The no-op regression test: a caller that never mentions the field sees the
        pre-feature mutation exactly -- no identifier rows, no error, unaffected
        title/attendee behavior.

        Note (see TestMapExternalClientIdentifiers): scheduleEvent only ever *creates*
        an event, so there is nothing pre-existing an incorrect UNSET->[] mapping could
        wipe -- both a correct (None) and an incorrect ([]) mapping write zero rows
        here. The mapping itself (UNSET -> None, distinct from [] -> clear) is pinned
        directly and is capable of failing there; this test instead pins that omitting
        the field is a true no-op end to end: no error, no identifier rows, and the
        rest of the mutation (title, id) behaves exactly as it did before this field
        existed.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {"input": self._event_input(org, calendar)},
        )

        data = assert_graphql_success(response)
        result = data["scheduleEvent"]
        assert result["title"] == "Scheduled Visit"
        event_id = int(result["id"])

        assert (
            not ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(identified_key=event_id)
            .exists()
        )

    def test_schedule_event_duplicate_identifier_errors_and_creates_no_event(
        self, mock_rate_limiter
    ):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        existing_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Existing Event",
            external_id=f"existing-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=existing_event,
            system="https://crm.example.com",
            identifier="deal-collision",
        )

        count_before = CalendarEvent.objects.filter_by_organization(org.id).count()

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    title="New Colliding Event",
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "deal-collision"}
                    ],
                )
            },
        )

        assert_graphql_error(response)
        assert CalendarEvent.objects.filter_by_organization(org.id).count() == count_before
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(title="New Colliding Event")
            .exists()
        )

    def test_schedule_event_invalid_system_url_errors(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    externalClientIdentifiers=[{"system": "not-a-url", "identifier": "deal-1"}],
                )
            },
        )

        assert_graphql_error(response)
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(title="Scheduled Visit")
            .exists()
        )

    def test_schedule_event_blank_identifier_errors(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "   "}
                    ],
                )
            },
        )

        assert_graphql_error(response)

    def test_schedule_event_too_long_identifier_errors(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "x" * 256}
                    ],
                )
            },
        )

        assert_graphql_error(response)

    def test_schedule_event_duplicate_normalized_system_in_one_payload_errors(
        self, mock_rate_limiter
    ):
        """Phase 2's fifth error case: two pairs in one payload normalize to the same
        ``system``. Must surface as a GraphQL error, not silently keep the last one."""
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_PLAIN,
            system_user,
            token,
            auth_service,
            {
                "input": self._event_input(
                    org,
                    calendar,
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "A"},
                        {"system": "HTTPS://CRM.EXAMPLE.COM/", "identifier": "B"},
                    ],
                )
            },
        )

        assert_graphql_error(response)
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(title="Scheduled Visit")
            .exists()
        )

    # ------------------------------------------------------------------
    # calendarEvents filtering
    # ------------------------------------------------------------------

    def test_calendar_events_filter_by_identifier_returns_only_matching_event(
        self, mock_rate_limiter
    ):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        matching_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Matching Event",
            external_id=f"match-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=matching_event,
            system="https://crm.example.com",
            identifier="deal-1",
        )
        other_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Other Event",
            external_id=f"other-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=other_event,
            system="https://crm.example.com",
            identifier="deal-2",
        )

        response = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER,
            system_user,
            token,
            auth_service,
            {"system": "https://crm.example.com", "identifier": "deal-1"},
        )

        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        assert len(events) == 1
        assert int(events[0]["id"]) == matching_event.id

    def test_calendar_events_filter_normalizes_system_before_matching(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Normalized Match",
            external_id=f"norm-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=event,
            system="https://crm.example.com",
            identifier="deal-1",
        )

        response = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER,
            system_user,
            token,
            auth_service,
            # Un-normalized: different case, trailing slash.
            {"system": "HTTPS://CRM.Example.com/", "identifier": "deal-1"},
        )

        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        assert len(events) == 1
        assert int(events[0]["id"]) == event.id

    def test_calendar_events_filter_one_argument_alone_errors(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, _calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER,
            system_user,
            token,
            auth_service,
            {"system": "https://crm.example.com", "identifier": None},
        )

        assert_graphql_error(response)

    def test_calendar_events_filter_scoped_token_cannot_see_other_owners_event(
        self, mock_rate_limiter
    ):
        """An owner-scoped token filtering by an identifier on another owner's event
        gets an empty result -- not that event.

        Proven capable of failing: the SAME (system, identifier) pair, queried by an
        org-wide token (no owner scoping applied), DOES return the other owner's event
        -- so the row genuinely exists and genuinely matches; only the owner-scope
        narrowing is what hides it from the scoped token.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)

        other_owners_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar_b,
            title="Other Owner's Event",
            external_id=f"ownerb-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=other_owners_event,
            system="https://crm.example.com",
            identifier="deal-owner-b",
        )

        scoped_system_user, scoped_token, scoped_auth_service = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.CALENDAR_EVENT]
        )
        org_wide_system_user, org_wide_token, org_wide_auth_service = (
            self._make_org_wide_system_user(org, [PublicAPIResources.CALENDAR_EVENT])
        )

        variables = {"system": "https://crm.example.com", "identifier": "deal-owner-b"}

        scoped_response = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER,
            scoped_system_user,
            scoped_token,
            scoped_auth_service,
            variables,
        )
        scoped_data = assert_graphql_success(scoped_response)
        assert scoped_data["calendarEvents"] == [], (
            "Scoped token must not see another owner's event via identifier lookup"
        )

        # Prove the row is real and the filter itself works: an org-wide token
        # (no owner-scope narrowing) DOES find it.
        org_wide_response = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER,
            org_wide_system_user,
            org_wide_token,
            org_wide_auth_service,
            variables,
        )
        org_wide_data = assert_graphql_success(org_wide_response)
        assert len(org_wide_data["calendarEvents"]) == 1
        assert int(org_wide_data["calendarEvents"][0]["id"]) == other_owners_event.id

    def test_calendar_events_filter_cross_organization_isolation(self, mock_rate_limiter):
        """A token from another organization filtering by a colliding (system,
        identifier) gets an empty result -- never the other organization's event.

        Proven capable of failing: both organizations are seeded with the identical
        (system, identifier) pair on their OWN event (legal -- the unique constraint is
        scoped by organization), and each organization's token is asserted to see only
        its own event, never the other's. If the CalendarEvent-side organization filter
        were dropped, the join would return both organizations' rows for either token.
        """
        mock_rate_limiter.return_value = iter([None])
        org_a = self._make_org()
        org_b = self._make_org()
        _owner_a, _membership_a, calendar_a = self._make_owner_with_calendar(org_a)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org_b)

        event_a = baker.make(
            CalendarEvent,
            organization=org_a,
            calendar=calendar_a,
            title="Org A Event",
            external_id=f"orga-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org_a,
            identified_object=event_a,
            system="https://crm.example.com",
            identifier="deal-shared",
        )
        event_b = baker.make(
            CalendarEvent,
            organization=org_b,
            calendar=calendar_b,
            title="Org B Event",
            external_id=f"orgb-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=org_b,
            identified_object=event_b,
            system="https://crm.example.com",
            identifier="deal-shared",
        )

        system_user_a, token_a, auth_service_a = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.CALENDAR_EVENT]
        )
        system_user_b, token_b, auth_service_b = self._make_org_wide_system_user(
            org_b, [PublicAPIResources.CALENDAR_EVENT]
        )

        variables = {"system": "https://crm.example.com", "identifier": "deal-shared"}

        response_a = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER, system_user_a, token_a, auth_service_a, variables
        )
        data_a = assert_graphql_success(response_a)
        assert len(data_a["calendarEvents"]) == 1
        assert int(data_a["calendarEvents"][0]["id"]) == event_a.id

        response_b = self._post_json(
            _CALENDAR_EVENTS_BY_IDENTIFIER, system_user_b, token_b, auth_service_b, variables
        )
        data_b = assert_graphql_success(response_b)
        assert len(data_b["calendarEvents"]) == 1
        assert int(data_b["calendarEvents"][0]["id"]) == event_b.id

    # ------------------------------------------------------------------
    # calendarId/userId branches -- identifier prefetch (N+1), recurring-series
    # matching, and combined-mode narrowing
    # ------------------------------------------------------------------

    def _make_recurring_master(self, org, calendar, *, external_id=None):
        rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=3", org)
        rule.save()
        master = CalendarEvent.objects.create(
            title="Recurring Series",
            description="",
            start_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 30),
            timezone="UTC",
            external_id=external_id or f"recurring-{uuid.uuid4().hex[:8]}",
            calendar=calendar,
            organization=org,
        )
        master.recurrence_rule = rule
        master.save()
        return master

    def test_calendar_events_by_calendar_id_prefetches_identifiers_no_n_plus_1(
        self, mock_rate_limiter, django_assert_num_queries
    ):
        """N one-off events on a calendar, each tagged with an identifier, selecting
        ``externalClientIdentifiers`` through the ``calendarId`` branch must not issue
        one extra query per event.

        Capable of failing: reverting either the ``optimize_queryset=...`` argument at
        the ``calendarId`` branch's call site (public_api/queries.py) or its
        application to ``non_recurring_events`` (calendar_event_service.py) makes this
        assert a strictly higher query count -- proven below by the "before" count.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        events = []
        for i in range(3):
            event = baker.make(
                CalendarEvent,
                organization=org,
                calendar=calendar,
                title=f"NPlus1 Event {i}",
                external_id=f"nplus1-{uuid.uuid4().hex[:8]}",
                timezone="UTC",
                start_time_tz_unaware=datetime.datetime(2026, 10, 1, 9 + i, 0),
                end_time_tz_unaware=datetime.datetime(2026, 10, 1, 9 + i, 30),
            )
            create_external_client_identifier(
                organization=org,
                identified_object=event,
                system="https://crm.example.com",
                identifier=f"deal-nplus1-{i}",
            )
            events.append(event)

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2026-10-01T00:00:00Z",
            "endDatetime": "2026-10-01T23:59:59Z",
            "system": None,
            "identifier": None,
        }

        # 9 queries regardless of N: auth/entitlement/rate-limit plumbing (5), the
        # calendar lookup (1), the non-recurring events queryset (1), the recurring
        # (empty) master queryset (1), and ONE prefetch query for
        # external_client_identifiers covering all 3 events (1). Pinned literal, not
        # a relative comparison -- with the fix reverted this is 11 (one extra query
        # per event instead of a single batched one). See the fixer's report for the
        # captured "before" query log.
        with django_assert_num_queries(9):
            response = self._post_json(
                _CALENDAR_EVENTS_BY_CALENDAR, system_user, token, auth_service, variables
            )

        data = assert_graphql_success(response)
        returned = data["calendarEvents"]
        assert len(returned) == 3
        for i, event in enumerate(events):
            matching = next(e for e in returned if int(e["id"]) == event.id)
            assert matching["externalClientIdentifiers"] == [
                {"system": "https://crm.example.com", "identifier": f"deal-nplus1-{i}"}
            ]

    def test_calendar_events_filter_by_identifier_returns_recurring_occurrences_via_calendar_id(
        self, mock_rate_limiter
    ):
        """A recurring series tagged with an identifier on its master row: filtering by
        that identifier through the ``calendarId`` branch must return the series'
        occurrences in the window, not a silent empty list.

        Capable of failing: before the recurrence-rule match was added, generated
        occurrences carried no reference to their master's id at all, so
        ``matching_event_ids`` filtering dropped every one of them.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        master = self._make_recurring_master(org, calendar)
        create_external_client_identifier(
            organization=org,
            identified_object=master,
            system="https://crm.example.com",
            identifier="series-calendar-id",
        )

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2026-10-01T00:00:00Z",
            "endDatetime": "2026-10-05T00:00:00Z",
            "system": "https://crm.example.com",
            "identifier": "series-calendar-id",
        }

        response = self._post_json(
            _CALENDAR_EVENTS_BY_CALENDAR_NO_ID, system_user, token, auth_service, variables
        )
        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        assert len(events) >= 1, "Recurring series occurrences must not be silently dropped"
        for event in events:
            assert event["externalClientIdentifiers"] == [
                {"system": "https://crm.example.com", "identifier": "series-calendar-id"}
            ]

    def test_calendar_events_filter_by_identifier_returns_recurring_occurrences_via_user_id(
        self, mock_rate_limiter
    ):
        """Same as the ``calendarId`` case above, but through the ``userId`` branch."""
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        master = self._make_recurring_master(org, calendar)
        create_external_client_identifier(
            organization=org,
            identified_object=master,
            system="https://crm.example.com",
            identifier="series-user-id",
        )

        variables = {
            "userId": owner.id,
            "startDatetime": "2026-10-01T00:00:00Z",
            "endDatetime": "2026-10-05T00:00:00Z",
            "system": "https://crm.example.com",
            "identifier": "series-user-id",
        }

        response = self._post_json(
            _CALENDAR_EVENTS_BY_USER_NO_ID, system_user, token, auth_service, variables
        )
        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        assert len(events) >= 1, "Recurring series occurrences must not be silently dropped"
        for event in events:
            assert event["externalClientIdentifiers"] == [
                {"system": "https://crm.example.com", "identifier": "series-user-id"}
            ]

    def test_calendar_events_filter_by_calendar_id_and_identifier_narrows_results(
        self, mock_rate_limiter
    ):
        """The ``calendarId`` + identifier combination: a matching event on the
        calendar is returned, a non-matching event on the same calendar is not."""
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        matching_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Matching",
            external_id=f"calid-match-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 30),
        )
        create_external_client_identifier(
            organization=org,
            identified_object=matching_event,
            system="https://crm.example.com",
            identifier="deal-narrow-calendar",
        )
        other_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Other",
            external_id=f"calid-other-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2026, 10, 1, 10, 0),
            end_time_tz_unaware=datetime.datetime(2026, 10, 1, 10, 30),
        )

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2026-10-01T00:00:00Z",
            "endDatetime": "2026-10-01T23:59:59Z",
            "system": "https://crm.example.com",
            "identifier": "deal-narrow-calendar",
        }

        response = self._post_json(
            _CALENDAR_EVENTS_BY_CALENDAR, system_user, token, auth_service, variables
        )
        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        returned_ids = {int(e["id"]) for e in events}
        assert returned_ids == {matching_event.id}
        assert other_event.id not in returned_ids

    def test_calendar_events_filter_by_user_id_and_identifier_narrows_results(
        self, mock_rate_limiter
    ):
        """The ``userId`` + identifier combination: a matching event owned by the
        user is returned, a non-matching event owned by the same user is not."""
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        owner, _membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        matching_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Matching",
            external_id=f"userid-match-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 30),
        )
        create_external_client_identifier(
            organization=org,
            identified_object=matching_event,
            system="https://crm.example.com",
            identifier="deal-narrow-user",
        )
        other_event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Other",
            external_id=f"userid-other-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2026, 10, 1, 10, 0),
            end_time_tz_unaware=datetime.datetime(2026, 10, 1, 10, 30),
        )

        variables = {
            "userId": owner.id,
            "startDatetime": "2026-10-01T00:00:00Z",
            "endDatetime": "2026-10-01T23:59:59Z",
            "system": "https://crm.example.com",
            "identifier": "deal-narrow-user",
        }

        response = self._post_json(
            _CALENDAR_EVENTS_BY_USER, system_user, token, auth_service, variables
        )
        data = assert_graphql_success(response)
        events = data["calendarEvents"]
        returned_ids = {int(e["id"]) for e in events}
        assert returned_ids == {matching_event.id}
        assert other_event.id not in returned_ids
