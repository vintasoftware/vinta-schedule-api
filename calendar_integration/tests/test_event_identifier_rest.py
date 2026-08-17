"""Integration tests for external client identifiers on the internal REST API.

Covers Phase 5 of the External Client Identifiers plan (``CalendarEventViewSet`` /
``CalendarEventSerializer``):

- ``POST`` with identifiers persists them on the created event.
- ``PUT`` replaces the stored set.
- ``PATCH`` omitting ``external_client_identifiers`` leaves the stored set untouched
  (the tri-state ``None`` no-op) -- proven against a seeded non-empty stored set, so a
  broken implementation that treats an omitted key as "clear" would fail this test.
- ``PATCH`` with ``external_client_identifiers: []`` clears the stored set.
- The attendee-level counterpart of the above: identifiers nested under
  ``external_attendances[].external_attendee.external_client_identifiers`` persist,
  and independently survive/clear on the same ``PATCH`` tri-state, scoped to the
  attendee and never bleeding into the event's own identifiers.
- The list endpoint filters by ``(system, identifier)``, normalizes ``system`` before
  matching, and rejects supplying only one of the pair with a 400.
- Listing events with identifiers -- at the event level and the attendee level --
  issues a constant number of queries regardless of event count (the virtual-model
  prefetch, not N+1 per event/attendee).
- A parametrized payload covering every ``ExternalClientIdentifierError`` subclass
  reachable through this endpoint's request body (duplicate system, blank identifier,
  invalid system, over-length identifier) is rejected with a 400 and writes no event
  -- the surfacing mechanism (``CalendarEventViewSet.perform_create``/
  ``perform_update`` catching ``CalendarIntegrationError``) is pre-existing and
  applies unchanged by this phase.
"""

import datetime
import uuid
from unittest.mock import Mock, patch

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    CalendarOwnership,
    ExternalAttendee,
    ExternalClientIdentifier,
)
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


@pytest.fixture
def mock_google_adapter():
    """Patches the low-level provider adapter only -- ``CalendarService`` and
    ``CalendarEventService`` (including the real ``ExternalClientIdentifierService``
    wired through DI) run unmocked, so identifier persistence is genuinely exercised.
    """
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


def _adapter_output(external_id: str) -> CalendarEventAdapterOutputData:
    start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    return CalendarEventAdapterOutputData(
        calendar_external_id="irrelevant",
        external_id=external_id,
        title="irrelevant",
        description="",
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
    )


def _organization() -> Organization:
    return baker.make(Organization, parent=None, can_invite_organizations=False)


def _google_backed_owner(organization: Organization, calendar: Calendar) -> User:
    """A user with a real Google ``SocialAccount``/``SocialToken``, an active
    membership, calendar ownership and a calendar-level ``CalendarManagementToken`` --
    the minimum a real (non-DI-mocked) user-driven REST create needs, matching
    ``test_event_creation_surfaces.py``'s ``_google_backed_owner``.
    """
    unique = uuid.uuid4().hex[:8]
    user = User.objects.create_user(email=f"rest-ident-{unique}@example.com", password="x")
    Profile.objects.create(user=user)
    OrganizationMembership.objects.create(user=user, organization=organization, is_active=True)
    social_account = SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid=f"uid-{unique}"
    )
    SocialToken.objects.create(
        account=social_account,
        token="access-token",
        token_secret="refresh-token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=user.id, organization=organization
    )
    token = CalendarManagementToken.objects.create(
        calendar_fk=calendar, membership_user_id=user.id, organization=organization
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=organization
        )
    return user


def _setup(mock_google_adapter) -> tuple[Organization, Calendar, User, APIClient]:
    organization = _organization()
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        external_id=f"cal-{uuid.uuid4().hex[:8]}",
    )
    user = _google_backed_owner(organization, calendar)
    client = APIClient()
    client.force_authenticate(user=user)
    return organization, calendar, user, client


