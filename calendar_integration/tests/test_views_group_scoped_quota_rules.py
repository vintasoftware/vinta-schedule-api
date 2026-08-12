"""Integration tests for the internal REST surface exposing group-scoped
quota rules (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3c).

Direct mirror of ``test_views_group_scoped_blocked_times.py`` for quota
rules, minus the recurrence/orphaned-booking machinery (quota rules are
non-recurring and never narrow already-confirmed bookings). Covers:
- Full lifecycle through the nested routes (create -> list -> retrieve ->
  update -> delete), for both the calendar's owner and an org admin.
- Multiple rules per (calendar, slot) -- day and week coexisting.
- The (calendar, slot, period) uniqueness constraint surfaced as a 400
  validation error, never an unhandled IntegrityError/500.
- Route-level group-visibility gating: a stranger and the owner of a
  *different* calendar in the same group (different slot) are both denied
  with the exact same not-found shape.
- Cross-organization access denied with the same not-found shape.
- No entitlement check gates quota-rule creation (unmetered).
"""

from __future__ import annotations

import datetime

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType, QuotaPeriod
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
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


def _list_url(group_id: int, slot_id: int) -> str:
    return reverse(
        "api:GroupScopedQuotaRules-list",
        kwargs={"group_id": group_id, "slot_id": slot_id},
    )


def _detail_url(group_id: int, slot_id: int, pk: int) -> str:
    return reverse(
        "api:GroupScopedQuotaRules-detail",
        kwargs={"group_id": group_id, "slot_id": slot_id, "pk": pk},
    )


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="Quota REST Test Org")


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
        external_id="dr_reyes_quota",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Costa",
        external_id="dr_costa_quota",
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
        "period": QuotaPeriod.WEEK,
        "cap": 3,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedQuotaRuleLifecycle:
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
            _create_payload(calendar.id),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
        rule_data = create_response.data
        assert rule_data["calendar_id"] == calendar.id
        assert rule_data["group_slot_id"] == group_slot.id
        assert rule_data["period"] == QuotaPeriod.WEEK
        assert rule_data["cap"] == 3
        rule_id = rule_data["id"]

        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(calendar.organization_id)
            .filter(id=rule_id)
            .exists()
        )

        # List.
        list_response = client.get(_list_url(group.id, group_slot.id))
        assert list_response.status_code == status.HTTP_200_OK
        ids = [r["id"] for r in list_response.data["results"]]
        assert ids == [rule_id]

        # Retrieve.
        retrieve_response = client.get(_detail_url(group.id, group_slot.id, rule_id))
        assert retrieve_response.status_code == status.HTTP_200_OK
        assert retrieve_response.data["id"] == rule_id

        # Update (raise the cap).
        update_response = client.patch(
            _detail_url(group.id, group_slot.id, rule_id),
            {"cap": 5},
            format="json",
        )
        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        assert update_response.data["cap"] == 5
        assert update_response.data["period"] == QuotaPeriod.WEEK

        # Delete.
        delete_response = client.delete(_detail_url(group.id, group_slot.id, rule_id))
        assert delete_response.status_code == status.HTTP_204_NO_CONTENT
        assert not CalendarGroupSlotQuotaRule.original_manager.filter(id=rule_id).exists()

        # Now invisible everywhere, including the group-scoped read path.
        retrieve_after_delete = client.get(_detail_url(group.id, group_slot.id, rule_id))
        assert retrieve_after_delete.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_can_manage_any_calendars_quota_rule(
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
        rule_id = create_response.data["id"]

        update_response = client.patch(
            _detail_url(group.id, group_slot.id, rule_id), {"cap": 10}, format="json"
        )
        assert update_response.status_code == status.HTTP_200_OK
        assert update_response.data["cap"] == 10

        delete_response = client.delete(_detail_url(group.id, group_slot.id, rule_id))
        assert delete_response.status_code == status.HTTP_204_NO_CONTENT

    def test_create_requires_positive_cap(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)
        response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, cap=0),
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_with_invalid_period(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        """A create with an invalid period value (not in QuotaPeriod.values)
        must be rejected as a 400 validation error, nothing created."""
        client = _auth_client(owner_membership)
        response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period="invalid"),
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

        # Nothing was created.
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(
                calendar.organization_id
            ).count()
            == 0
        )

    def test_patch_requires_positive_cap(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        """Mirrors test_create_requires_positive_cap: a PATCH setting cap=0
        or cap < 1 must be rejected as a 400 validation error."""
        client = _auth_client(owner_membership)

        # Create a rule first.
        create_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, cap=3),
            format="json",
        )
        assert create_response.status_code == status.HTTP_201_CREATED
        rule_id = create_response.data["id"]

        # Try to PATCH it with cap=0.
        patch_response = client.patch(
            _detail_url(group.id, group_slot.id, rule_id),
            {"cap": 0},
            format="json",
        )
        assert patch_response.status_code == status.HTTP_400_BAD_REQUEST

        # The rule's cap should remain unchanged.
        rule = CalendarGroupSlotQuotaRule.objects.filter_by_organization(
            calendar.organization_id
        ).get(id=rule_id)
        assert rule.cap == 3

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
        rule_id = create_response.data["id"]

        response = client.put(
            _detail_url(group.id, group_slot.id, rule_id),
            {"cap": 5},
            format="json",
        )
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_day_and_week_rules_coexist_for_same_calendar_and_slot(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        """Multiple rules per (calendar, slot) are allowed and ALL must pass
        -- e.g. "at most 1 a day AND 3 a week" -- as long as each names a
        DIFFERENT period."""
        client = _auth_client(owner_membership)

        day_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.DAY, cap=1),
            format="json",
        )
        assert day_response.status_code == status.HTTP_201_CREATED, day_response.data

        week_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.WEEK, cap=3),
            format="json",
        )
        assert week_response.status_code == status.HTTP_201_CREATED, week_response.data

        list_response = client.get(_list_url(group.id, group_slot.id))
        assert list_response.status_code == status.HTTP_200_OK
        periods = {r["period"] for r in list_response.data["results"]}
        assert periods == {QuotaPeriod.DAY, QuotaPeriod.WEEK}


