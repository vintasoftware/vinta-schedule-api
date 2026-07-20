"""Direct unit tests for ``CalendarEventService``.

These construct the event sub-service directly from a shared
:class:`CalendarServiceContext` (built by authenticating a facade, then reused —
the perf guardrail) and exercise create / update / delete of a single event, a
recurring-event path, and transfer against a mocked adapter. The exhaustive
event-behavior coverage stays in ``test_calendar_service.py`` against the facade;
these assert the extracted service behaves identically in isolation and that the
facade forwards to it.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarEvent, CalendarManagementToken
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


@pytest.fixture
def mock_google_adapter():
    """Mock Google Calendar adapter (mirrors the facade test suite fixture)."""
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        # Avoid Django expression-resolution attribute hits on the Mock.
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Event Service Org", should_sync_rooms=False)


@pytest.fixture
def social_account(db):
    user = User.objects.create_user(email="event-service@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="99999")


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
        name="Event Service Calendar",
        description="A test calendar",
        external_id="evt_cal_1",
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
        token_hash="evt_service_token_hash",
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
    """An authenticated facade — used only as the source of the shared context."""
    service = CalendarService()
    service.authenticate(account=social_account.user, organization=calendar.organization)
    return service


@pytest.fixture
def event_service(authenticated_facade):
    """``CalendarEventService`` wired from the facade's shared context.

    The facade is supplied as the ``host`` (availability — Phase 4, bundle fan-out —
    Phase 3, and the shared write-adapter / attendee-permission helpers route through
    it), exactly the wiring the facade uses internally.
    """
    return CalendarEventService(
        context=authenticated_facade._context,
        recurrence_manager=authenticated_facade._recurrence_manager,
        calendar_cache=authenticated_facade._calendar_cache,
        host=authenticated_facade,
    )


def _grant_event_owner_token(event, user, organization):
    """Create an owner-level event management token so update/delete permission passes."""
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"evt_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=organization.id,
        )
    return token


@pytest.fixture
def sample_event_input_data():
    return CalendarEventInputData(
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _adapter_output(external_id: str, *, recurrence_rule: str | None = None):
    return CalendarEventAdapterOutputData(
        calendar_external_id="evt_cal_1",
        external_id=external_id,
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule,
    )


@pytest.mark.django_db
def test_create_event(
    event_service, mock_google_adapter, calendar, calendar_management_token, sample_event_input_data
):
    """The service creates the event and forwards the adapter input."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_new_123")

    result = event_service.create_event(calendar.id, sample_event_input_data)

    assert result.external_id == "event_new_123"
    assert result.title == "New Event"
    assert result.calendar == calendar
    mock_google_adapter.create_event.assert_called_once()


@pytest.mark.django_db
def test_create_recurring_event(
    event_service, mock_google_adapter, calendar, calendar_management_token
):
    """The recurring create-shortcut persists the recurrence rule."""
    mock_google_adapter.create_event.return_value = _adapter_output(
        "recurring_event_123", recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO"
    )

    result = event_service.create_recurring_event(
        calendar_id=calendar.id,
        title="Weekly Meeting",
        description="Recurring weekly meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )

    assert result.external_id == "recurring_event_123"
    assert result.is_recurring is True
    assert result.recurrence_rule is not None


@pytest.mark.django_db
def test_update_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """The service updates an existing event in place."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_to_update")
    mock_google_adapter.update_event.return_value = _adapter_output("event_to_update")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = CalendarEventInputData(
        title="Updated Title",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    result = event_service.update_event(calendar.id, created.id, updated_input)

    assert result.id == created.id
    assert result.title == "Updated Title"
    mock_google_adapter.update_event.assert_called_once()


@pytest.mark.django_db
def test_delete_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """The service deletes the event and calls the adapter delete."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_to_delete")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    created_id = created.id
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    event_service.delete_event(calendar.id, created_id)

    assert not CalendarEvent.objects.filter(
        id=created_id, organization_id=calendar.organization_id
    ).exists()
    mock_google_adapter.delete_event.assert_called_once()


@pytest.mark.django_db
def test_transfer_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """Transfer creates a new event on the target calendar and removes the original."""
    mock_google_adapter.create_event.side_effect = [
        _adapter_output("original_event"),
        _adapter_output("transferred_event"),
    ]
    mock_google_adapter.get_event.return_value = _adapter_output("original_event")

    original = event_service.create_event(calendar.id, sample_event_input_data)
    _grant_event_owner_token(original, social_account.user, calendar.organization)

    target_calendar = Calendar.objects.create(
        name="Target Calendar",
        external_id="evt_target_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=calendar.organization,
    )
    OrganizationMembership.objects.get_or_create(
        user=social_account.user, organization=calendar.organization
    )
    target_token = CalendarManagementToken.objects.create(
        calendar=target_calendar,
        membership_user_id=social_account.user.id,
        token_hash="evt_target_token_hash",
        organization=calendar.organization,
    )
    target_token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        target_token.permissions.create(
            permission=permission_str,
            organization_id=calendar.organization_id,
        )

    new_event = event_service.transfer_event(original, target_calendar)

    assert new_event.calendar == target_calendar
    assert new_event.external_id == "transferred_event"
    assert not CalendarEvent.objects.filter(
        id=original.id, organization_id=calendar.organization_id
    ).exists()


@pytest.mark.django_db
def test_facade_create_event_delegates_to_event_service(
    authenticated_facade, sample_event_input_data
):
    """The facade's ``create_event`` forwards args + result to the event sub-service."""
    sentinel = object()
    fake_event_service = Mock()
    fake_event_service.create_event.return_value = sentinel

    with patch.object(authenticated_facade, "_get_event_service", return_value=fake_event_service):
        result = authenticated_facade.create_event(123, sample_event_input_data)

    assert result is sentinel
    fake_event_service.create_event.assert_called_once_with(
        123, sample_event_input_data, bypass_limits=False, _check_postpaid_allowance=True
    )


