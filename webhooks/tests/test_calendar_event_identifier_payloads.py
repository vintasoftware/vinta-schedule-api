"""External client identifiers carried in outbound calendar-event webhook payloads.

Covers the full chain: `calendar_integration.services.calendar_service_utils.serialize_event`
and `serialize_event_data_input` populate `CalendarEventData.external_client_identifiers`, and
`WebhookCalendarEventSideEffectsService._serialize_event` renders it into the JSON payload as
`external_client_identifiers: list[dict[str, str]]` -- `[]` when the record has none, never a
missing key and never `null`.

Two tests carry the real risk in this phase and are called out individually:

- ``test_delete_event_webhook_payload_captures_identifiers_before_cascade`` -- the
  ``GenericRelation`` cascade removes identifier rows as part of deleting the event, so the
  webhook snapshot must be built (and *materialized*, not left as a lazy queryset) before the
  delete. Verified capable of failing: deferring the queryset evaluation in
  ``calendar_service_utils.serialize_event`` (storing the lazy ``QuerySet`` instead of a
  materialized list) makes this test fail with an empty ``external_client_identifiers`` list --
  see the phase report for the observed failure.
- ``test_serialize_event_for_n_events_issues_no_per_event_identifier_query`` -- proves
  ``serialize_event`` uses the prefetch cache (``.all()`` on an already-prefetched
  ``GenericRelation``) rather than a fresh per-event query. Verified capable of failing: dropping
  ``external_client_identifiers`` from the ``prefetch_related`` call in this test makes the query
  count assertion fail -- see the phase report for the observed before/after counts.
"""

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    RecurrenceRule,
)
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_utils import (
    serialize_event,
    serialize_event_data_input,
)
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventData,
    CalendarEventInputData,
    EventExternalAttendanceInputData,
    EventExternalAttendeeData,
    ExternalAttendeeInputData,
    ExternalClientIdentifierData,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services.webhook_calendar_side_effects import WebhookCalendarEventSideEffectsService
from webhooks.services.webhook_service import WebhookService