# ---------------------------------------------------------------------------
# Uniqueness -> validation error, not a server error
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedQuotaRuleUniqueness:
    def test_create_duplicate_period_for_same_calendar_and_slot_is_validation_error(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)

        first_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.WEEK, cap=3),
            format="json",
        )
        assert first_response.status_code == status.HTTP_201_CREATED, first_response.data

        second_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.WEEK, cap=5),
            format="json",
        )
        assert second_response.status_code == status.HTTP_400_BAD_REQUEST, second_response.data
        assert "non_field_errors" in second_response.data

        # Nothing extra was written -- exactly the one rule from the first create.
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(
                calendar.organization_id
            ).count()
            == 1
        )

    def test_update_colliding_with_existing_period_is_validation_error(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
    ) -> None:
        client = _auth_client(owner_membership)

        day_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.DAY, cap=1),
            format="json",
        )
        assert day_response.status_code == status.HTTP_201_CREATED, day_response.data
        day_rule_id = day_response.data["id"]

        week_response = client.post(
            _list_url(group.id, group_slot.id),
            _create_payload(calendar.id, period=QuotaPeriod.WEEK, cap=3),
            format="json",
        )
        assert week_response.status_code == status.HTTP_201_CREATED, week_response.data

        # Updating the DAY rule to WEEK collides with the existing WEEK rule.
        update_response = client.patch(
            _detail_url(group.id, group_slot.id, day_rule_id),
            {"period": QuotaPeriod.WEEK},
            format="json",
        )
        assert update_response.status_code == status.HTTP_400_BAD_REQUEST, update_response.data
        assert "non_field_errors" in update_response.data

        # The DAY rule kept its original period -- the failed update did not
        # partially apply.
        reloaded = CalendarGroupSlotQuotaRule.objects.filter_by_organization(
            calendar.organization_id
        ).get(id=day_rule_id)
        assert reloaded.period == QuotaPeriod.DAY


# ---------------------------------------------------------------------------
# Non-disclosure / visibility
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedQuotaRuleNonDisclosure:
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
        assert not CalendarGroupSlotQuotaRule.original_manager.filter(
            group_slot_fk=group_slot
        ).exists()

    def test_other_owner_can_see_and_manage_their_own_slot_in_the_group(
        self,
        other_owner_membership: OrganizationMembership,
        other_calendar: Calendar,
        group: CalendarGroup,
        other_slot: CalendarGroupSlot,
    ) -> None:
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
            external_id="other_quota",
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
        group_a = CalendarGroup.objects.create(organization=organization, name="A")
        group_b = CalendarGroup.objects.create(organization=organization, name="B")
        slot_on_b = CalendarGroupSlot.objects.create(
            organization=organization, group=group_b, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot_on_b, calendar=other_calendar
        )
        callers_slot_on_b = CalendarGroupSlot.objects.create(
            organization=organization, group=group_b, name="Caller's Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=callers_slot_on_b, calendar=calendar
        )

        client = _auth_client(owner_membership)
        sanity_response = client.get(_list_url(group_b.id, callers_slot_on_b.id))
        assert sanity_response.status_code == status.HTTP_200_OK, sanity_response.data

        response = client.get(_list_url(group_a.id, slot_on_b.id))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unauthenticated_returns_401(
        self, group: CalendarGroup, group_slot: CalendarGroupSlot
    ) -> None:
        client = APIClient()
        response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Query budget (bounded, must not scale with row count)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedQuotaRuleQueryBudget:
    def test_list_query_count_is_bounded_and_does_not_scale_with_rule_count(
        self,
        owner_membership: OrganizationMembership,
        calendar: Calendar,
        group: CalendarGroup,
        group_slot: CalendarGroupSlot,
        django_assert_max_num_queries,
    ) -> None:
        """`GroupScopedQuotaRuleSerializer` never nests `calendar` (it sources
        `calendar_id` straight from `calendar_fk_id`), so listing several
        rules must not eagerly pull in the general-purpose
        `CalendarVirtualModel` sub-graph -- the query count must stay flat as
        the number of rules grows. Row count is grown by adding more
        calendars to the SAME slot's roster (each with one quota rule) rather
        than more periods, since the model's (calendar, slot, period) unique
        constraint caps periods at three per calendar."""

        def _add_calendar_with_rule(index: int) -> None:
            extra_calendar = Calendar.objects.create(
                organization=calendar.organization,
                name=f"Dr. Extra {index}",
                external_id=f"dr_extra_quota_{index}",
                provider=CalendarProvider.GOOGLE,
                calendar_type=CalendarType.PERSONAL,
            )
            CalendarGroupSlotMembership.objects.create(
                organization=calendar.organization, slot=group_slot, calendar=extra_calendar
            )
            CalendarGroupSlotQuotaRule.objects.create(
                organization=calendar.organization,
                group_slot=group_slot,
                calendar=extra_calendar,
                period=QuotaPeriod.WEEK,
                cap=1,
            )

        for i in range(4):
            _add_calendar_with_rule(i)

        client = _auth_client(owner_membership)
        # Generous margin over the observed query count -- what matters is
        # that this bound does NOT scale with the number of rules below.
        with django_assert_max_num_queries(12):
            response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 4

        for i in range(4, 8):
            _add_calendar_with_rule(i)

        with django_assert_max_num_queries(12):
            response = client.get(_list_url(group.id, group_slot.id))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 8
