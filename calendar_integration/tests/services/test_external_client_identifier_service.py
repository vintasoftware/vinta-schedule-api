"""Unit tests for ExternalClientIdentifierService.

Covers:
- ``replace_for_target``: adds / removes / keeps identifiers correctly on a diff.
- ``None`` is a no-op (nothing written, old == new).
- ``[]`` clears every stored identifier for the target.
- ``system`` is normalized before comparison, so a re-send with different casing /
  trailing slash is NOT treated as a change (no delete+recreate).
- A non-allowlisted ``content_type`` is rejected.
- A target from another organization is rejected.
- Blank, whitespace-only and over-length identifiers are rejected.
- ``get_for_targets`` batch reads, keyed by (content_type_id, pk), defaulting missing
  targets to an empty list.
- Calling before ``initialize()`` raises ``CalendarServiceOrganizationNotSetError``.
"""

from __future__ import annotations

import datetime

import pytest
from model_bakery import baker

from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    ExternalClientIdentifierBlankIdentifierError,
    ExternalClientIdentifierCrossOrganizationError,
    ExternalClientIdentifierDuplicateSystemError,
    ExternalClientIdentifierInvalidSystemError,
    ExternalClientIdentifierInvalidTargetError,
    ExternalClientIdentifierTooLongError,
)
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import Calendar, CalendarEvent, ExternalAttendee
from calendar_integration.services.dataclasses import ExternalClientIdentifierData
from calendar_integration.services.external_client_identifier_service import (
    ExternalClientIdentifierService,
)
from organizations.models import Organization


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def other_organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


@pytest.fixture
def event(organization: Organization, calendar: Calendar) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Test Event",
        external_id="event-test-1",
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, 0),
        timezone="UTC",
    )


@pytest.fixture
def other_org_event(other_organization: Organization) -> CalendarEvent:
    other_calendar = baker.make(Calendar, organization=other_organization)
    return baker.make(
        CalendarEvent,
        organization=other_organization,
        calendar=other_calendar,
        title="Other Org Event",
        external_id="event-test-2",
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, 0),
        timezone="UTC",
    )


@pytest.fixture
def service(organization: Organization) -> ExternalClientIdentifierService:
    svc = ExternalClientIdentifierService()
    svc.initialize(organization)
    return svc


# ---------------------------------------------------------------------------
# Not initialized
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_replace_for_target_requires_initialize(event: CalendarEvent) -> None:
    svc = ExternalClientIdentifierService()
    with pytest.raises(CalendarServiceOrganizationNotSetError):
        svc.replace_for_target(event, None)


# ---------------------------------------------------------------------------
# Add / remove / keep diffing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_replace_for_target_creates_new_identifiers_from_empty(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    old, new = service.replace_for_target(
        event,
        [
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1"),
            ExternalClientIdentifierData(system="https://erp.example.com", identifier="po-9"),
        ],
    )

    assert old == []
    assert sorted((d.system, d.identifier) for d in new) == [
        ("https://crm.example.com", "deal-1"),
        ("https://erp.example.com", "po-9"),
    ]
    stored = {row.system: row.identifier for row in event.external_client_identifiers.all()}
    assert stored == {
        "https://crm.example.com": "deal-1",
        "https://erp.example.com": "po-9",
    }


@pytest.mark.django_db
def test_replace_for_target_adds_removes_and_keeps(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    kept = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://erp.example.com",
        identifier="po-9",
    )

    old, new = service.replace_for_target(
        event,
        [
            # Same (system, identifier) as `kept` -- must survive untouched.
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1"),
            # New system.
            ExternalClientIdentifierData(system="https://support.example.com", identifier="tk-3"),
            # "https://erp.example.com" is omitted -- must be removed.
        ],
    )

    assert sorted((d.system, d.identifier) for d in old) == [
        ("https://crm.example.com", "deal-1"),
        ("https://erp.example.com", "po-9"),
    ]
    assert sorted((d.system, d.identifier) for d in new) == [
        ("https://crm.example.com", "deal-1"),
        ("https://support.example.com", "tk-3"),
    ]

    stored = {row.system: row.identifier for row in event.external_client_identifiers.all()}
    assert stored == {
        "https://crm.example.com": "deal-1",
        "https://support.example.com": "tk-3",
    }
    # The untouched pair's row was never deleted+recreated.
    assert event.external_client_identifiers.get(system="https://crm.example.com").pk == kept.pk