# ---------------------------------------------------------------------------
# calendar_service_utils.serialize_event / serialize_event_data_input
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSerializeEventPopulatesIdentifiers:
    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="Serialize Event Org")

    @pytest.fixture
    def calendar(self, organization):
        return baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="serialize-event-cal",
        )

    def _make_event(self, calendar, organization, **overrides):
        defaults = dict(
            organization=organization,
            calendar=calendar,
            title="Serialize Event Test",
            external_id="serialize-event-evt",
            start_time_tz_unaware=datetime.datetime(2025, 6, 1, 9, 0, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 1, 10, 0, 0),
            timezone="UTC",
        )
        defaults.update(overrides)
        return baker.make(CalendarEvent, **defaults)

    def test_serialize_event_includes_identifiers(self, calendar, organization):
        event = self._make_event(calendar, organization)
        create_external_client_identifier(
            organization=organization,
            identified_object=event,
            system="https://crm.example.com",
            identifier="deal-1",
        )
        create_external_client_identifier(
            organization=organization,
            identified_object=event,
            system="https://tickets.example.com",
            identifier="ticket-2",
        )

        result = serialize_event(event)

        assert sorted(
            (i.system, i.identifier) for i in result.external_client_identifiers
        ) == sorted(
            [
                ("https://crm.example.com", "deal-1"),
                ("https://tickets.example.com", "ticket-2"),
            ]
        )

    def test_serialize_event_with_no_identifiers_returns_empty_list(self, calendar, organization):
        event = self._make_event(calendar, organization, external_id="serialize-event-noident")

        result = serialize_event(event)

        assert result.external_client_identifiers == []

    def test_serialize_event_for_modified_occurrence_falls_back_to_master_identifiers(
        self, calendar, organization
    ):
        """A persisted modified-occurrence exception has no identifier rows of its
        own -- identifiers live on the recurring master. ``serialize_event`` must
        fall back to ``event.parent_recurring_object.external_client_identifiers``
        for such a row, the same way it already falls back for attendees/external
        attendees/resources."""
        rule = baker.make(
            RecurrenceRule,
            organization=organization,
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
        )
        master = self._make_event(
            calendar,
            organization,
            title="Recurring master",
            external_id="serialize-event-master",
            recurrence_rule=rule,
        )
        create_external_client_identifier(
            organization=organization,
            identified_object=master,
            system="https://crm.example.com",
            identifier="deal-master",
        )
        modified_occurrence = self._make_event(
            calendar,
            organization,
            title="Recurring master (modified)",
            external_id="serialize-event-occurrence",
            start_time_tz_unaware=datetime.datetime(2025, 6, 2, 9, 0, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 2, 10, 0, 0),
            parent_recurring_object=master,
            is_recurring_exception=True,
        )
        master.create_exception(
            exception_date=datetime.datetime(2025, 6, 2, 9, 0, tzinfo=datetime.UTC),
            is_cancelled=False,
            modified_object=modified_occurrence,
        )

        result = serialize_event(modified_occurrence)

        assert [(i.system, i.identifier) for i in result.external_client_identifiers] == [
            ("https://crm.example.com", "deal-master")
        ]

    def test_serialize_event_data_input_includes_identifiers_when_provided(
        self, calendar, organization
    ):
        event = self._make_event(calendar, organization, external_id="serialize-input-evt")
        event_data = CalendarEventInputData(
            title="Renamed",
            description="",
            start_time=datetime.datetime(2025, 6, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 1, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_client_identifiers=[
                ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-3")
            ],
        )

        result = serialize_event_data_input(event, event_data, organization)

        assert result.external_client_identifiers == [
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-3")
        ]

    def test_serialize_event_data_input_returns_empty_list_when_omitted(
        self, calendar, organization
    ):
        """``None`` (omitted -- untouched) must not surface as ``None`` on the
        dataclass; the field is a plain list, so it maps to ``[]``."""
        event = self._make_event(calendar, organization, external_id="serialize-input-omit")
        event_data = CalendarEventInputData(
            title="Renamed",
            description="",
            start_time=datetime.datetime(2025, 6, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 1, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        result = serialize_event_data_input(event, event_data, organization)

        assert result.external_client_identifiers == []


# ---------------------------------------------------------------------------
# WebhookCalendarEventSideEffectsService._serialize_event -- payload shape
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWebhookCalendarEventSideEffectsServiceIdentifierPayload:
    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="Webhook Payload Org")

    @pytest.fixture
    def mock_webhook_service(self):
        mock = MagicMock()
        mock.send_event.return_value = []
        return mock

    @pytest.fixture
    def handler(self, mock_webhook_service):
        return WebhookCalendarEventSideEffectsService(webhook_service=mock_webhook_service)

    def _event_data(self, **overrides: Any) -> CalendarEventData:
        defaults: dict[str, Any] = dict(
            id=1,
            calendar_id=2,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            title="Intro call",
            description="",
            external_id="ext-1",
            calendar_settings=None,
            status="confirmed",
            attendees=[],
            external_attendees=[],
            resources=[],
            recurrence_rule=None,
            is_recurring=False,
            recurring_event_id=None,
        )
        defaults.update(overrides)
        return CalendarEventData(**defaults)

    @pytest.mark.parametrize(
        "method_name,event_type",
        [
            ("on_create_event", WebhookEventType.CALENDAR_EVENT_CREATED),
            ("on_update_event", WebhookEventType.CALENDAR_EVENT_UPDATED),
            ("on_delete_event", WebhookEventType.CALENDAR_EVENT_DELETED),
        ],
    )
    def test_payload_carries_two_identifiers(
        self, handler, mock_webhook_service, organization, method_name, event_type
    ):
        event_data = self._event_data(
            external_client_identifiers=[
                ExternalClientIdentifierData(
                    system="https://crm.example.com", identifier="deal-9182"
                ),
                ExternalClientIdentifierData(
                    system="https://tickets.example.com", identifier="ticket-42"
                ),
            ]
        )

        getattr(handler, method_name)(actor=None, event=event_data, organization=organization)

        payload = mock_webhook_service.send_event.call_args[1]["payload"]
        assert payload["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-9182"},
            {"system": "https://tickets.example.com", "identifier": "ticket-42"},
        ]

    @pytest.mark.parametrize(
        "method_name",
        ["on_create_event", "on_update_event", "on_delete_event"],
    )
    def test_event_with_no_identifiers_emits_empty_list(
        self, handler, mock_webhook_service, organization, method_name
    ):
        """An event with no identifiers must emit ``[]`` -- never a missing key,
        never ``null``."""
        event_data = self._event_data(external_client_identifiers=[])

        getattr(handler, method_name)(actor=None, event=event_data, organization=organization)

        payload = mock_webhook_service.send_event.call_args[1]["payload"]
        assert "external_client_identifiers" in payload
        assert payload["external_client_identifiers"] == []

    def test_attendee_webhook_embeds_event_identifiers(
        self, handler, mock_webhook_service, organization
    ):
        """Attendee webhooks embed the event object, so they carry the event's
        identifiers -- but gain no top-level identifier field of their own (the
        attendee's own identifiers are an explicit non-goal of this plan)."""
        event_data = self._event_data(
            external_client_identifiers=[
                ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1")
            ]
        )
        attendance = EventExternalAttendeeData(
            email="attendee@example.com", name="Attendee", status="accepted"
        )

        handler.on_add_attendee_to_event(
            actor=None, event=event_data, attendance=attendance, organization=organization
        )

        payload = mock_webhook_service.send_event.call_args[1]["payload"]
        assert payload["event"]["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-1"}
        ]
        # Non-goal: `EventAttendeeWebhookPayload` gains no top-level identifier
        # field of its own -- only the embedded event carries one.
        assert set(payload.keys()) == {"email", "name", "status", "user_id", "event"}