# ----------------------------------------------------------------------------------------
# Owner-scoped public-API event-creation allowance (Phase 3)
#
# ``create_event`` hard-blocks all SystemUser callers EXCEPT a token that is owner-scoped
# AND whose owner independently owns the target calendar (verified against CalendarOwnership,
# not trusted from the caller). These tests pin that authorization boundary at the service
# layer. The shared CalendarService facade (built via the DI container) supplies the wired
# context; create_event is exercised through it.
# ----------------------------------------------------------------------------------------


@pytest.fixture
def scoped_event_setup(db):
    """Build an org with a provider who owns a non-managed calendar.

    Returns a dict with ``organization``, ``owner`` (User), ``membership``
    (OrganizationMembership), and ``calendar`` (owned by ``owner``). The calendar has
    ``manage_available_windows=False`` so the whole range is bookable (no window setup
    needed) and no provider, so no external write adapter is invoked.
    """
    from calendar_integration.models import CalendarOwnership
    from organizations.models import OrganizationMembership

    organization = Organization.objects.create(name="Scoped Event Org", should_sync_rooms=False)
    owner = User.objects.create_user(email="scoped-owner@example.com", password="testpass123")
    Profile.objects.create(user=owner)
    membership = OrganizationMembership.objects.create(
        user=owner, organization=organization, is_active=True
    )
    calendar = Calendar.objects.create(
        name="Owned Calendar",
        external_id="owned_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    CalendarOwnership.objects.create(
        calendar=calendar,
        membership_user_id=owner.id,
        organization=organization,
    )
    return {
        "organization": organization,
        "owner": owner,
        "membership": membership,
        "calendar": calendar,
    }


def _scoped_event_input():
    return CalendarEventInputData(
        title="Scoped Event",
        description="Scheduled by an owner-scoped token",
        start_time=datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 1, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _facade_for_system_user(system_user, organization):
    """Return a DI-wired CalendarService facade initialized for the given SystemUser."""
    from di_core.containers import container

    service = container.calendar_service()
    service.initialize_without_provider(user_or_token=system_user, organization=organization)
    return service


@pytest.mark.django_db
def test_create_event_allows_owner_scoped_system_user_on_owned_calendar(scoped_event_setup):
    """An owner-scoped token whose owner owns the calendar may create an event."""
    from public_api.services import PublicAPIAuthService

    org = scoped_event_setup["organization"]
    calendar = scoped_event_setup["calendar"]
    membership = scoped_event_setup["membership"]

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="scoped_event_svc_owned",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    event = facade.create_event(calendar.id, _scoped_event_input())

    assert event.id is not None
    assert event.calendar_fk_id == calendar.id
    assert event.title == "Scoped Event"


@pytest.mark.django_db
def test_create_event_still_blocks_org_wide_system_user(scoped_event_setup):
    """An org-wide (unscoped) token is still blocked from creating events."""
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    org = scoped_event_setup["organization"]
    calendar = scoped_event_setup["calendar"]

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="org_wide_event_svc",
        organization=org,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(PermissionDenied, match="Events cannot be created through the Public API"):
        facade.create_event(calendar.id, _scoped_event_input())

    assert not CalendarEvent.objects.filter(
        calendar_fk_id=calendar.id, organization_id=org.id
    ).exists()


@pytest.mark.django_db
def test_create_event_blocks_scoped_token_on_non_owned_calendar(scoped_event_setup):
    """A scoped token may not create on a calendar its owner does not own.

    The independent CalendarOwnership verification fails (no ownership row links the
    token's owner to this other calendar), so the block stays in force.
    """
    from django.core.exceptions import PermissionDenied

    from organizations.models import OrganizationMembership
    from public_api.services import PublicAPIAuthService

    org = scoped_event_setup["organization"]
    membership = scoped_event_setup["membership"]

    # A second provider's calendar with no ownership row for the first provider.
    other_calendar = Calendar.objects.create(
        name="Other Calendar",
        external_id="other_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=org,
    )
    # (sanity: the token is scoped to a real membership but does NOT own other_calendar)
    # OrganizationMembership has a composite PK (user, organization); identify it by pair.
    assert OrganizationMembership.objects.filter(
        user_id=membership.user_id, organization_id=membership.organization_id
    ).exists()

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="scoped_event_svc_foreign",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(PermissionDenied, match="Events cannot be created through the Public API"):
        facade.create_event(other_calendar.id, _scoped_event_input())

    assert not CalendarEvent.objects.filter(
        calendar_fk_id=other_calendar.id, organization_id=org.id
    ).exists()


@pytest.mark.django_db
def test_create_event_owner_scoped_audit_actor_is_system_user(scoped_event_setup):
    """The post-commit audit actor falls back to the SystemUser (no AttributeError).

    The owner-scoped path never initializes a permission token, so the on_commit side
    effect must read the (absent) token defensively and use the SystemUser as the actor.
    Running with ``django_capture_on_commit_callbacks`` actually fires the callback, so a
    regression to direct ``permission_service.token`` access would raise here.
    """
    from public_api.services import PublicAPIAuthService

    org = scoped_event_setup["organization"]
    calendar = scoped_event_setup["calendar"]
    membership = scoped_event_setup["membership"]

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="scoped_event_svc_actor",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)

    recorded: list = []
    side_effects = facade.calendar_side_effects_service
    if side_effects is not None:
        original = side_effects.on_create_event

        def _spy(actor, event, organization):
            recorded.append(actor)
            return original(actor, event, organization)

        side_effects.on_create_event = _spy  # type: ignore[method-assign]

    try:
        # Fire on_commit callbacks inline so the side effect actually runs (a transaction
        # rollback in tests would otherwise swallow it and hide the actor-resolution bug).
        with patch("django.db.transaction.on_commit", side_effect=lambda func: func()):
            event = facade.create_event(calendar.id, _scoped_event_input())
    finally:
        if side_effects is not None:
            side_effects.on_create_event = original  # type: ignore[method-assign]

    assert event.id is not None
    # The callback fired (patched on_commit ran it inline) and the actor is the SystemUser.
    if side_effects is not None:
        assert recorded, "on_create_event side effect did not fire"
        assert recorded[0] == system_user