@pytest.mark.django_db
def test_replace_for_target_updates_identifier_for_existing_system(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    """Same system, different identifier -- old row must go, new value must persist."""
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    old, new = service.replace_for_target(
        event,
        [ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-2")],
    )

    assert [(d.system, d.identifier) for d in old] == [("https://crm.example.com", "deal-1")]
    assert [(d.system, d.identifier) for d in new] == [("https://crm.example.com", "deal-2")]
    assert event.external_client_identifiers.count() == 1
    assert event.external_client_identifiers.get().identifier == "deal-2"


# ---------------------------------------------------------------------------
# None / [] tri-state
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_none_is_a_noop(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    old, new = service.replace_for_target(event, None)

    assert old == new
    assert [(d.system, d.identifier) for d in old] == [("https://crm.example.com", "deal-1")]
    assert event.external_client_identifiers.count() == 1


@pytest.mark.django_db
def test_empty_list_clears_all(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://erp.example.com",
        identifier="po-9",
    )

    old, new = service.replace_for_target(event, [])

    assert len(old) == 2
    assert new == []
    assert event.external_client_identifiers.count() == 0


@pytest.mark.django_db
def test_empty_list_on_already_empty_set_is_still_a_noop_result(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    old, new = service.replace_for_target(event, [])
    assert old == []
    assert new == []


# ---------------------------------------------------------------------------
# system normalization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_system_normalization_prevents_false_change(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    """A re-send with different casing/trailing-slash must not be treated as a change."""
    stored = create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    old, new = service.replace_for_target(
        event,
        [ExternalClientIdentifierData(system="HTTPS://CRM.EXAMPLE.COM/", identifier="deal-1")],
    )

    assert old == new
    assert [(d.system, d.identifier) for d in new] == [("https://crm.example.com", "deal-1")]
    # No delete+recreate: the original row survives with the same pk.
    assert event.external_client_identifiers.get().pk == stored.pk


# ---------------------------------------------------------------------------
# Allowlist + organization validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_non_allowlisted_content_type_is_rejected(
    service: ExternalClientIdentifierService, calendar: Calendar
) -> None:
    with pytest.raises(ExternalClientIdentifierInvalidTargetError):
        service.replace_for_target(
            calendar,
            [ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1")],
        )


@pytest.mark.django_db
def test_non_allowlisted_content_type_is_rejected_even_when_noop(
    service: ExternalClientIdentifierService, calendar: Calendar
) -> None:
    """The allowlist check runs even for a `None` (would-be no-op) call."""
    with pytest.raises(ExternalClientIdentifierInvalidTargetError):
        service.replace_for_target(calendar, None)


@pytest.mark.django_db
def test_cross_organization_target_is_rejected(
    service: ExternalClientIdentifierService, other_org_event: CalendarEvent
) -> None:
    with pytest.raises(ExternalClientIdentifierCrossOrganizationError):
        service.replace_for_target(
            other_org_event,
            [ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1")],
        )


@pytest.mark.django_db
def test_cross_organization_target_is_rejected_even_when_noop(
    service: ExternalClientIdentifierService, other_org_event: CalendarEvent
) -> None:
    with pytest.raises(ExternalClientIdentifierCrossOrganizationError):
        service.replace_for_target(other_org_event, None)


# ---------------------------------------------------------------------------
# identifier validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_blank_identifier_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    with pytest.raises(ExternalClientIdentifierBlankIdentifierError):
        service.replace_for_target(
            event,
            [ExternalClientIdentifierData(system="https://crm.example.com", identifier="")],
        )


@pytest.mark.django_db
def test_whitespace_only_identifier_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    with pytest.raises(ExternalClientIdentifierBlankIdentifierError):
        service.replace_for_target(
            event,
            [ExternalClientIdentifierData(system="https://crm.example.com", identifier="   ")],
        )


@pytest.mark.django_db
def test_over_length_identifier_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    with pytest.raises(ExternalClientIdentifierTooLongError):
        service.replace_for_target(
            event,
            [ExternalClientIdentifierData(system="https://crm.example.com", identifier="x" * 256)],
        )


@pytest.mark.django_db
def test_max_length_identifier_is_accepted(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    old, new = service.replace_for_target(
        event,
        [ExternalClientIdentifierData(system="https://crm.example.com", identifier="x" * 255)],
    )
    assert old == []
    assert len(new) == 1
    assert new[0].identifier == "x" * 255


@pytest.mark.django_db
def test_invalid_system_url_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    """``system`` is stored in a ``URLField`` and is the leading match column of
    ``extclientid_uniq_system_ident`` (after organization/content_type), so an
    unparseable value cannot be a stable lookup key. ``bulk_create`` never calls
    ``Model.full_clean()``, so the service must validate this explicitly."""
    with pytest.raises(ExternalClientIdentifierInvalidSystemError):
        service.replace_for_target(
            event,
            [ExternalClientIdentifierData(system="not-a-url", identifier="deal-1")],
        )


@pytest.mark.django_db
def test_invalid_system_url_leaves_target_untouched(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    create_external_client_identifier(
        organization=event.organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )
    with pytest.raises(ExternalClientIdentifierInvalidSystemError):
        service.replace_for_target(
            event,
            [ExternalClientIdentifierData(system="not-a-url", identifier="deal-2")],
        )
    old, new = service.replace_for_target(event, None)
    assert len(old) == 1
    assert old[0].identifier == "deal-1"
    assert new == old


@pytest.mark.django_db
def test_invalid_write_leaves_target_untouched(
    service: ExternalClientIdentifierService, organization: Organization, event: CalendarEvent
) -> None:
    """A rejected write must not partially apply -- the whole call raises before any
    delete/create happens (validation runs before the diff/apply step)."""
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )

    with pytest.raises(ExternalClientIdentifierBlankIdentifierError):
        service.replace_for_target(
            event,
            [
                ExternalClientIdentifierData(system="https://erp.example.com", identifier="po-9"),
                ExternalClientIdentifierData(system="https://support.example.com", identifier=""),
            ],
        )

    stored = {row.system: row.identifier for row in event.external_client_identifiers.all()}
    assert stored == {"https://crm.example.com": "deal-1"}


# ---------------------------------------------------------------------------
# duplicate system in one payload
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_duplicate_system_in_one_payload_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    """Two pairs with the same system in one call must raise, not silently keep the
    last one and drop the first."""
    with pytest.raises(ExternalClientIdentifierDuplicateSystemError):
        service.replace_for_target(
            event,
            [
                ExternalClientIdentifierData(system="https://crm.example.com", identifier="A"),
                ExternalClientIdentifierData(system="https://crm.example.com", identifier="B"),
            ],
        )


@pytest.mark.django_db
def test_duplicate_system_spelled_two_ways_in_one_payload_is_rejected(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    """The duplicate check runs on the normalized system, so different casing and a
    trailing slash for the same system still count as a duplicate."""
    with pytest.raises(ExternalClientIdentifierDuplicateSystemError):
        service.replace_for_target(
            event,
            [
                ExternalClientIdentifierData(system="https://crm.example.com", identifier="A"),
                ExternalClientIdentifierData(system="HTTPS://CRM.EXAMPLE.COM/", identifier="B"),
            ],
        )


@pytest.mark.django_db
def test_distinct_systems_in_one_payload_still_succeeds(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    old, new = service.replace_for_target(
        event,
        [
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="A"),
            ExternalClientIdentifierData(system="https://erp.example.com", identifier="B"),
        ],
    )
    assert old == []
    assert sorted((d.system, d.identifier) for d in new) == [
        ("https://crm.example.com", "A"),
        ("https://erp.example.com", "B"),
    ]


# ---------------------------------------------------------------------------
# get_for_targets
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_for_targets_batches_reads_across_models(
    service: ExternalClientIdentifierService,
    organization: Organization,
    event: CalendarEvent,
) -> None:
    attendee = baker.make(ExternalAttendee, organization=organization, email="a@example.com")
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="deal-1",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=attendee,
        system="https://crm.example.com",
        identifier="contact-7",
    )

    result = service.get_for_targets([event, attendee])

    from django.contrib.contenttypes.models import ContentType

    event_key = (ContentType.objects.get_for_model(event).id, event.pk)
    attendee_key = (ContentType.objects.get_for_model(attendee).id, attendee.pk)

    assert [(d.system, d.identifier) for d in result[event_key]] == [
        ("https://crm.example.com", "deal-1")
    ]
    assert [(d.system, d.identifier) for d in result[attendee_key]] == [
        ("https://crm.example.com", "contact-7")
    ]


@pytest.mark.django_db
def test_get_for_targets_defaults_missing_to_empty_list(
    service: ExternalClientIdentifierService, event: CalendarEvent
) -> None:
    result = service.get_for_targets([event])

    from django.contrib.contenttypes.models import ContentType

    key = (ContentType.objects.get_for_model(event).id, event.pk)
    assert result[key] == []


@pytest.mark.django_db
def test_get_for_targets_empty_sequence_returns_empty_dict(
    service: ExternalClientIdentifierService,
) -> None:
    assert service.get_for_targets([]) == {}


@pytest.mark.django_db
def test_get_for_targets_mixed_type_pk_collision_does_not_leak_unrequested_data(
    service: ExternalClientIdentifierService,
    organization: Organization,
    event: CalendarEvent,
    calendar: Calendar,
) -> None:
    """Pins the ``if key in result`` check in ``get_for_targets``.

    ``get_for_targets`` builds ``content_type_id__in`` and ``identified_key__in`` as
    two separate sets, not as matched (content_type, pk) pairs. For a batch that mixes
    content types, this reads as a cross product: it can match a row for a
    content-type/pk combination that nobody in the batch actually asked for.

    This test forces that combination to exist. It creates a third, unrequested
    ``CalendarEvent`` whose pk equals the requested ``ExternalAttendee``'s pk, and
    gives it its own identifier. The unrequested event's content type is the same as
    the requested event's, so the cross product now includes a row that belongs to
    neither requested target. Without the ``if key in result`` check, that row would
    end up under a key nobody asked for, or attached to the wrong target.
    """
    # Explicit, far-apart pks: the fixture `event` already holds a low auto-assigned
    # pk, so picking a large explicit pk for both `attendee` and `unrequested_event`
    # guarantees the forced collision is between those two, not an accidental one
    # with `event`.
    collision_pk = 900_001
    attendee = baker.make(
        ExternalAttendee, id=collision_pk, organization=organization, email="a@example.com"
    )
    unrequested_event = baker.make(
        CalendarEvent,
        id=collision_pk,
        organization=organization,
        calendar=calendar,
        title="Unrequested event sharing the attendee's pk",
        external_id="event-pk-collision",
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, 0),
        timezone="UTC",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=event,
        system="https://crm.example.com",
        identifier="event-ident",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=attendee,
        system="https://crm.example.com",
        identifier="attendee-ident",
    )
    create_external_client_identifier(
        organization=organization,
        identified_object=unrequested_event,
        system="https://crm.example.com",
        identifier="unrequested-event-ident",
    )

    result = service.get_for_targets([event, attendee])

    from django.contrib.contenttypes.models import ContentType

    event_key = (ContentType.objects.get_for_model(event).id, event.pk)
    attendee_key = (ContentType.objects.get_for_model(attendee).id, attendee.pk)

    # Only the two requested keys are present -- the unrequested event's key never
    # entered `result`, so a matched row for it has nowhere to be discarded except by
    # the guard.
    assert set(result.keys()) == {event_key, attendee_key}
    assert [(d.system, d.identifier) for d in result[event_key]] == [
        ("https://crm.example.com", "event-ident")
    ]
    assert [(d.system, d.identifier) for d in result[attendee_key]] == [
        ("https://crm.example.com", "attendee-ident")
    ]
