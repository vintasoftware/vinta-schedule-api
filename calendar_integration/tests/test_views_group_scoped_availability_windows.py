"""Integration tests for the internal REST surface exposing group-scoped
availability windows (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1c).

Covers:
- Full lifecycle through the nested routes (create -> list -> retrieve ->
  update -> delete), for both the calendar's owner and an org admin.
- Route-level group-visibility gating: a stranger and the owner of a
  *different* calendar in the same group (different slot) are both denied
  with the exact same not-found shape -- neither can distinguish "forbidden"
  from "does not exist".
- Cross-organization access denied with the same not-found shape.
- The orphaned-booking warning surfaces in the narrowing response, both on
  the first-window create and on a narrowing update.
"""

from __future__ import annotations

import datetime

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_client(membership: OrganizationMembership) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=membership.user)
    return client


def _next_weekday(after: datetime.datetime, weekday: int) -> datetime.date:
    """Next date (strictly after `after`'s date) landing on ISO `weekday`
    (Monday=0 ... Sunday=6)."""
    days_ahead = (weekday - after.weekday()) % 7
    days_ahead = days_ahead or 7
    return (after + datetime.timedelta(days=days_ahead)).date()


def _list_url(group_id: int, slot_id: int) -> str:
    return reverse(
        "api:GroupScopedAvailabilityWindows-list",
        kwargs={"group_id": group_id, "slot_id": slot_id},
    )


def _detail_url(group_id: int, slot_id: int, pk: int) -> str:
    return reverse(
        "api:GroupScopedAvailabilityWindows-detail",
        kwargs={"group_id": group_id, "slot_id": slot_id, "pk": pk},
    )


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="Windows REST Test Org")


@pytest.fixture
def admin_membership(organization: Organization) -> OrganizationMembership:
    user = UserFactory().create_user()
    return OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN, is_active=True
    )


@pytest.fixture
def owner_membership(organization: Organization) -> OrganizationMembership:
    user = UserFactory().create_user()
    return OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.MEMBER, is_active=True
    )


@pytest.fixture
def other_owner_membership(organization: Organization) -> OrganizationMembership:
    """Owns a DIFFERENT calendar (in a different slot of the SAME group) --
    used to prove that seeing the group is not enough to manage a calendar
    the caller does not own."""
    user = UserFactory().create_user()
    return OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.MEMBER, is_active=True
    )


@pytest.fixture
def stranger_membership(organization: Organization) -> OrganizationMembership:
    user = UserFactory().create_user()
    return OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.MEMBER, is_active=True
    )


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="dr_reyes",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Costa",
        external_id="dr_costa",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture(autouse=True)
def _ownerships(
    owner_membership: OrganizationMembership,
    other_owner_membership: OrganizationMembership,
    calendar: Calendar,
    other_calendar: Calendar,
) -> None:
    create_calendar_ownership(calendar=calendar, user=owner_membership.user)
    create_calendar_ownership(calendar=other_calendar, user=other_owner_membership.user)


@pytest.fixture
def group(organization: Organization) -> CalendarGroup:
    return CalendarGroup.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def group_slot(
    organization: Organization, group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Lead Surgeon"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def other_slot(
    organization: Organization, group: CalendarGroup, other_calendar: Calendar
) -> CalendarGroupSlot:
    """A second slot in the same group, populated with `other_calendar` -- makes
    `other_owner_membership`'s user a genuine member of the group without
    owning `calendar`."""
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Assist")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=other_calendar
    )
    return slot