# ----------------------------------------------------------------------------------------
# Public-token update/delete write allowance (Phase 0a)
#
# ``update_event`` and ``delete_event`` now accept:
#   - Owner-scoped tokens whose owner owns the target calendar.
#   - Org-wide tokens for any calendar in their organization.
# Cross-owner scoped tokens and bundle calendars (for scoped tokens) are rejected.
# The regression test asserts that ``create_event`` for an org-wide token STAYS blocked.
# ----------------------------------------------------------------------------------------


@pytest.fixture
def write_allowance_setup(db):
    """Minimal org + two providers + two calendars for write-allowance tests.

    Returns a dict with:
    - ``organization`` — the shared org.
    - ``owner_a`` / ``membership_a`` / ``calendar_a`` — first provider (owns calendar_a).
    - ``owner_b`` / ``membership_b`` / ``calendar_b`` — second provider (owns calendar_b).
    - ``bundle_calendar`` — a BUNDLE-type calendar owned by ``owner_a``.

    No external-provider credentials are set; the calendar provider is left as None
    so no write adapter fires (pure-DB path), which is sufficient for authorization tests.
    """
    from calendar_integration.constants import CalendarType
    from calendar_integration.models import CalendarOwnership

    org = Organization.objects.create(name="Write Allowance Org", should_sync_rooms=False)

    owner_a = User.objects.create_user(email="write-owner-a@example.com", password="pw")
    Profile.objects.create(user=owner_a)
    membership_a = OrganizationMembership.objects.create(
        user=owner_a, organization=org, is_active=True
    )
    calendar_a = Calendar.objects.create(
        name="Calendar A",
        external_id="write_cal_a",
        organization=org,
    )
    CalendarOwnership.objects.create(
        calendar=calendar_a, membership_user_id=owner_a.id, organization=org
    )

    owner_b = User.objects.create_user(email="write-owner-b@example.com", password="pw")
    Profile.objects.create(user=owner_b)
    membership_b = OrganizationMembership.objects.create(
        user=owner_b, organization=org, is_active=True
    )
    calendar_b = Calendar.objects.create(
        name="Calendar B",
        external_id="write_cal_b",
        organization=org,
    )
    CalendarOwnership.objects.create(
        calendar=calendar_b, membership_user_id=owner_b.id, organization=org
    )

    bundle_calendar = Calendar.objects.create(
        name="Bundle Calendar",
        external_id="write_bundle_cal",
        organization=org,
        calendar_type=CalendarType.BUNDLE,
    )
    CalendarOwnership.objects.create(
        calendar=bundle_calendar, membership_user_id=owner_a.id, organization=org
    )

    return {
        "organization": org,
        "owner_a": owner_a,
        "membership_a": membership_a,
        "calendar_a": calendar_a,
        "owner_b": owner_b,
        "membership_b": membership_b,
        "calendar_b": calendar_b,
        "bundle_calendar": bundle_calendar,
    }


def _make_event_for_write_tests(organization, calendar) -> CalendarEvent:
    """Create a CalendarEvent directly in the DB (no adapter needed for auth tests)."""
    return CalendarEvent.objects.create(
        title="Test Event",
        description="A test event",
        start_time_tz_unaware=datetime.datetime(2026, 7, 10, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 7, 10, 11, 0),
        timezone="UTC",
        external_id=f"write-evt-{calendar.id}",
        calendar=calendar,
        organization=organization,
    )


def _updated_event_input():
    return CalendarEventInputData(
        title="Updated Event",
        description="Updated",
        start_time=datetime.datetime(2026, 7, 10, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 10, 13, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


# ------------------------------------------------------------------
# Owner-scoped token on OWNED calendar — update and delete succeed
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_allows_owner_scoped_system_user_on_owned_calendar(write_allowance_setup):
    """An owner-scoped token whose owner owns the calendar may update its event."""
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_update_scoped_owned",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    updated = facade.update_event(calendar.id, event.id, _updated_event_input())

    assert updated.id == event.id
    assert updated.title == "Updated Event"


@pytest.mark.django_db
def test_delete_event_allows_owner_scoped_system_user_on_owned_calendar(write_allowance_setup):
    """An owner-scoped token whose owner owns the calendar may delete its event."""
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar)
    event_id = event.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_delete_scoped_owned",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    facade.delete_event(calendar.id, event_id)

    assert not CalendarEvent.objects.filter(id=event_id, organization_id=org.id).exists()


# ------------------------------------------------------------------
# Owner-scoped token on FOREIGN (cross-owner) calendar — rejected
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_blocks_owner_scoped_system_user_on_foreign_calendar(write_allowance_setup):
    """An owner-scoped token is denied on a calendar its owner does not own.

    The not-found-parity message ``'Calendar matching query does not exist.'``
    is returned so the caller cannot distinguish a forbidden calendar from a
    genuinely missing one.
    """
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    # calendar_b is owned by owner_b; the scoped token is for owner_a's membership.
    calendar_b = setup["calendar_b"]
    membership_a = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar_b)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_deny_update_scoped_foreign",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(PermissionDenied, match=r"Calendar matching query does not exist\."):
        facade.update_event(calendar_b.id, event.id, _updated_event_input())

    # The event must be unchanged.
    assert CalendarEvent.objects.filter(id=event.id, organization_id=org.id).exists()