# ---------------------------------------------------------------------------
# Integration: create_event / update_event / delete_event through the real
# service stack, including the delete-snapshot ordering trap.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_google_adapter():
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = MagicMock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Webhook Identifier Event Org", should_sync_rooms=False)


@pytest.fixture
def social_account(db):
    user = User.objects.create_user(
        email="webhook-identifier-event@example.com", password="testpass123"
    )
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="88888")


@pytest.fixture
def social_token(social_account):
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def calendar(db, organization):
    return Calendar.objects.create(
        name="Webhook Identifier Event Calendar",
        description="A test calendar",
        external_id="webhook_evt_ident_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_management_token(db, calendar, social_account):
    OrganizationMembership.objects.get_or_create(
        user=social_account.user, organization=calendar.organization
    )
    token = CalendarManagementToken.objects.create(
        calendar=calendar,
        membership_user_id=social_account.user.id,
        token_hash="webhook_evt_ident_token_hash",
        organization=calendar.organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=calendar.organization_id,
        )
    return token


@pytest.fixture
def authenticated_facade(social_account, social_token, mock_google_adapter, calendar):
    service = CalendarService()
    # The DI container's `calendar_side_effects_service` provider passes
    # `side_effects_pipeline=(webhook_calendar_side_effects_service,)` -- a plain
    # tuple literal, not a `providers.List(...)`. `dependency_injector` only
    # auto-resolves a provider that is a *direct* kwarg value, not one nested
    # inside a tuple, so the pipeline the container builds holds the unresolved
    # `Factory` provider itself. `isinstance(provider, OnCreateEventHandler)` is
    # then always `False`, and no calendar-event webhook (of any kind, not just
    # this phase's identifiers) ever dispatches through the DI-wired path.
    # Pre-existing, cross-cutting, and out of scope for this phase -- see the
    # phase report. Every other test file that exercises calendar event side
    # effects works around it the same way `test_calendar_side_effects.py` and
    # `test_calendar_service.py` do: wire the pipeline explicitly rather than
    # rely on the container. Doing that here (instead of the mocked pipeline
    # those files use) is what makes this file's integration tests exercise a
    # *real* `WebhookService.send_event` -> persisted `WebhookEvent` round trip.
    service.calendar_side_effects_service = CalendarSideEffectsService(
        side_effects_pipeline=(
            WebhookCalendarEventSideEffectsService(webhook_service=WebhookService()),
        )
    )
    service.authenticate(account=social_account.user, organization=calendar.organization)
    return service


@pytest.fixture
def event_service(authenticated_facade):
    return CalendarEventService(
        context=authenticated_facade._context,
        recurrence_manager=authenticated_facade._recurrence_manager,
        calendar_cache=authenticated_facade._calendar_cache,
        host=authenticated_facade,
    )


def _grant_event_owner_token(event, user, organization):
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"webhook_evt_ident_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=organization.id,
        )
    return token


def _adapter_output(external_id: str) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id="webhook_evt_ident_cal_1",
        external_id=external_id,
        title="Webhook Identifier Event",
        description="An event with identifiers",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )


def _base_event_input(**overrides: Any) -> CalendarEventInputData:
    defaults: dict[str, Any] = dict(
        title="Webhook Identifier Event",
        description="An event with identifiers",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )
    defaults.update(overrides)
    return CalendarEventInputData(**defaults)


@pytest.mark.django_db
class TestCalendarEventWebhookDeliveryCarriesIdentifiers:
    """Drives the real ``CalendarEventService`` + a real ``WebhookService`` (not
    mocked) so the persisted ``WebhookEvent.payload`` is exactly what a partner
    would receive. ``process_webhook_event.delay`` is patched so no outbound HTTP
    call is attempted -- only the enqueue-time payload matters here.
    """

    def test_create_event_webhook_carries_identifiers(
        self,
        event_service,
        mock_google_adapter,
        calendar,
        calendar_management_token,
        django_capture_on_commit_callbacks,
    ):
        baker.make(
            WebhookConfiguration,
            organization=calendar.organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/hooks/created",
        )
        mock_google_adapter.create_event.return_value = _adapter_output("evt-webhook-create")

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                event_service.create_event(
                    calendar.id,
                    _base_event_input(
                        external_client_identifiers=[
                            ExternalClientIdentifierData(
                                system="https://crm.example.com", identifier="deal-create"
                            ),
                            ExternalClientIdentifierData(
                                system="https://tickets.example.com", identifier="ticket-create"
                            ),
                        ]
                    ),
                )

        webhook_event = WebhookEvent.objects.filter_by_organization(calendar.organization_id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED
        )
        assert sorted(
            (i["system"], i["identifier"])
            for i in webhook_event.payload["external_client_identifiers"]
        ) == sorted(
            [
                ("https://crm.example.com", "deal-create"),
                ("https://tickets.example.com", "ticket-create"),
            ]
        )

    def test_create_event_webhook_with_no_identifiers_carries_empty_list(
        self,
        event_service,
        mock_google_adapter,
        calendar,
        calendar_management_token,
        django_capture_on_commit_callbacks,
    ):
        baker.make(
            WebhookConfiguration,
            organization=calendar.organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/hooks/created",
        )
        mock_google_adapter.create_event.return_value = _adapter_output("evt-webhook-create-none")

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                event_service.create_event(calendar.id, _base_event_input())

        webhook_event = WebhookEvent.objects.filter_by_organization(calendar.organization_id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED
        )
        assert webhook_event.payload["external_client_identifiers"] == []

    def test_update_event_webhook_carries_identifiers(
        self,
        event_service,
        mock_google_adapter,
        calendar,
        calendar_management_token,
        social_account,
        django_capture_on_commit_callbacks,
    ):
        baker.make(
            WebhookConfiguration,
            organization=calendar.organization,
            event_type=WebhookEventType.CALENDAR_EVENT_UPDATED,
            url="https://example.com/hooks/updated",
        )
        mock_google_adapter.create_event.return_value = _adapter_output("evt-webhook-update")
        mock_google_adapter.update_event.return_value = _adapter_output("evt-webhook-update")

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                created = event_service.create_event(calendar.id, _base_event_input())
        _grant_event_owner_token(created, social_account.user, calendar.organization)

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                event_service.update_event(
                    calendar.id,
                    created.id,
                    _base_event_input(
                        external_client_identifiers=[
                            ExternalClientIdentifierData(
                                system="https://crm.example.com", identifier="deal-update"
                            )
                        ]
                    ),
                )

        webhook_event = WebhookEvent.objects.filter_by_organization(calendar.organization_id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_UPDATED
        )
        assert webhook_event.payload["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-update"}
        ]

    def test_add_attendee_webhook_embeds_event_identifiers(
        self,
        event_service,
        mock_google_adapter,
        calendar,
        calendar_management_token,
        social_account,
        django_capture_on_commit_callbacks,
    ):
        baker.make(
            WebhookConfiguration,
            organization=calendar.organization,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED,
            url="https://example.com/hooks/attendee-added",
        )
        mock_google_adapter.create_event.return_value = _adapter_output("evt-webhook-attendee")
        mock_google_adapter.update_event.return_value = _adapter_output("evt-webhook-attendee")

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                created = event_service.create_event(
                    calendar.id,
                    _base_event_input(
                        external_client_identifiers=[
                            ExternalClientIdentifierData(
                                system="https://crm.example.com", identifier="deal-attendee"
                            )
                        ]
                    ),
                )
        _grant_event_owner_token(created, social_account.user, calendar.organization)

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                event_service.update_event(
                    calendar.id,
                    created.id,
                    _base_event_input(
                        external_client_identifiers=[
                            ExternalClientIdentifierData(
                                system="https://crm.example.com", identifier="deal-attendee"
                            )
                        ],
                        external_attendances=[
                            EventExternalAttendanceInputData(
                                external_attendee=ExternalAttendeeInputData(
                                    email="attendee@example.com", name="Attendee"
                                )
                            )
                        ],
                    ),
                )

        webhook_event = WebhookEvent.objects.filter_by_organization(calendar.organization_id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED
        )
        assert webhook_event.payload["event"]["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-attendee"}
        ]

    def test_delete_event_webhook_payload_captures_identifiers_before_cascade(
        self,
        event_service,
        mock_google_adapter,
        calendar,
        calendar_management_token,
        social_account,
        django_capture_on_commit_callbacks,
    ):
        """The genuine ordering trap: the ``GenericRelation`` cascade removes
        ``ExternalClientIdentifier`` rows as part of deleting the event. The
        webhook payload must be a snapshot captured (and materialized) *before*
        the delete, or a naive implementation ships ``[]`` for every deleted
        event that had identifiers.
        """
        baker.make(
            WebhookConfiguration,
            organization=calendar.organization,
            event_type=WebhookEventType.CALENDAR_EVENT_DELETED,
            url="https://example.com/hooks/deleted",
        )
        mock_google_adapter.create_event.return_value = _adapter_output("evt-webhook-delete")

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                created = event_service.create_event(
                    calendar.id,
                    _base_event_input(
                        external_client_identifiers=[
                            ExternalClientIdentifierData(
                                system="https://crm.example.com", identifier="deal-delete"
                            )
                        ]
                    ),
                )
        _grant_event_owner_token(created, social_account.user, calendar.organization)
        event_id = created.id

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                event_service.delete_event(calendar.id, event_id)

        # The row -- and, by GenericRelation cascade, its identifiers -- is gone.
        assert (
            not CalendarEvent.objects.filter_by_organization(calendar.organization_id)
            .filter(id=event_id)
            .exists()
        )

        webhook_event = WebhookEvent.objects.filter_by_organization(calendar.organization_id).get(
            event_type=WebhookEventType.CALENDAR_EVENT_DELETED
        )
        assert webhook_event.payload["external_client_identifiers"] == [
            {"system": "https://crm.example.com", "identifier": "deal-delete"}
        ]


