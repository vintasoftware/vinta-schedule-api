"""Integration tests for the public GraphQL ``updateCalendarEvent`` mutation.

Covers Phase 6 of the External Client Identifiers plan. This mutation is the plan's
highest-risk phase (Tier 4 review override): it hands an external API token a
general-purpose event mutation, and the two failure modes to guard against are:

1. An owner-scope bypass -- a scoped token editing an event it should not be able to
   see.
2. An unintended attendee/identifier wipe from an omitted-versus-empty mix-up.

Both risks are addressed with tests designed to be falsifiable -- see each test's
docstring for how it was verified capable of failing before the fix (or would be,
against a broken implementation).
"""

import datetime
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    ExternalClientIdentifier,
)
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import Profile
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services.webhook_calendar_side_effects import WebhookCalendarEventSideEffectsService
from webhooks.services.webhook_service import WebhookService


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


_UPDATE_CALENDAR_EVENT = """
mutation UpdateCalendarEvent($input: UpdateCalendarEventInput!) {
    updateCalendarEvent(input: $input) {
        id
        title
        description
        attendances {
            membership {
                userId
            }
        }
        externalAttendees {
            email
            name
            externalClientIdentifiers {
                system
                identifier
            }
        }
        externalClientIdentifiers {
            system
            identifier
        }
    }
}
"""


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestUpdateCalendarEventMutation:
    """A single class so the org/owner/calendar/event scaffolding helpers can be
    shared across the write-side (title/description/attendees/identifiers), the
    owner-scope, and the permission tests."""

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Scaffolding helpers -- mirror TestExternalClientIdentifiersGraphQL /
    # TestScopedTokenRescheduleCalendarEvent in the neighboring test files.
    # ------------------------------------------------------------------

    def _make_org(self):
        return baker.make(Organization, name=f"UpdateCalEvt Org {uuid.uuid4().hex[:8]}")

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

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _make_event(self, org, calendar, **overrides):
        base = {
            "organization": org,
            "calendar": calendar,
            "title": "Original Title",
            "description": "Original description",
            "external_id": f"evt-{uuid.uuid4().hex[:10]}",
            "start_time_tz_unaware": datetime.datetime(2026, 10, 1, 10, 0, 0),
            "end_time_tz_unaware": datetime.datetime(2026, 10, 1, 11, 0, 0),
            "timezone": "UTC",
        }
        base.update(overrides)
        return baker.make(CalendarEvent, **base)

    def _add_internal_attendee(self, org, event):
        user_model = get_user_model()
        user = baker.make(user_model, email=f"attendee_{uuid.uuid4().hex[:8]}@example.com")
        # update_event's serialization (for the permission-check snapshot) calls
        # User.get_full_name(), which reads through the profile -- a Profile-less
        # user raises RelatedObjectDoesNotExist there.
        baker.make(Profile, user=user, first_name="Attendee", last_name="Person")
        baker.make(OrganizationMembership, user=user, organization=org, is_active=True)
        baker.make(
            EventAttendance,
            event=event,
            organization=org,
            membership_user_id=user.id,
        )
        return user

    def _add_external_attendee(self, org, event, email, name="Guest"):
        attendee = baker.make(ExternalAttendee, organization=org, email=email, name=name)
        baker.make(
            EventExternalAttendance,
            organization=org,
            event=event,
            external_attendee=attendee,
        )
        return attendee

    def _update_input(self, org, event, **overrides):
        base = {"organizationId": org.id, "eventId": event.id}
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # Title alone leaves everything else untouched (attendee/identifier wipe guard)
    # ------------------------------------------------------------------

    def test_update_title_alone_leaves_description_attendees_and_identifiers_untouched(
        self, mock_rate_limiter
    ):
        """Seeds NON-EMPTY attendees, external attendees and identifiers on both the
        event and an external attendee, then updates ONLY the title.

        Falsifiable by construction: an implementation that maps an omitted field to
        ``[]`` instead of leaving it untouched would wipe every one of these. Verified
        live: temporarily forcing the resolver's ``external_client_identifiers``
        branch to always pass ``_map_external_client_identifiers([])`` (ignoring
        ``strawberry.UNSET``) makes this test fail with the event's
        ``externalClientIdentifiers`` collapsing to ``[]``; restoring the UNSET check
        makes it pass again -- see the phase report for the observed red/green.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)

        internal_attendee = self._add_internal_attendee(org, event)
        external_attendee = self._add_external_attendee(
            org, event, "guest@example.com", name="Guest"
        )
        create_external_client_identifier(
            organization=org,
            identified_object=external_attendee,
            system="https://crm.example.com",
            identifier="contact-guest",
        )
        create_external_client_identifier(
            organization=org,
            identified_object=event,
            system="https://crm.example.com",
            identifier="deal-event",
        )

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {"input": self._update_input(org, event, title="New Title")},
        )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]

        assert result["title"] == "New Title"
        assert result["description"] == "Original description"

        attendee_user_ids = {a["membership"]["userId"] for a in result["attendances"]}
        assert attendee_user_ids == {internal_attendee.id}

        assert len(result["externalAttendees"]) == 1
        assert result["externalAttendees"][0]["email"] == "guest@example.com"
        assert result["externalAttendees"][0]["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-guest"}
        ]

        assert result["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-event"}
        ]

        # Persisted, not just echoed: the external attendee row itself was never
        # deleted/recreated (its identifier's identified_key still points at the
        # SAME external attendee pk).
        assert (
            ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(system="https://crm.example.com", identifier="contact-guest")
            .filter(identified_key=external_attendee.pk)
            .exists()
        )

    # ------------------------------------------------------------------
    # Identifier replace / clear
    # ------------------------------------------------------------------

    def test_update_replaces_event_identifiers(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        create_external_client_identifier(
            organization=org,
            identified_object=event,
            system="https://crm.example.com",
            identifier="deal-old",
        )
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {
                "input": self._update_input(
                    org,
                    event,
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "deal-new"}
                    ],
                )
            },
        )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]
        assert result["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-new"}
        ]
        assert not (
            ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(identified_key=event.pk, identifier="deal-old")
            .exists()
        )

    def test_update_with_empty_list_clears_event_identifiers(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        create_external_client_identifier(
            organization=org,
            identified_object=event,
            system="https://crm.example.com",
            identifier="deal-clear-me",
        )
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {"input": self._update_input(org, event, externalClientIdentifiers=[])},
        )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]
        assert result["externalClientIdentifiers"] == []
        assert not (
            ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(identified_key=event.pk)
            .exists()
        )

    # ------------------------------------------------------------------
    # External attendee replace: webhooks + surviving-attendee identifier preservation
    # ------------------------------------------------------------------

    def test_replacing_external_attendees_fires_webhooks_and_preserves_surviving_identifiers(
        self, mock_rate_limiter, django_capture_on_commit_callbacks
    ):
        """Replacing the external-attendee list: a surviving attendee (same email
        re-sent) keeps its identifiers untouched and is NOT deleted/recreated; a
        dropped attendee fires the attendee-removed webhook; a new attendee fires
        the attendee-added webhook.

        Calendar side-effect webhooks do not dispatch through the DI container on
        this branch (di_core/containers.py wires `side_effects_pipeline` as a plain
        tuple holding an unresolved provider -- see PR #278, not in this stack), so
        the pipeline is hand-wired here, mirroring
        webhooks/tests/test_calendar_event_identifier_payloads.py.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)

        surviving = self._add_external_attendee(
            org, event, "surviving@example.com", name="Surviving"
        )
        create_external_client_identifier(
            organization=org,
            identified_object=surviving,
            system="https://crm.example.com",
            identifier="contact-surviving",
        )
        removed = self._add_external_attendee(org, event, "removed@example.com", name="Removed")

        baker.make(
            WebhookConfiguration,
            organization=org,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED,
            url="https://example.com/hooks/added",
        )
        baker.make(
            WebhookConfiguration,
            organization=org,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_REMOVED,
            url="https://example.com/hooks/removed",
        )

        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        from di_core.containers import container

        side_effects_service = CalendarSideEffectsService(
            side_effects_pipeline=(
                WebhookCalendarEventSideEffectsService(webhook_service=WebhookService()),
            )
        )

        with container.calendar_side_effects_service.override(side_effects_service):
            with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
                with django_capture_on_commit_callbacks(execute=True):
                    response = self._post(
                        _UPDATE_CALENDAR_EVENT,
                        system_user,
                        token,
                        auth_service,
                        {
                            "input": self._update_input(
                                org,
                                event,
                                externalAttendees=[
                                    {"email": "surviving@example.com", "name": "Surviving"},
                                    {"email": "new@example.com", "name": "New"},
                                ],
                            )
                        },
                    )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]
        emails = {a["email"] for a in result["externalAttendees"]}
        assert emails == {"surviving@example.com", "new@example.com"}

        surviving_result = next(
            a for a in result["externalAttendees"] if a["email"] == "surviving@example.com"
        )
        assert surviving_result["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-surviving"}
        ]

        # The surviving row was updated in place, not deleted + recreated.
        assert (
            ExternalAttendee.objects.filter_by_organization(org.id).filter(id=surviving.id).exists()
        )
        # The dropped attendee (and, via cascade, anything it carried) is gone.
        assert (
            not ExternalAttendee.objects.filter_by_organization(org.id)
            .filter(id=removed.id)
            .exists()
        )

        added_webhook = WebhookEvent.objects.filter_by_organization(org.id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED
        )
        assert added_webhook.payload["email"] == "new@example.com"

        removed_webhook = WebhookEvent.objects.filter_by_organization(org.id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_REMOVED
        )
        assert removed_webhook.payload["email"] == "removed@example.com"

    # ------------------------------------------------------------------
    # BLOCKER regression: case/whitespace-insensitive email matching must NOT
    # wipe an existing external attendee (and its identifiers) just because the
    # caller re-sent the same email with different casing/whitespace.
    # ------------------------------------------------------------------

    def test_resending_attendee_email_with_different_case_preserves_pk_and_identifiers(
        self, mock_rate_limiter
    ):
        """Re-sending an existing external attendee's email with different case (and
        surrounding whitespace) must be treated as the SAME attendee, not a delete +
        recreate.

        Falsifiable: against the unfixed resolver (raw, case-sensitive, untrimmed
        dict key/lookup), "Guest@Example.com" stored vs " guest@example.com "
        supplied is a lookup miss -- the existing attendee row is deleted (cascading
        away its ExternalClientIdentifier) and a new attendee is created with zero
        identifiers. This test pins the opposite: the pk is unchanged and the
        identifier survives.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)

        attendee = self._add_external_attendee(org, event, "Guest@Example.com", name="Guest")
        create_external_client_identifier(
            organization=org,
            identified_object=attendee,
            system="https://crm.example.com",
            identifier="contact-1",
        )

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {
                "input": self._update_input(
                    org,
                    event,
                    externalAttendees=[{"email": " guest@example.com ", "name": "Guest"}],
                )
            },
        )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]

        assert len(result["externalAttendees"]) == 1
        assert result["externalAttendees"][0]["externalClientIdentifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-1"}
        ]

        # The pk is unchanged -- the row was updated in place, not deleted+recreated.
        assert (
            ExternalAttendee.objects.filter_by_organization(org.id).filter(id=attendee.id).exists()
        )
        assert (
            ExternalClientIdentifier.objects.filter_by_organization(org.id)
            .filter(system="https://crm.example.com", identifier="contact-1")
            .filter(identified_key=attendee.pk)
            .exists()
        )

    # ------------------------------------------------------------------
    # SHOULD-FIX #2: omitted `name` on a matched attendee falls back to the stored
    # name instead of blanking it.
    # ------------------------------------------------------------------

    def test_resending_attendee_with_empty_name_keeps_stored_name(self, mock_rate_limiter):
        """`ScheduleEventExternalAttendeeInput.name` defaults to "" (not UNSET). A
        caller supplying only `{email}` to mean "keep this attendee, touch nothing
        else" must not blank the stored name.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)

        self._add_external_attendee(org, event, "guest@example.com", name="Alice Guest")

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {
                "input": self._update_input(
                    org, event, externalAttendees=[{"email": "guest@example.com"}]
                )
            },
        )

        data = assert_graphql_success(response)
        result = data["updateCalendarEvent"]
        assert len(result["externalAttendees"]) == 1
        assert result["externalAttendees"][0]["name"] == "Alice Guest"

        assert (
            ExternalAttendee.objects.filter_by_organization(org.id)
            .filter(email="guest@example.com", name="Alice Guest")
            .exists()
        )

    # ------------------------------------------------------------------
    # SHOULD-FIX #3: duplicate normalized emails in one payload are rejected, not
    # silently collapsed.
    # ------------------------------------------------------------------

    def test_duplicate_normalized_emails_in_external_attendees_is_rejected(self, mock_rate_limiter):
        """Two entries in the same `externalAttendees` payload resolving to the same
        normalized email must be rejected outright, not silently collapsed onto one
        row (which would drop the second entry's name/identifiers).
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {
                "input": self._update_input(
                    org,
                    event,
                    externalAttendees=[
                        {"email": "a@x.com", "name": "A"},
                        {"email": " A@X.com ", "name": "B"},
                    ],
                )
            },
        )

        assert_graphql_error(response)
        assert (
            not EventExternalAttendance.objects.filter_by_organization(org.id)
            .filter(event_fk_id=event.id)
            .exists()
        )

    # ------------------------------------------------------------------
    # Owner scope: org-wide vs scoped, and the not-found-shaped cross-owner error
    # ------------------------------------------------------------------

    def test_org_wide_token_may_update_any_event(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, _membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {"input": self._update_input(org, event, title="Org Wide Update")},
        )

        data = assert_graphql_success(response)
        assert data["updateCalendarEvent"]["title"] == "Org Wide Update"

    def test_scoped_token_may_update_only_its_owners_events(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {"input": self._update_input(org, event, title="Own Event Update")},
        )

        data = assert_graphql_success(response)
        assert data["updateCalendarEvent"]["title"] == "Own Event Update"

    def test_scoped_token_targeting_another_owners_event_gets_not_found_shaped_error(
        self, mock_rate_limiter
    ):
        """A scoped token attempting to update another owner's event gets the exact
        same error message as a genuinely missing event -- existence must not leak.

        Proven capable of failing: the SAME event, targeted by an org-wide token (no
        owner-scope narrowing), succeeds and the title change is persisted -- so the
        event genuinely exists and is genuinely reachable when the guard is absent;
        only the owner-scope check hides/rejects it for the scoped token.

        Also verified live by temporarily replacing the resolver's
        ``assert_calendar_in_owner_scope`` call with a no-op: this test goes RED --
        the request is still blocked (the service's own independent ownership check,
        ``CalendarEventService._public_token_may_write``, is a second, defense-in-
        depth layer), but with a DIFFERENT message,
        ``"Calendar matching query does not exist."``, not ``"Event not found."`` --
        breaking the "identical to a missing event" invariant this test exists to
        pin. Restoring the resolver-level guard makes both paths agree on
        ``"Event not found."`` again. See the phase report for the observed
        before/after.
        """
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        event_b = self._make_event(org, calendar_b, title="Owner B's Event")

        scoped_system_user, scoped_token, scoped_auth_service = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        cross_owner_response = self._post(
            _UPDATE_CALENDAR_EVENT,
            scoped_system_user,
            scoped_token,
            scoped_auth_service,
            {"input": self._update_input(org, event_b, title="Should Not Apply From Scoped Token")},
        )
        cross_owner_errors = assert_graphql_error(cross_owner_response)
        cross_owner_message = cross_owner_errors[0]["message"]

        missing_event_response = self._post(
            _UPDATE_CALENDAR_EVENT,
            scoped_system_user,
            scoped_token,
            scoped_auth_service,
            {"input": {"organizationId": org.id, "eventId": 999999995}},
        )
        missing_event_errors = assert_graphql_error(missing_event_response)
        missing_event_message = missing_event_errors[0]["message"]

        assert cross_owner_message == missing_event_message == "Event not found."

        # Event B is genuinely unmodified.
        event_b.refresh_from_db()
        assert event_b.title == "Owner B's Event"

        # Prove the event is real and reachable: an org-wide token (no owner-scope
        # narrowing) CAN update it.
        org_wide_system_user, org_wide_token, org_wide_auth_service = (
            self._make_org_wide_system_user(org, [PublicAPIResources.CALENDAR_EVENT])
        )
        org_wide_response = self._post(
            _UPDATE_CALENDAR_EVENT,
            org_wide_system_user,
            org_wide_token,
            org_wide_auth_service,
            {"input": self._update_input(org, event_b, title="Updated By Org Wide Token")},
        )
        org_wide_data = assert_graphql_success(org_wide_response)
        assert org_wide_data["updateCalendarEvent"]["title"] == "Updated By Org Wide Token"

    # ------------------------------------------------------------------
    # Permission: missing calendar_event resource grant
    # ------------------------------------------------------------------

    def test_token_lacking_calendar_event_scope_is_rejected(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CREATE_BLOCKED_TIME]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {"input": self._update_input(org, event, title="Should Not Apply")},
        )

        errors = assert_graphql_error(response)
        assert "don't have access" in str(errors).lower()
        event.refresh_from_db()
        assert event.title == "Original Title"

    # ------------------------------------------------------------------
    # Duplicate identifier rolls back the whole update
    # ------------------------------------------------------------------

    def test_duplicate_identifier_rolls_back_title_and_attendees(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        org = self._make_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        event = self._make_event(org, calendar)
        internal_attendee = self._add_internal_attendee(org, event)

        other_event = self._make_event(org, calendar, title="Other Event")
        create_external_client_identifier(
            organization=org,
            identified_object=other_event,
            system="https://crm.example.com",
            identifier="deal-collision",
        )

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _UPDATE_CALENDAR_EVENT,
            system_user,
            token,
            auth_service,
            {
                "input": self._update_input(
                    org,
                    event,
                    title="Should Not Persist",
                    attendeeUserIds=[],
                    externalClientIdentifiers=[
                        {"system": "https://crm.example.com", "identifier": "deal-collision"}
                    ],
                )
            },
        )

        errors = assert_graphql_error(response)
        # The IntegrityError's raw message (constraint name, column tuple, internal
        # organization_id/content_type_id) must NOT reach an external API token --
        # only the fixed, friendly message.
        assert (
            errors[0]["message"]
            == "That (system, identifier) pair is already in use by another record."
        )

        event.refresh_from_db()
        assert event.title == "Original Title"
        assert (
            EventAttendance.objects.filter_by_organization(org.id)
            .filter(event_fk_id=event.id, membership_user_id=internal_attendee.id)
            .exists()
        )