@pytest.mark.django_db
def test_delete_event_blocks_owner_scoped_system_user_on_foreign_calendar(write_allowance_setup):
    """An owner-scoped token is denied when deleting on a cross-owner calendar.

    The not-found-parity message is returned; the event row survives.
    """
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar_b = setup["calendar_b"]
    membership_a = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar_b)
    event_id = event.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_deny_delete_scoped_foreign",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(PermissionDenied, match=r"Calendar matching query does not exist\."):
        facade.delete_event(calendar_b.id, event_id)

    assert CalendarEvent.objects.filter(id=event_id, organization_id=org.id).exists()


# ------------------------------------------------------------------
# Org-wide token — update and delete succeed on any org calendar
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_allows_org_wide_system_user(write_allowance_setup):
    """An org-wide token may update any event in the organization."""
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    # Use calendar_b (not owned by any membership associated with the token).
    calendar = setup["calendar_b"]

    event = _make_event_for_write_tests(org, calendar)

    # Org-wide token: no scoped_to_membership.
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_update_org_wide",
        organization=org,
    )

    facade = _facade_for_system_user(system_user, org)
    updated = facade.update_event(calendar.id, event.id, _updated_event_input())

    assert updated.id == event.id
    assert updated.title == "Updated Event"


@pytest.mark.django_db
def test_delete_event_allows_org_wide_system_user(write_allowance_setup):
    """An org-wide token may delete any event in the organization."""
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_b"]

    event = _make_event_for_write_tests(org, calendar)
    event_id = event.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_delete_org_wide",
        organization=org,
    )

    facade = _facade_for_system_user(system_user, org)
    facade.delete_event(calendar.id, event_id)

    assert not CalendarEvent.objects.filter(id=event_id, organization_id=org.id).exists()


# ------------------------------------------------------------------
# Regression: create_event for an org-wide token STAYS blocked
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_create_event_org_wide_system_user_stays_blocked(write_allowance_setup):
    """Regression: org-wide tokens are still forbidden from creating events.

    ``update_event``/``delete_event`` now allow org-wide tokens, but ``create_event``
    must remain blocked — this is an explicit divergence captured here so it
    does not regress silently.
    """
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_regression_create_org_wide",
        organization=org,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(PermissionDenied, match="Events cannot be created through the Public API"):
        facade.create_event(calendar.id, _scoped_event_input())

    assert not CalendarEvent.objects.filter(
        calendar_fk_id=calendar.id, organization_id=org.id
    ).exists()


@pytest.mark.django_db
def test_create_event_allows_scoped_token_on_owned_bundle_calendar(write_allowance_setup):
    """A scoped token that OWNS the bundle calendar may CREATE a bundle event.

    Bundle creation routes to ``_create_bundle_event`` (the designed fan-out), permitted
    through the Public API for an owner-scoped token that owns the bundle calendar. This
    is the create-side parity of the bundle reschedule/cancel allowance. Org-wide create
    stays blocked (see ``test_create_event_org_wide_system_user_stays_blocked``). The
    fan-out is mocked to isolate the authorization decision (no child/availability setup).
    """
    from unittest import mock

    from calendar_integration.constants import CalendarType
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    bundle = setup["bundle_calendar"]
    membership_a = setup["membership_a"]
    assert bundle.calendar_type == CalendarType.BUNDLE

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_create_bundle",
        organization=org,
        scoped_to_membership=membership_a,
    )

    sentinel_event = _make_event_for_write_tests(org, bundle)

    facade = _facade_for_system_user(system_user, org)
    with mock.patch.object(
        facade, "_create_bundle_event", return_value=sentinel_event
    ) as mock_create_bundle:
        result = facade.create_event(bundle.id, _scoped_event_input())

    # No PermissionDenied raised; the bundle create fan-out was reached.
    mock_create_bundle.assert_called_once()
    assert result.id == sentinel_event.id


# ------------------------------------------------------------------
# Scoped token writing to a BUNDLE calendar it owns — ALLOWED
# (bundle reschedule/cancel is permitted through the Public API, unlike create)
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_allows_scoped_token_on_owned_bundle_primary_event(write_allowance_setup):
    """A scoped token that OWNS the bundle calendar may update a bundle PRIMARY event.

    Updating an existing bundle primary event is a well-defined operation that reaches
    ``_update_bundle_event`` (the fan-out). It is permitted through the Public API,
    gated only by ``_public_token_may_write`` — unlike ``create_event``, which blocks
    creation on a bundle calendar. The bundle fan-out is mocked so the test isolates the
    authorization decision (no provider/child setup needed).
    """
    from unittest import mock

    from calendar_integration.constants import CalendarType
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    bundle = setup["bundle_calendar"]
    membership_a = setup["membership_a"]

    event = CalendarEvent.objects.create(
        title="Bundle Event",
        description="On the bundle calendar",
        start_time_tz_unaware=datetime.datetime(2026, 7, 10, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 7, 10, 11, 0),
        timezone="UTC",
        external_id="bundle-evt-update",
        calendar=bundle,
        organization=org,
        is_bundle_primary=True,
    )
    assert bundle.calendar_type == CalendarType.BUNDLE

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_update_bundle",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)
    with mock.patch.object(
        facade, "_update_bundle_event", return_value=event
    ) as mock_update_bundle:
        result = facade.update_event(bundle.id, event.id, _updated_event_input())

    # No PermissionDenied raised; the bundle primary path was reached.
    mock_update_bundle.assert_called_once()
    assert result.id == event.id