def _create_payload(calendar_id: int, **overrides) -> dict:
    payload = {
        "calendar": calendar_id,
        "start_time": _utc(2025, 9, 2, 9).isoformat(),  # 2025-09-02 is a Tuesday
        "end_time": _utc(2025, 9, 2, 17).isoformat(),
        "timezone": "UTC",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedAvailabilityWindowLifecycle:
    def test_full_lifecycle_as_owner(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)

        # Create.
        create_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH"),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        window_data = create_response.data["window"]
        assert window_data["calendar_id"] == calendar.id
        assert window_data["group_slot_id"] == group_slot.id
        assert window_data["timezone"] == "UTC"
        assert window_data["is_recurring"] is True
        assert window_data["rrule_string"] == "FREQ=WEEKLY;BYDAY=TU,TH"
        assert create_response.data["orphaned_bookings"] == []
        window_id = window_data["id"]

        # Invisible on the base (unscoped-from-group) manager.
        assert (
            not AvailableTime.objects.filter_by_organization(calendar.organization_id)
            .filter(id=window_id)
            .exists()
        )
        assert (
            AvailableTime.objects.for_group_slot(group_slot.id)
            .filter_by_organization(calendar.organization_id)
            .filter(id=window_id)
            .exists()
        )

        # List.
        list_response = client.get(_list_url(group.id, group_slot.id))
        assert list_response.status_code == status.HTTP_200_OK
        ids = [w["id"] for w in list_response.data["results"]]
        assert ids == [window_id]

        # Retrieve.
        retrieve_response = client.get(_detail_url(group.id, group_slot.id, window_id))
        assert retrieve_response.status_code == status.HTTP_200_OK
        assert retrieve_response.data["id"] == window_id

        # Update (narrow the recurrence to Thursdays only).
        update_response = client.patch(
            _detail_url(group.id, group_slot.id, window_id),
            {"rrule_string": "RRULE:FREQ=WEEKLY;BYDAY=TH"},
            format="json",
        )
        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        assert update_response.data["window"]["rrule_string"] == "FREQ=WEEKLY;BYDAY=TH"
        assert update_response.data["orphaned_bookings"] == []

        # Delete.
        delete_response = client.delete(_detail_url(group.id, group_slot.id, window_id))
        assert delete_response.status_code == status.HTTP_204_NO_CONTENT
        assert not AvailableTime.objects.unscoped().filter(id=window_id).exists()

        # Now invisible everywhere, including the group-scoped read path.
        retrieve_after_delete = client.get(_detail_url(group.id, group_slot.id, window_id))
        assert retrieve_after_delete.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_can_manage_any_calendars_window(
        self,
        admin_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(admin_membership)

        create_response = client.post(
            _list_url(group.id, group_slot.id), _create_payload(calendar.id), format="json"
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        window_id = create_response.data["window"]["id"]

        update_response = client.patch(
            _detail_url(group.id, group_slot.id, window_id), {"timezone": "America/Sao_Paulo"}
        )
        assert update_response.status_code == status.HTTP_200_OK
        assert update_response.data["window"]["timezone"] == "America/Sao_Paulo"

        delete_response = client.delete(_detail_url(group.id, group_slot.id, window_id))
        assert delete_response.status_code == status.HTTP_204_NO_CONTENT

    def test_create_requires_start_before_end(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)
        response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(
                calendar.id,
                start_time=_utc(2025, 9, 2, 17).isoformat(),
                end_time=_utc(2025, 9, 2, 9).isoformat(),
            ),
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_put_not_allowed(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)
        create_response = client.post(
            _list_url(group.id, group_slot.id), _create_payload(calendar.id), format="json"
        )
        window_id = create_response.data["window"]["id"]

        response = client.put(
            _detail_url(group.id, group_slot.id, window_id),
            {"timezone": "America/Sao_Paulo"},
            format="json",
        )
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_patch_null_rrule_string_clears_recurrence(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        """PATCH `{"rrule_string": null}` is the tri-state "clear" case -- it
        must actually detach the recurrence rule, not be a silent no-op."""
        client = _auth_client(owner_membership)
        create_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH"),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        window_id = create_response.data["window"]["id"]

        update_response = client.patch(
            _detail_url(group.id, group_slot.id, window_id),
            {"rrule_string": None},
            format="json",
        )
        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        window_data = update_response.data["window"]
        assert window_data["rrule_string"] is None
        assert window_data["is_recurring"] is False

        reloaded = (
            AvailableTime.objects.unscoped()
            .filter_by_organization(calendar.organization_id)
            .get(id=window_id)
        )
        assert reloaded.recurrence_rule is None
        assert reloaded.is_recurring is False

    def test_patch_omitting_rrule_string_leaves_recurrence_unchanged(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        """PATCH that never mentions `rrule_string` must leave the existing
        recurrence untouched -- the "absent" tri-state case."""
        client = _auth_client(owner_membership)
        create_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH"),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        window_id = create_response.data["window"]["id"]

        update_response = client.patch(
            _detail_url(group.id, group_slot.id, window_id),
            {"timezone": "America/Sao_Paulo"},
            format="json",
        )
        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        window_data = update_response.data["window"]
        assert window_data["is_recurring"] is True
        assert window_data["rrule_string"] == "FREQ=WEEKLY;BYDAY=TU,TH"

        reloaded = (
            AvailableTime.objects.unscoped()
            .filter_by_organization(calendar.organization_id)
            .get(id=window_id)
        )
        assert reloaded.recurrence_rule is not None
        assert reloaded.is_recurring is True


# ---------------------------------------------------------------------------
# Non-disclosure / visibility
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedAvailabilityWindowNonDisclosure:
    def test_stranger_cannot_list_or_create(
        self,
        stranger_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(stranger_membership)

        list_response = client.get(_list_url(group.id, group_slot.id))
        assert list_response.status_code == status.HTTP_404_NOT_FOUND

        create_response = client.post(
            _list_url(group.id, group_slot.id), _create_payload(calendar.id), format="json"
        )
        assert create_response.status_code == status.HTTP_404_NOT_FOUND

    def test_owner_of_other_calendar_in_same_group_denied_same_shape_as_stranger(
        self,
        other_owner_membership: OrganizationMembership,
        stranger_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
        other_slot: CalendarGroupSlot,
    ) -> None:
        """`other_owner_membership`'s user owns a calendar in the SAME group (a
        different slot), so they can see the group -- but they do not own
        `calendar`, so writing its window must still be denied, with the exact
        same not-found shape a genuine stranger gets."""
        other_owner_client = _auth_client(other_owner_membership)
        stranger_client = _auth_client(stranger_membership)

        other_owner_response = other_owner_client.post(
            _list_url(group.id, group_slot.id), _create_payload(calendar.id), format="json"
        )
        stranger_response = stranger_client.post(
            _list_url(group.id, group_slot.id), _create_payload(calendar.id), format="json"
        )

        assert other_owner_response.status_code == status.HTTP_404_NOT_FOUND
        assert stranger_response.status_code == status.HTTP_404_NOT_FOUND
        assert other_owner_response.data == stranger_response.data
        assert not AvailableTime.objects.unscoped().filter(group_slot_fk=group_slot).exists()

    def test_other_owner_can_see_and_manage_their_own_slot_in_the_group(
        self,
        other_owner_membership: OrganizationMembership,
        other_calendar: Calendar,
        group: CalendarGroup,
        other_slot: CalendarGroupSlot,
    ) -> None:
        """Sanity check for the fixture setup above: `other_owner_membership`'s
        user genuinely can manage `other_calendar`'s window in `other_slot` --
        the denial in the sibling test is calendar-specific, not group-wide."""
        client = _auth_client(other_owner_membership)
        response = client.post(
            _list_url(group.id, other_slot.id), _create_payload(other_calendar.id), format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data

    def test_cross_organization_access_denied(
        self,
        owner_membership: OrganizationMembership,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        other_org = baker.make(Organization)
        other_cal = Calendar.objects.create(
            organization=other_org,
            name="Other",
            external_id="other",
            provider=CalendarProvider.GOOGLE,
        )
        other_group = CalendarGroup.objects.create(organization=other_org, name="Other Group")
        other_org_slot = CalendarGroupSlot.objects.create(
            organization=other_org, group=other_group, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=other_org, slot=other_org_slot, calendar=other_cal
        )

        # `owner_membership`'s user has no membership in `other_org` at all.
        client = _auth_client(owner_membership)
        response = client.get(_list_url(other_group.id, other_org_slot.id))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_slot_not_belonging_to_group_in_url_is_not_found(
        self,
        owner_membership: OrganizationMembership,
        organization: Organization,
        calendar: Calendar,
        other_calendar: Calendar,
    ) -> None:
        """A slot that exists, but under a DIFFERENT group than the one named in
        the URL, must not resolve -- same not-found shape. The caller here
        genuinely has visibility into `group_b` (owns a calendar in ANOTHER of
        its slots), so the 404 must come from the group/slot mismatch branch
        in `GroupScopedAvailabilityWindowPermission.has_permission` -- not
        merely from the caller having no relationship to `group_b` at all."""
        group_a = CalendarGroup.objects.create(organization=organization, name="A")
        group_b = CalendarGroup.objects.create(organization=organization, name="B")
        slot_on_b = CalendarGroupSlot.objects.create(
            organization=organization, group=group_b, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot_on_b, calendar=other_calendar
        )
        # A second slot in `group_b`, owned by the caller -- gives them genuine
        # visibility into `group_b` without owning `slot_on_b`'s calendar.
        callers_slot_on_b = CalendarGroupSlot.objects.create(
            organization=organization, group=group_b, name="Caller's Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=callers_slot_on_b, calendar=calendar
        )

        client = _auth_client(owner_membership)
        # Sanity check: the caller genuinely sees `group_b` through their own slot.
        sanity_response = client.get(_list_url(group_b.id, callers_slot_on_b.id))
        assert sanity_response.status_code == status.HTTP_200_OK, sanity_response.data

        # `slot_on_b` belongs to `group_b`, not `group_a` -- naming `group_a` in
        # the URL must 404 even though the caller can see `group_b`.
        response = client.get(_list_url(group_a.id, slot_on_b.id))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unauthenticated_returns_401(
        self, group: CalendarGroup, group_slot: CalendarGroupSlot
    ) -> None:
        client = APIClient()
        response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Orphaned-booking warning
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedAvailabilityWindowOrphanedBookings:
    def test_create_first_window_reports_orphaned_booking(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC)
        thursday = _next_weekday(now, weekday=3)

        booking = CalendarEvent.objects.create(
            organization=calendar.organization,
            calendar=calendar,
            title="Operation",
            description="",
            external_id="ev_thursday_rest",
            start_time_tz_unaware=datetime.datetime.combine(
                thursday, datetime.time(18), tzinfo=datetime.UTC
            ),
            end_time_tz_unaware=datetime.datetime.combine(
                thursday, datetime.time(19), tzinfo=datetime.UTC
            ),
            timezone="UTC",
            calendar_group=group,
        )
        CalendarEventGroupSelection.objects.create(
            organization=calendar.organization,
            event=booking,
            slot=group_slot,
            calendar=calendar,
        )

        client = _auth_client(owner_membership)
        response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(
                calendar.id,
                start_time=datetime.datetime.combine(
                    thursday, datetime.time(9), tzinfo=datetime.UTC
                ).isoformat(),
                end_time=datetime.datetime.combine(
                    thursday, datetime.time(17), tzinfo=datetime.UTC
                ).isoformat(),
            ),
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        orphaned = response.data["orphaned_bookings"]
        assert len(orphaned) == 1
        assert orphaned[0]["id"] == booking.id
        assert orphaned[0]["calendar_id"] == calendar.id
        assert orphaned[0]["title"] == "Operation"

        # Nothing about the booking was touched.
        booking.refresh_from_db()
        assert booking.title == "Operation"

    def test_update_narrowing_reports_orphaned_booking(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC)
        tuesday = _next_weekday(now, weekday=1)
        thursday = tuesday + datetime.timedelta(days=2)

        client = _auth_client(owner_membership)
        create_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(
                calendar.id,
                start_time=datetime.datetime.combine(
                    tuesday, datetime.time(9), tzinfo=datetime.UTC
                ).isoformat(),
                end_time=datetime.datetime.combine(
                    tuesday, datetime.time(17), tzinfo=datetime.UTC
                ).isoformat(),
                rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
            ),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        window_id = create_response.data["window"]["id"]

        tuesday_event = CalendarEvent.objects.create(
            organization=calendar.organization,
            calendar=calendar,
            title="Tuesday Op",
            description="",
            external_id="ev_tuesday_rest",
            start_time_tz_unaware=datetime.datetime.combine(
                tuesday, datetime.time(10), tzinfo=datetime.UTC
            ),
            end_time_tz_unaware=datetime.datetime.combine(
                tuesday, datetime.time(11), tzinfo=datetime.UTC
            ),
            timezone="UTC",
            calendar_group=group,
        )
        CalendarEventGroupSelection.objects.create(
            organization=calendar.organization,
            event=tuesday_event,
            slot=group_slot,
            calendar=calendar,
        )
        thursday_event = CalendarEvent.objects.create(
            organization=calendar.organization,
            calendar=calendar,
            title="Thursday Op",
            description="",
            external_id="ev_thursday_rest2",
            start_time_tz_unaware=datetime.datetime.combine(
                thursday, datetime.time(10), tzinfo=datetime.UTC
            ),
            end_time_tz_unaware=datetime.datetime.combine(
                thursday, datetime.time(11), tzinfo=datetime.UTC
            ),
            timezone="UTC",
            calendar_group=group,
        )
        CalendarEventGroupSelection.objects.create(
            organization=calendar.organization,
            event=thursday_event,
            slot=group_slot,
            calendar=calendar,
        )

        update_response = client.patch(
            _detail_url(group.id, group_slot.id, window_id),
            {"rrule_string": "RRULE:FREQ=WEEKLY;BYDAY=TH"},
            format="json",
        )
        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        orphaned_ids = {b["id"] for b in update_response.data["orphaned_bookings"]}
        assert orphaned_ids == {tuesday_event.id}

        # Neither booking nor its group selection was touched.
        assert CalendarEvent.objects.filter_by_organization(calendar.organization_id).count() == 2
        assert (
            CalendarEventGroupSelection.objects.filter_by_organization(
                calendar.organization_id
            ).count()
            == 2
        )


# ---------------------------------------------------------------------------
# Query budget (bounded, must not scale with row count)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedAvailabilityWindowQueryBudget:
    def test_list_query_count_is_bounded_and_does_not_scale_with_window_count(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
        django_assert_max_num_queries,
    ) -> None:
        """`GroupScopedAvailabilityWindowSerializer` never nests `calendar` (it
        sources `calendar_id` straight from `calendar_fk_id`), so listing
        several windows must not eagerly pull in the general-purpose
        `CalendarVirtualModel` sub-graph (memberships/calendar_ownerships) --
        the query count must stay flat as the number of windows grows."""
        for i in range(4):
            AvailableTime.objects.unscoped().create(
                organization=calendar.organization,
                calendar=calendar,
                group_slot=group_slot,
                start_time_tz_unaware=_utc(2025, 9, 2 + i, 9),
                end_time_tz_unaware=_utc(2025, 9, 2 + i, 17),
                timezone="UTC",
            )

        client = _auth_client(owner_membership)
        # Generous margin over the observed ~6-7 queries -- what matters is
        # that this bound does NOT scale with the number of windows below.
        with django_assert_max_num_queries(12):
            response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 4

        for i in range(4, 8):
            AvailableTime.objects.unscoped().create(
                organization=calendar.organization,
                calendar=calendar,
                group_slot=group_slot,
                start_time_tz_unaware=_utc(2025, 9, 2 + i, 9),
                end_time_tz_unaware=_utc(2025, 9, 2 + i, 17),
                timezone="UTC",
            )

        with django_assert_max_num_queries(12):
            response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 8