# ---------------------------------------------------------------------------
# No per-event identifier query when serializing N events
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_serialize_event_for_n_events_issues_no_per_event_identifier_query(
    django_assert_num_queries,
):
    """Serializing N events with their ``GenericRelation`` prefetched must not
    issue one identifier query per event. N=3 (>1) so a per-event query is
    unambiguously visible in the count: with the prefetch, evaluating
    ``serialize_event`` for all three events costs zero additional queries;
    without it, each event's ``.external_client_identifiers.all()`` call fires
    its own query and the count grows by one per event.
    """
    organization = baker.make(Organization, name="Query Count Org")
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_id="query-count-cal",
    )

    event_ids = []
    for n in range(3):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title=f"Query Count Event {n}",
            external_id=f"query-count-evt-{n}",
            start_time_tz_unaware=datetime.datetime(2025, 6, 1, 9 + n, 0, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 1, 10 + n, 0, 0),
            timezone="UTC",
        )
        create_external_client_identifier(
            organization=organization,
            identified_object=event,
            system="https://crm.example.com",
            identifier=f"deal-{event.id}",
        )
        event_ids.append(event.id)

    fetched_events = list(
        CalendarEvent.objects.filter_by_organization(organization.id)
        .filter(id__in=event_ids)
        .select_related("calendar_fk")
        .prefetch_related(
            "attendances",
            "external_attendances",
            "resource_allocations",
            "external_client_identifiers",
        )
    )
    assert len(fetched_events) == 3

    with django_assert_num_queries(0):
        serialized = [serialize_event(event) for event in fetched_events]

    for result in serialized:
        assert len(result.external_client_identifiers) == 1