@pytest.mark.django_db
def test_delete_event_allows_scoped_token_on_owned_bundle_primary_event(write_allowance_setup):
    """A scoped token that OWNS the bundle calendar may delete a bundle PRIMARY event.

    Deleting an existing bundle primary event reaches ``_delete_bundle_event`` (the
    fan-out), permitted through the Public API and gated only by
    ``_public_token_may_write``. The fan-out is mocked to isolate the authorization.
    """
    from unittest import mock

    from calendar_integration.constants import CalendarType
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    bundle = setup["bundle_calendar"]
    membership_a = setup["membership_a"]

    event = CalendarEvent.objects.create(
        title="Bundle Event",
        description="On the bundle calendar",
        start_time_tz_unaware=datetime.datetime(2026, 7, 10, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 7, 10, 11, 0),
        timezone="UTC",
        external_id="bundle-evt-delete",
        calendar=bundle,
        organization=org,
        is_bundle_primary=True,
    )
    assert bundle.calendar_type == CalendarType.BUNDLE

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="write_allow_delete_bundle",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)
    with mock.patch.object(facade, "_delete_bundle_event") as mock_delete_bundle:
        facade.delete_event(bundle.id, event.id)

    # No PermissionDenied raised; the bundle primary delete path was reached
    # (the fan-out itself is mocked, so the row is not actually removed here).
    mock_delete_bundle.assert_called_once()


# ------------------------------------------------------------------
# Django User path — behavior unchanged (regression)
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_django_user_path_still_uses_permission_token(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """The Django User principal still uses the permission-token flow for update.

    The new ``is_public_token_write`` branch must NOT fire for a ``User`` principal.
    This test mirrors the existing ``test_update_event`` to confirm regression-safety.
    """
    mock_google_adapter.create_event.return_value = _adapter_output("user_path_event")
    mock_google_adapter.update_event.return_value = _adapter_output("user_path_event")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = CalendarEventInputData(
        title="User-Path Update",
        description="Updated via User principal",
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    result = event_service.update_event(calendar.id, created.id, updated_input)

    assert result.id == created.id
    assert result.title == "User-Path Update"
    mock_google_adapter.update_event.assert_called_once()


# ------------------------------------------------------------------
# Cross-tenant: org-wide token in org A cannot write to org B's events
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_org_wide_token_denied_across_tenants(db):
    """An org-wide token in org A is denied when targeting org B's calendar/event.

    The defense-in-depth guard in _public_token_may_write compares
    system_user.organization_id to calendar.organization_id directly, so a
    cross-tenant write is blocked even if the caller supplies org A as context.
    """
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    org_a = Organization.objects.create(name="Cross Tenant Org A", should_sync_rooms=False)
    org_b = Organization.objects.create(name="Cross Tenant Org B", should_sync_rooms=False)

    calendar_b = Calendar.objects.create(
        name="Org B Calendar",
        external_id="cross_tenant_cal_b_update",
        organization=org_b,
    )
    event_b = CalendarEvent.objects.create(
        title="Org B Event",
        description="",
        start_time_tz_unaware=datetime.datetime(2026, 8, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 8, 1, 11, 0),
        timezone="UTC",
        external_id="cross-tenant-evt-update",
        calendar=calendar_b,
        organization=org_b,
    )

    # Org-wide token minted for org A.
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="cross_tenant_org_wide_update",
        organization=org_a,
    )

    # Initialize the facade with org A as context but target org B's calendar/event.
    facade = _facade_for_system_user(system_user, org_a)
    with pytest.raises((PermissionDenied, CalendarEvent.DoesNotExist)):
        facade.update_event(calendar_b.id, event_b.id, _updated_event_input())

    # The event must survive.
    assert CalendarEvent.objects.filter(id=event_b.id, organization_id=org_b.id).exists()


@pytest.mark.django_db
def test_delete_event_org_wide_token_denied_across_tenants(db):
    """An org-wide token in org A is denied when deleting org B's event.

    Mirrors the update cross-tenant test; pins the tenant boundary for delete.
    """
    from django.core.exceptions import PermissionDenied

    from public_api.services import PublicAPIAuthService

    org_a = Organization.objects.create(name="Cross Tenant Del Org A", should_sync_rooms=False)
    org_b = Organization.objects.create(name="Cross Tenant Del Org B", should_sync_rooms=False)

    calendar_b = Calendar.objects.create(
        name="Org B Calendar",
        external_id="cross_tenant_cal_b_delete",
        organization=org_b,
    )
    event_b = CalendarEvent.objects.create(
        title="Org B Event",
        description="",
        start_time_tz_unaware=datetime.datetime(2026, 8, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 8, 1, 11, 0),
        timezone="UTC",
        external_id="cross-tenant-evt-delete",
        calendar=calendar_b,
        organization=org_b,
    )
    event_id = event_b.id

    # Org-wide token minted for org A.
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="cross_tenant_org_wide_delete",
        organization=org_a,
    )

    facade = _facade_for_system_user(system_user, org_a)
    with pytest.raises((PermissionDenied, CalendarEvent.DoesNotExist)):
        facade.delete_event(calendar_b.id, event_id)

    # The event must survive.
    assert CalendarEvent.objects.filter(id=event_id, organization_id=org_b.id).exists()


# ------------------------------------------------------------------
# Audit actor: owner-scoped / org-wide token → actor is the SystemUser
# ------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_owner_scoped_audit_actor_is_system_user(write_allowance_setup):
    """The on_update_event side-effect actor is the SystemUser, not None.

    The owner-scoped path (SystemUser principal) never populates a permission
    token on the permission service.  The previous code did an unconditional
    ``context.calendar_permission_service.token`` access that would raise
    AttributeError.  After the fix the code falls back to ``context.user_or_token``
    (the SystemUser).  Patching ``transaction.on_commit`` to fire inline lets us
    capture the actor that reaches ``on_update_event``.
    """
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="audit_actor_update_scoped",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)

    recorded: list = []
    side_effects = facade.calendar_side_effects_service
    if side_effects is not None:
        original = side_effects.on_update_event

        def _spy(actor, event, organization):
            recorded.append(actor)
            return original(actor, event, organization)

        side_effects.on_update_event = _spy  # type: ignore[method-assign]

    try:
        with patch("django.db.transaction.on_commit", side_effect=lambda func: func()):
            facade.update_event(calendar.id, event.id, _updated_event_input())
    finally:
        if side_effects is not None:
            side_effects.on_update_event = original  # type: ignore[method-assign]

    if side_effects is not None:
        assert recorded, "on_update_event side effect did not fire"
        assert recorded[0] == system_user