def _external_attendance_payload(
    *,
    email: str,
    name: str = "External Attendee",
    attendee_id: int | None = None,
    identifiers: list[dict[str, str]] | None = None,
) -> dict:
    """One ``external_attendances`` list item: an ``EventExternalAttendanceSerializer``
    write payload wrapping an ``ExternalAttendeeSerializer``. ``attendee_id`` supplied
    matches an existing ``ExternalAttendee`` (by its own pk, not the join-table
    ``EventExternalAttendance`` id) so an update targets it instead of creating a new
    row. ``identifiers`` omitted (``None``) leaves the attendee's stored identifiers
    untouched; an explicit list -- including ``[]`` -- replaces them.
    """
    attendee: dict = {"email": email, "name": name}
    if attendee_id is not None:
        attendee["id"] = attendee_id
    if identifiers is not None:
        attendee["external_client_identifiers"] = identifiers
    return {"external_attendee": attendee}


def _payload(
    calendar: Calendar,
    *,
    title: str = "REST Identifier Event",
    identifiers: list[dict[str, str]] | None = None,
    external_attendances: list[dict] | None = None,
    start: datetime.datetime | None = None,
) -> dict:
    start = start or (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
    data: dict = {
        "calendar": calendar.id,
        "title": title,
        "description": "",
        "start_time": start.isoformat(),
        "end_time": (start + datetime.timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "resource_allocations": [],
        "attendances": [],
        "external_attendances": external_attendances if external_attendances is not None else [],
    }
    if identifiers is not None:
        data["external_client_identifiers"] = identifiers
    return data


def _create_event_via_rest(
    client: APIClient,
    calendar: Calendar,
    mock_google_adapter,
    *,
    identifiers: list[dict[str, str]] | None = None,
    external_attendances: list[dict] | None = None,
    title: str = "REST Identifier Event",
):
    mock_google_adapter.create_event.return_value = _adapter_output(f"ext-{uuid.uuid4().hex[:8]}")
    response = client.post(
        reverse("api:CalendarEvents-list"),
        _payload(
            calendar,
            title=title,
            identifiers=identifiers,
            external_attendances=external_attendances,
        ),
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    return response


def _grant_event_token(
    organization: Organization, user: User, event_id: int, mock_google_adapter
) -> CalendarEvent:
    """Mints the event-level ``CalendarManagementToken`` ``update_event`` requires
    (distinct from the calendar-level token used for create), and primes the mocked
    provider adapter's ``update_event`` return value -- required for every PUT/PATCH
    below since the target calendar is ``PERSONAL``/Google-provider so the real
    ``update_event`` code path always resolves and calls a write adapter.
    """
    event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
    mock_google_adapter.update_event.return_value = _adapter_output(event.external_id)
    token = CalendarManagementToken.objects.create(
        event_fk=event, membership_user_id=user.id, organization=organization
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=organization
        )
    return event


def _stored_identifiers(organization: Organization, event_id: int) -> list[tuple[str, str]]:
    content_type = ContentType.objects.get_for_model(CalendarEvent)
    return sorted(
        ExternalClientIdentifier.objects.filter_by_organization(organization.id)
        .filter(content_type=content_type, identified_key=event_id)
        .values_list("system", "identifier")
    )


def _stored_attendee_identifiers(
    organization: Organization, attendee_id: int
) -> list[tuple[str, str]]:
    content_type = ContentType.objects.get_for_model(ExternalAttendee)
    return sorted(
        ExternalClientIdentifier.objects.filter_by_organization(organization.id)
        .filter(content_type=content_type, identified_key=attendee_id)
        .values_list("system", "identifier")
    )


@pytest.mark.django_db
class TestCalendarEventIdentifiersRest:
    def test_post_with_identifiers_persists_them(self, mock_google_adapter):
        organization, calendar, _user, client = _setup(mock_google_adapter)

        response = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-1"}],
        )

        assert response.data["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-1"}
        ]
        assert _stored_identifiers(organization, response.data["id"]) == [
            ("https://crm.example.com", "deal-1")
        ]

    def test_post_with_external_attendee_identifiers_persists_them(self, mock_google_adapter):
        """The attendee-level counterpart of ``test_post_with_identifiers_persists_them``
        -- identifiers nested under ``external_attendances[].external_attendee``
        persist on the ``ExternalAttendee``, not the event."""
        organization, calendar, _user, client = _setup(mock_google_adapter)

        response = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            external_attendances=[
                _external_attendance_payload(
                    email="attendee@example.com",
                    identifiers=[{"system": "https://crm.example.com", "identifier": "contact-1"}],
                )
            ],
        )

        attendee = response.data["external_attendances"][0]["external_attendee"]
        assert attendee["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-1"}
        ]
        assert _stored_attendee_identifiers(organization, attendee["id"]) == [
            ("https://crm.example.com", "contact-1")
        ]
        # Not written to the event itself.
        assert _stored_identifiers(organization, response.data["id"]) == []

    def test_post_without_identifiers_writes_none(self, mock_google_adapter):
        """No-op regression: a caller that never mentions the field writes nothing --
        current (pre-feature) behavior is unchanged."""
        organization, calendar, _user, client = _setup(mock_google_adapter)

        response = _create_event_via_rest(client, calendar, mock_google_adapter)

        assert response.data["external_client_identifiers"] == []
        assert _stored_identifiers(organization, response.data["id"]) == []

    def test_put_replaces_the_set(self, mock_google_adapter):
        organization, calendar, user, client = _setup(mock_google_adapter)
        created = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-old"}],
        )
        event_id = created.data["id"]
        _grant_event_token(organization, user, event_id, mock_google_adapter)

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": event_id})
        response = client.put(
            url,
            _payload(
                calendar,
                title="Replaced via PUT",
                identifiers=[{"system": "https://crm.example.com", "identifier": "deal-new"}],
            ),
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-new"}
        ]
        assert _stored_identifiers(organization, event_id) == [
            ("https://crm.example.com", "deal-new")
        ]

    def test_patch_omitting_key_leaves_identifiers_untouched(self, mock_google_adapter):
        """The heart of this phase: a seeded non-empty set must survive a PATCH that
        never mentions ``external_client_identifiers``. Verified red against a broken
        implementation before landing (see phase report)."""
        organization, calendar, user, client = _setup(mock_google_adapter)
        created = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-keep"}],
        )
        event_id = created.data["id"]
        _grant_event_token(organization, user, event_id, mock_google_adapter)
        assert _stored_identifiers(organization, event_id) == [
            ("https://crm.example.com", "deal-keep")
        ]

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": event_id})
        response = client.patch(url, {"title": "Renamed via PATCH"}, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["title"] == "Renamed via PATCH"
        assert response.data["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-keep"}
        ]
        assert _stored_identifiers(organization, event_id) == [
            ("https://crm.example.com", "deal-keep")
        ]

    def test_patch_with_empty_list_clears_identifiers(self, mock_google_adapter):
        """The other half of the tri-state: an explicit ``[]`` clears a seeded
        non-empty set. Verified red against a broken implementation before landing."""
        organization, calendar, user, client = _setup(mock_google_adapter)
        created = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-clear"}],
        )
        event_id = created.data["id"]
        _grant_event_token(organization, user, event_id, mock_google_adapter)
        assert _stored_identifiers(organization, event_id) == [
            ("https://crm.example.com", "deal-clear")
        ]

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": event_id})
        response = client.patch(url, {"external_client_identifiers": []}, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["external_client_identifiers"] == []
        assert _stored_identifiers(organization, event_id) == []

    def test_patch_attendee_omitting_identifiers_key_leaves_them_untouched(
        self, mock_google_adapter
    ):
        """The attendee-level counterpart of
        ``test_patch_omitting_key_leaves_identifiers_untouched``: a seeded non-empty
        set on an ``ExternalAttendee`` must survive a PATCH that maintains the
        attendee (by id, inside ``external_attendances``) but never mentions its
        ``external_client_identifiers`` key. Verified red against a broken
        implementation before landing (see phase report)."""
        organization, calendar, user, client = _setup(mock_google_adapter)
        created = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            external_attendances=[
                _external_attendance_payload(
                    email="attendee@example.com",
                    identifiers=[
                        {"system": "https://crm.example.com", "identifier": "contact-keep"}
                    ],
                )
            ],
        )
        event_id = created.data["id"]
        attendee_id = created.data["external_attendances"][0]["external_attendee"]["id"]
        _grant_event_token(organization, user, event_id, mock_google_adapter)
        assert _stored_attendee_identifiers(organization, attendee_id) == [
            ("https://crm.example.com", "contact-keep")
        ]

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": event_id})
        response = client.patch(
            url,
            {
                "external_attendances": [
                    _external_attendance_payload(
                        email="attendee@example.com", attendee_id=attendee_id
                    )
                ]
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        attendee = response.data["external_attendances"][0]["external_attendee"]
        assert attendee["id"] == attendee_id
        assert attendee["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "contact-keep"}
        ]
        assert _stored_attendee_identifiers(organization, attendee_id) == [
            ("https://crm.example.com", "contact-keep")
        ]

    def test_patch_attendee_with_empty_list_clears_identifiers_leaves_event_untouched(
        self, mock_google_adapter
    ):
        """The attendee-level counterpart of ``test_patch_with_empty_list_clears_identifiers``:
        an explicit ``[]`` on the attendee clears only that attendee's stored
        identifiers, leaving the event's own identifiers alone. Verified red against
        a broken implementation before landing (see phase report)."""
        organization, calendar, user, client = _setup(mock_google_adapter)
        created = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "event-keep"}],
            external_attendances=[
                _external_attendance_payload(
                    email="attendee@example.com",
                    identifiers=[
                        {"system": "https://crm.example.com", "identifier": "contact-clear"}
                    ],
                )
            ],
        )
        event_id = created.data["id"]
        attendee_id = created.data["external_attendances"][0]["external_attendee"]["id"]
        _grant_event_token(organization, user, event_id, mock_google_adapter)
        assert _stored_attendee_identifiers(organization, attendee_id) == [
            ("https://crm.example.com", "contact-clear")
        ]

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": event_id})
        response = client.patch(
            url,
            {
                "external_attendances": [
                    _external_attendance_payload(
                        email="attendee@example.com",
                        attendee_id=attendee_id,
                        identifiers=[],
                    )
                ]
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        attendee = response.data["external_attendances"][0]["external_attendee"]
        assert attendee["id"] == attendee_id
        assert attendee["external_client_identifiers"] == []
        assert _stored_attendee_identifiers(organization, attendee_id) == []
        # The event's own identifiers are untouched by the attendee-level clear.
        assert response.data["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "event-keep"}
        ]
        assert _stored_identifiers(organization, event_id) == [
            ("https://crm.example.com", "event-keep")
        ]

    def test_list_filters_by_identifier_pair_and_normalizes_system(self, mock_google_adapter):
        _organization, calendar, _user, client = _setup(mock_google_adapter)
        target = _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-match"}],
            title="Target Event",
        )
        _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-other"}],
            title="Other Event",
        )

        url = reverse("api:CalendarEvents-list")
        # Un-normalized (uppercase host + trailing slash) on purpose: proves `system`
        # is normalized before matching.
        response = client.get(
            url,
            {
                "external_client_identifier_system": "https://CRM.example.com/",
                "external_client_identifier_identifier": "deal-match",
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        ids = {row["id"] for row in response.data["results"]}
        assert ids == {target.data["id"]}

    def test_list_requires_both_filter_arguments_together(self, mock_google_adapter):
        _organization, calendar, _user, client = _setup(mock_google_adapter)
        _create_event_via_rest(
            client,
            calendar,
            mock_google_adapter,
            identifiers=[{"system": "https://crm.example.com", "identifier": "deal-1"}],
        )

        url = reverse("api:CalendarEvents-list")
        only_system = client.get(
            url, {"external_client_identifier_system": "https://crm.example.com"}
        )
        assert only_system.status_code == status.HTTP_400_BAD_REQUEST, only_system.data

        only_identifier = client.get(url, {"external_client_identifier_identifier": "deal-1"})
        assert only_identifier.status_code == status.HTTP_400_BAD_REQUEST, only_identifier.data

    def test_list_issues_constant_number_of_queries_regardless_of_event_count(
        self, mock_google_adapter
    ):
        """N=3 events with identifiers, then two more (N=5): the query count must not
        grow -- the virtual-model prefetch on ``external_client_identifiers``, not a
        per-event ``.all()`` call in ``to_representation``. Capable of failing: drop
        the ``external_client_identifiers`` field from ``CalendarEventVirtualModel``
        and the request fails outright with
        ``django_virtual_models.prefetch.exceptions.MissingVirtualModelFieldException``
        (django-virtual-models refuses to serve a nested serializer field with no
        matching virtual-model field, rather than silently falling back to N+1) --
        observed directly, not merely asserted. If a future change makes the
        prefetch declaration a no-op instead of removing it outright, that failure
        mode would show up as this test's query-count assertion going red instead.
        """
        _organization, calendar, _user, client = _setup(mock_google_adapter)
        for i in range(3):
            _create_event_via_rest(
                client,
                calendar,
                mock_google_adapter,
                identifiers=[{"system": "https://crm.example.com", "identifier": f"deal-{i}"}],
                title=f"Query Count Event {i}",
            )

        url = reverse("api:CalendarEvents-list")
        with CaptureQueriesContext(connection) as captured_three:
            response_three = client.get(url)
        assert response_three.status_code == status.HTTP_200_OK

        for i in range(3, 5):
            _create_event_via_rest(
                client,
                calendar,
                mock_google_adapter,
                identifiers=[{"system": "https://crm.example.com", "identifier": f"deal-{i}"}],
                title=f"Query Count Event {i}",
            )

        with CaptureQueriesContext(connection) as captured_five:
            response_five = client.get(url)
        assert response_five.status_code == status.HTTP_200_OK

        assert len(captured_three.captured_queries) == len(captured_five.captured_queries), (
            "Listing events with identifiers must issue a constant number of queries "
            f"regardless of event count: 3 events -> {len(captured_three.captured_queries)} "
            f"queries, 5 events -> {len(captured_five.captured_queries)} queries."
        )

    def test_list_issues_constant_number_of_queries_with_external_attendee_identifiers(
        self, mock_google_adapter
    ):
        """The realistic N+1 shape for the attendee-level nested prefetch
        (``ExternalAttendeeVirtualModel.external_client_identifiers``, wired through
        ``EventExternalAttendanceVirtualModel``): N=3 events, each with an external
        attendee carrying its own identifier, then two more (N=5) -- the query count
        must not grow. The sibling of
        ``test_list_issues_constant_number_of_queries_regardless_of_event_count``,
        which only covers event-level identifiers.
        """
        _organization, calendar, _user, client = _setup(mock_google_adapter)
        for i in range(3):
            _create_event_via_rest(
                client,
                calendar,
                mock_google_adapter,
                identifiers=[{"system": "https://crm.example.com", "identifier": f"deal-{i}"}],
                external_attendances=[
                    _external_attendance_payload(
                        email=f"attendee-{i}@example.com",
                        identifiers=[
                            {"system": "https://crm.example.com", "identifier": f"contact-{i}"}
                        ],
                    )
                ],
                title=f"Attendee Query Count Event {i}",
            )

        url = reverse("api:CalendarEvents-list")
        with CaptureQueriesContext(connection) as captured_three:
            response_three = client.get(url)
        assert response_three.status_code == status.HTTP_200_OK

        for i in range(3, 5):
            _create_event_via_rest(
                client,
                calendar,
                mock_google_adapter,
                identifiers=[{"system": "https://crm.example.com", "identifier": f"deal-{i}"}],
                external_attendances=[
                    _external_attendance_payload(
                        email=f"attendee-{i}@example.com",
                        identifiers=[
                            {"system": "https://crm.example.com", "identifier": f"contact-{i}"}
                        ],
                    )
                ],
                title=f"Attendee Query Count Event {i}",
            )

        with CaptureQueriesContext(connection) as captured_five:
            response_five = client.get(url)
        assert response_five.status_code == status.HTTP_200_OK

        assert len(captured_three.captured_queries) == len(captured_five.captured_queries), (
            "Listing events with external-attendee identifiers must issue a constant "
            f"number of queries regardless of event count: 3 events -> "
            f"{len(captured_three.captured_queries)} queries, 5 events -> "
            f"{len(captured_five.captured_queries)} queries."
        )

    @pytest.mark.parametrize(
        ("title", "identifiers"),
        [
            (
                "Duplicate Identifiers Event",
                [
                    {"system": "https://crm.example.com", "identifier": "deal-1"},
                    {"system": "https://CRM.example.com/", "identifier": "deal-2"},
                ],
            ),
            (
                "Blank Identifier Event",
                [{"system": "https://crm.example.com", "identifier": "   "}],
            ),
            (
                "Invalid System Event",
                [{"system": "not-a-url", "identifier": "deal-invalid-system"}],
            ),
            (
                "Too Long Identifier Event",
                [{"system": "https://crm.example.com", "identifier": "x" * 256}],
            ),
        ],
        ids=["duplicate_system", "blank_identifier", "invalid_system", "too_long_identifier"],
    )
    def test_invalid_identifier_payload_returns_400_with_no_partial_write(
        self, mock_google_adapter, title, identifiers
    ):
        """Every ``ExternalClientIdentifierError`` subclass reachable through this
        endpoint's request body surfaces as a 400 with no partial write, via the same,
        unmodified mechanism (``CalendarEventViewSet.perform_create`` catching
        ``CalendarIntegrationError``, pre-existing and unchanged by this phase):

        - two pairs that normalize to the same ``system``
          (``ExternalClientIdentifierDuplicateSystemError``)
        - a blank identifier (``ExternalClientIdentifierBlankIdentifierError``) --
          not special-cased to duplicates
        - a ``system`` that fails URL validation
          (``ExternalClientIdentifierInvalidSystemError``)
        - an identifier over 255 characters -- rejected by
          ``ExternalClientIdentifierSerializer``'s own ``max_length=255`` before it
          can even reach ``ExternalClientIdentifierTooLongError``; defense in depth,
          not a gap, since the field's limit matches
          ``ExternalClientIdentifierService.MAX_IDENTIFIER_LENGTH`` exactly

        ``ExternalClientIdentifierInvalidTargetError`` and
        ``ExternalClientIdentifierCrossOrganizationError`` are not covered here: this
        endpoint only ever targets ``CalendarEvent``/``ExternalAttendee`` (never a
        caller-supplied model) within the caller's own organization (never a
        caller-supplied organization), so neither is reachable through the request
        body.
        """
        organization, calendar, _user, client = _setup(mock_google_adapter)
        mock_google_adapter.create_event.return_value = _adapter_output(
            f"ext-{uuid.uuid4().hex[:8]}"
        )

        response = client.post(
            reverse("api:CalendarEvents-list"),
            _payload(calendar, title=title, identifiers=identifiers),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert (
            not CalendarEvent.objects.filter_by_organization(organization.id)
            .filter(title=title)
            .exists()
        )
        assert not (
            ExternalClientIdentifier.objects.filter_by_organization(organization.id)
            .filter(identifier__in=[item["identifier"] for item in identifiers])
            .exists()
        )