@pytest.mark.django_db
def test_delete_event_owner_scoped_audit_actor_is_system_user(write_allowance_setup):
    """The on_delete_event side-effect actor is the SystemUser, not None.

    Mirrors ``test_update_event_owner_scoped_audit_actor_is_system_user`` for
    the delete path.  The actor is captured before ``event.delete()`` and closed
    over into the on_commit lambda; patching on_commit to fire inline lets the spy
    observe the actor.
    """
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    event = _make_event_for_write_tests(org, calendar)
    event_id = event.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="audit_actor_delete_scoped",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)

    recorded: list = []
    side_effects = facade.calendar_side_effects_service
    if side_effects is not None:
        original = side_effects.on_delete_event

        def _spy(actor, event, organization):
            recorded.append(actor)
            return original(actor, event, organization)

        side_effects.on_delete_event = _spy  # type: ignore[method-assign]

    try:
        with patch("django.db.transaction.on_commit", side_effect=lambda func: func()):
            facade.delete_event(calendar.id, event_id)
    finally:
        if side_effects is not None:
            side_effects.on_delete_event = original  # type: ignore[method-assign]

    assert not CalendarEvent.objects.filter(id=event_id, organization_id=org.id).exists()

    if side_effects is not None:
        assert recorded, "on_delete_event side effect did not fire"
        assert recorded[0] == system_user


# ----------------------------------------------------------------------------------------
# Single-occurrence reschedule / cancel service methods (Phase 0b)
#
# ``reschedule_event_occurrence`` and ``cancel_event_occurrence`` address ONE occurrence of
# a recurring series by ``(calendar_id, master_event_id, recurrence_id)`` and link it via an
# ``EventRecurrenceException`` — without mutating the master or its recurrence rule. They
# authorize through the same ``_public_token_may_write`` seam (org-wide AND owner-scoped).
# ----------------------------------------------------------------------------------------


_RECURRENCE_ID = datetime.datetime(2026, 7, 17, 10, 0, tzinfo=datetime.UTC)


def _make_recurring_master_for_write_tests(organization, calendar) -> CalendarEvent:
    """Create a recurring master CalendarEvent (weekly) directly in the DB."""
    from calendar_integration.models import RecurrenceRule

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=10", organization)
    rule.save()
    master = CalendarEvent.objects.create(
        title="Weekly Standup",
        description="Recurring standup",
        start_time_tz_unaware=datetime.datetime(2026, 7, 10, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 7, 10, 11, 0),
        timezone="UTC",
        external_id=f"recurring-master-{calendar.id}",
        calendar=calendar,
        organization=organization,
    )
    master.recurrence_rule = rule
    master.save()
    return master


@pytest.mark.django_db
def test_reschedule_event_occurrence_creates_modified_exception(write_allowance_setup):
    """An owner-scoped token reschedules one occurrence → modified exception created.

    The exception carries ``is_cancelled=False`` and a ``modified_event`` at the new
    time; the master event and its recurrence rule are left untouched.
    """
    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar)
    master_rule_id = master.recurrence_rule.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="reschedule_occurrence_scoped",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    new_start = datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC)
    new_end = datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC)

    modified = facade.reschedule_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
        start_time=new_start,
        end_time=new_end,
        timezone="UTC",
    )

    # Returned modified-occurrence event.
    assert modified.is_recurring_exception is True
    assert modified.parent_recurring_object_fk_id == master.id
    assert modified.recurrence_id == _RECURRENCE_ID
    assert modified.start_time == new_start
    assert modified.end_time == new_end

    # Exactly one exception, modified (not cancelled), pointing at the new event.
    exceptions = EventRecurrenceException.objects.filter(
        parent_event_fk=master, organization_id=org.id
    )
    assert exceptions.count() == 1
    exception = exceptions.get()
    assert exception.is_cancelled is False
    assert exception.modified_event_fk_id == modified.id
    assert exception.exception_date == _RECURRENCE_ID

    # The master + its rule are untouched.
    master.refresh_from_db()
    assert master.recurrence_rule_fk_id == master_rule_id
    assert master.title == "Weekly Standup"
    assert master.is_recurring is True
    # The master's own time was NOT moved (only the occurrence's modified event was).
    assert master.start_time == datetime.datetime(2026, 7, 10, 10, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_reschedule_event_occurrence_is_idempotent(write_allowance_setup):
    """Rescheduling the same occurrence twice updates in place (one exception row)."""
    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="reschedule_occurrence_idempotent",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)

    facade.reschedule_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
        start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )
    second = facade.reschedule_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
        start_time=datetime.datetime(2026, 7, 17, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 17, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )

    exceptions = EventRecurrenceException.objects.filter(
        parent_event_fk=master, organization_id=org.id
    )
    assert exceptions.count() == 1
    exception = exceptions.get()
    # The single exception now points at the most recently created modified event.
    assert exception.modified_event_fk_id == second.id
    assert exception.is_cancelled is False
    assert second.start_time == datetime.datetime(2026, 7, 17, 16, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_cancel_event_occurrence_creates_cancellation_exception(write_allowance_setup):
    """An owner-scoped token cancels one occurrence → cancellation exception created.

    The exception carries ``is_cancelled=True`` and no ``modified_event``; the master
    is untouched.
    """
    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar)
    master_rule_id = master.recurrence_rule.id

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="cancel_occurrence_scoped",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    result = facade.cancel_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
    )
    assert result is None

    exceptions = EventRecurrenceException.objects.filter(
        parent_event_fk=master, organization_id=org.id
    )
    assert exceptions.count() == 1
    exception = exceptions.get()
    assert exception.is_cancelled is True
    assert exception.modified_event_fk_id is None
    assert exception.exception_date == _RECURRENCE_ID

    # Master + rule untouched.
    master.refresh_from_db()
    assert master.recurrence_rule_fk_id == master_rule_id
    assert master.is_recurring is True


@pytest.mark.django_db
def test_cancel_event_occurrence_is_idempotent(write_allowance_setup):
    """Cancelling the same occurrence twice keeps a single exception row."""
    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="cancel_occurrence_idempotent",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    facade.cancel_event_occurrence(
        calendar_id=calendar.id, master_event_id=master.id, recurrence_id=_RECURRENCE_ID
    )
    facade.cancel_event_occurrence(
        calendar_id=calendar.id, master_event_id=master.id, recurrence_id=_RECURRENCE_ID
    )

    assert (
        EventRecurrenceException.objects.filter(
            parent_event_fk=master, organization_id=org.id
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_occurrence_methods_allow_org_wide_token(write_allowance_setup):
    """An org-wide token may reschedule AND cancel an occurrence (acts org-wide).

    Uses calendar_b, which no token-associated membership owns — proving org-wide
    reach (owner-scoped ownership is NOT required for org-wide tokens).
    """
    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_b"]

    master = _make_recurring_master_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_org_wide",
        organization=org,
    )

    facade = _facade_for_system_user(system_user, org)

    modified = facade.reschedule_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
        start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )
    assert modified.parent_recurring_object_fk_id == master.id

    other_recurrence_id = datetime.datetime(2026, 7, 24, 10, 0, tzinfo=datetime.UTC)
    facade.cancel_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=other_recurrence_id,
    )

    assert (
        EventRecurrenceException.objects.filter(
            parent_event_fk=master, organization_id=org.id
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_occurrence_methods_block_owner_scoped_on_foreign_master(write_allowance_setup):
    """An owner-scoped token cannot reschedule/cancel a cross-owner master.

    The not-found-parity message is raised and NO exception row is created.
    """
    from django.core.exceptions import PermissionDenied

    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    # calendar_b is owned by owner_b; the token is scoped to owner_a's membership.
    calendar_b = setup["calendar_b"]
    membership_a = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar_b)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_deny_foreign",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)

    with pytest.raises(PermissionDenied, match=r"Calendar matching query does not exist\."):
        facade.reschedule_event_occurrence(
            calendar_id=calendar_b.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
            start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
    with pytest.raises(PermissionDenied, match=r"Calendar matching query does not exist\."):
        facade.cancel_event_occurrence(
            calendar_id=calendar_b.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
        )

    assert not EventRecurrenceException.objects.filter(
        parent_event_fk=master, organization_id=org.id
    ).exists()


@pytest.mark.django_db
def test_occurrence_methods_cross_tenant_master_denied():
    """A token in org A cannot address a recurring master in org B.

    Either the org-scoped load misses (``CalendarEvent.DoesNotExist``) or the
    public-token guard denies (``PermissionDenied``); no exception row is created.
    """
    from django.core.exceptions import PermissionDenied

    from calendar_integration.models import EventRecurrenceException
    from public_api.services import PublicAPIAuthService

    org_a = Organization.objects.create(name="Occ Cross Tenant A", should_sync_rooms=False)
    org_b = Organization.objects.create(name="Occ Cross Tenant B", should_sync_rooms=False)

    calendar_b = Calendar.objects.create(
        name="Org B Calendar",
        external_id="occ_cross_tenant_cal_b",
        organization=org_b,
    )
    master = _make_recurring_master_for_write_tests(org_b, calendar_b)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occ_cross_tenant_org_wide",
        organization=org_a,
    )

    facade = _facade_for_system_user(system_user, org_a)
    with pytest.raises((PermissionDenied, CalendarEvent.DoesNotExist)):
        facade.cancel_event_occurrence(
            calendar_id=calendar_b.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
        )

    assert not EventRecurrenceException.objects.filter(
        parent_event_fk=master, organization_id=org_b.id
    ).exists()


@pytest.mark.django_db
def test_occurrence_methods_reject_non_recurring_master(write_allowance_setup):
    """A non-recurring master cannot be addressed by a single-occurrence op → ValueError."""
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    # A plain (non-recurring) event.
    master = _make_event_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_non_recurring",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)
    with pytest.raises(ValueError, match="non-recurring"):
        facade.reschedule_event_occurrence(
            calendar_id=calendar.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
            start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
    with pytest.raises(ValueError, match="non-recurring"):
        facade.cancel_event_occurrence(
            calendar_id=calendar.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
        )


@pytest.mark.django_db
def test_occurrence_methods_allowed_for_scoped_token_on_owned_bundle_calendar(
    write_allowance_setup,
):
    """A scoped token that OWNS the bundle calendar may reschedule/cancel an occurrence.

    Bundle reschedule/cancel is permitted through the Public API (mirrors update/delete):
    the occurrence loader gates only on ``_public_token_may_write``. Occurrence ops operate
    on the master's exception primitives (no bundle fan-out), so they create the expected
    ``EventRecurrenceException`` rows.
    """
    from calendar_integration.models import EventRecurrenceException, RecurrenceRule
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    bundle = setup["bundle_calendar"]
    membership_a = setup["membership_a"]

    # Create a recurring master directly on the bundle calendar.
    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=4", org)
    rule.save()
    master = CalendarEvent.objects.create(
        title="Bundle Recurring",
        description="",
        start_time_tz_unaware=datetime.datetime(2026, 7, 10, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 7, 10, 11, 0),
        timezone="UTC",
        external_id="bundle-recurring-master",
        calendar=bundle,
        organization=org,
    )
    master.recurrence_rule = rule
    master.save()

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_allow_bundle",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)

    # Reschedule one occurrence → a modified-occurrence exception is created (no bundle
    # fan-out; BUNDLE calendars have no write adapter, so the modified event is local).
    facade.reschedule_event_occurrence(
        calendar_id=bundle.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
        start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )
    exc = EventRecurrenceException.objects.get(parent_event_fk=master, organization_id=org.id)
    assert exc.is_cancelled is False
    assert exc.modified_event_fk_id is not None

    # Cancel the same occurrence → the exception upserts to cancelled.
    facade.cancel_event_occurrence(
        calendar_id=bundle.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,
    )
    exc.refresh_from_db()
    assert exc.is_cancelled is True


@pytest.mark.django_db
def test_reschedule_event_occurrence_calls_adapter_create_on_personal_calendar(
    write_allowance_setup,
):
    """On a PERSONAL calendar with a write adapter, reschedule calls create_event on the
    adapter with is_recurring_instance=True and captures the returned external_id.
    """
    from unittest.mock import Mock, patch

    from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar_a = setup["calendar_a"]
    membership_a = setup["membership_a"]

    master = _make_recurring_master_for_write_tests(org, calendar_a)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_adapter_create",
        organization=org,
        scoped_to_membership=membership_a,
    )

    facade = _facade_for_system_user(system_user, org)

    mock_adapter = Mock()
    mock_adapter.create_event.return_value = CalendarEventAdapterOutputData(
        calendar_external_id=calendar_a.external_id,
        external_id="occ-ext-123",
        title=master.title,
        description=master.description or "",
        start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )

    with patch.object(facade, "_get_write_adapter_for_calendar", return_value=mock_adapter):
        modified = facade.reschedule_event_occurrence(
            calendar_id=calendar_a.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
            start_time=datetime.datetime(2026, 7, 17, 14, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 7, 17, 15, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

    # create_event called once with is_recurring_instance=True.
    mock_adapter.create_event.assert_called_once()
    call_data = mock_adapter.create_event.call_args[0][0]
    assert call_data.is_recurring_instance is True

    # external_id captured from adapter response.
    assert modified.external_id == "occ-ext-123"

    # Repeated reschedule of the same recurrence_id → update_event (not create_event again).
    mock_adapter.update_event.return_value = CalendarEventAdapterOutputData(
        calendar_external_id=calendar_a.external_id,
        external_id="occ-ext-123",
        title=master.title,
        description=master.description or "",
        start_time=datetime.datetime(2026, 7, 17, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2026, 7, 17, 17, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )

    with patch.object(facade, "_get_write_adapter_for_calendar", return_value=mock_adapter):
        modified2 = facade.reschedule_event_occurrence(
            calendar_id=calendar_a.id,
            master_event_id=master.id,
            recurrence_id=_RECURRENCE_ID,
            start_time=datetime.datetime(2026, 7, 17, 16, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 7, 17, 17, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

    # Second reschedule updates in place → update_event called once, no second create.
    mock_adapter.update_event.assert_called_once()
    assert mock_adapter.create_event.call_count == 1  # still exactly 1 (no second create)
    # Same modified event updated in place (same id).
    assert modified2.id == modified.id
    assert modified2.external_id == "occ-ext-123"  # external_id preserved, no orphan


@pytest.mark.django_db
def test_reschedule_and_cancel_occurrence_expansion(write_allowance_setup):
    """After reschedule/cancel, get_occurrences_in_range reflects the change.

    Acceptance pin: the expansion correctly picks up the modified or cancelled
    occurrence so callers see the right timeline.
    """
    from public_api.services import PublicAPIAuthService

    setup = write_allowance_setup
    org = setup["organization"]
    calendar = setup["calendar_a"]
    membership = setup["membership_a"]

    # Master starts 2026-07-10 (Friday), repeats weekly for 4 occurrences:
    # Jul 10, Jul 17, Jul 24, Jul 31.
    master = _make_recurring_master_for_write_tests(org, calendar)

    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="occurrence_expansion",
        organization=org,
        scoped_to_membership=membership,
    )

    facade = _facade_for_system_user(system_user, org)

    # --- Reschedule the Jul-17 occurrence to 16:00 ---
    new_start = datetime.datetime(2026, 7, 17, 16, 0, tzinfo=datetime.UTC)
    new_end = datetime.datetime(2026, 7, 17, 17, 0, tzinfo=datetime.UTC)
    facade.reschedule_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=_RECURRENCE_ID,  # 2026-07-17 10:00 UTC
        start_time=new_start,
        end_time=new_end,
        timezone="UTC",
    )

    window_start = datetime.datetime(2026, 7, 1, 0, 0, tzinfo=datetime.UTC)
    window_end = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.UTC)
    occurrences = master.get_occurrences_in_range(window_start, window_end)

    # The Jul-17 occurrence must appear at the NEW time, not the original 10:00.
    jul17_occurrences = [o for o in occurrences if o.start_time.date().day == 17]
    assert len(jul17_occurrences) == 1
    assert jul17_occurrences[0].start_time == new_start

    # --- Cancel the Jul-24 occurrence ---
    jul24_recurrence_id = datetime.datetime(2026, 7, 24, 10, 0, tzinfo=datetime.UTC)
    facade.cancel_event_occurrence(
        calendar_id=calendar.id,
        master_event_id=master.id,
        recurrence_id=jul24_recurrence_id,
    )

    occurrences_after_cancel = master.get_occurrences_in_range(window_start, window_end)

    # Jul-24 must be absent; Jul-10, Jul-17 (rescheduled), Jul-31 remain.
    days_present = {o.start_time.date().day for o in occurrences_after_cancel}
    assert 24 not in days_present
    assert 10 in days_present
    assert 31 in days_present
