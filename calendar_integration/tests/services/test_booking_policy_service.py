"""Unit tests for BookingPolicyService (Phase 2).

Covers:
- ``EffectivePolicy`` dataclass methods (``unconstrained``, ``from_model``,
  ``most_restrictive``).
- ``BookingPolicyService.resolve_for_calendar``: full precedence matrix.
- Owning-membership ambiguity (zero / one / multiple with is_default).
- ``resolve_for_bundle`` and ``resolve_for_group`` precedence.
- Create-uniqueness rejection (``DuplicateBookingPolicyError``).
- Update with field diffs.
- Delete-absent idempotent no-op.
- Audit records emitted on create / update / delete.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    DuplicateBookingPolicyError,
)
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    BookingPolicy,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.dataclasses import EffectivePolicy
from organizations.models import Organization, OrganizationMembership


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org() -> Organization:
    return baker.make(Organization)


_calendar_counter = 0


def _calendar(org: Organization, **extra) -> Calendar:
    """Create a Calendar with a unique external_id to avoid the unique constraint."""
    global _calendar_counter
    _calendar_counter += 1
    return baker.make(Calendar, organization=org, external_id=f"cal-{_calendar_counter}", **extra)


def _membership(org: Organization) -> int:
    """Create an OrganizationMembership and return its user_id (denormalized PK)."""
    from users.models import User

    user: User = baker.make(User)
    OrganizationMembership.objects.create(user=user, organization=org)
    return user.id


def _own(calendar: Calendar, membership_user_id: int, is_default: bool = False):
    """Create a CalendarOwnership row."""
    CalendarOwnership.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        membership_user_id=membership_user_id,
        is_default=is_default,
    )


def _service(org: Organization, *, audit_service=None) -> BookingPolicyService:
    svc = BookingPolicyService(audit_service=audit_service)
    svc.initialize(organization=org)
    return svc


# ---------------------------------------------------------------------------
# EffectivePolicy — unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestEffectivePolicyUnconstrained:
    def test_all_zero_or_none(self):
        ep = EffectivePolicy.unconstrained()
        assert ep.lead_time == datetime.timedelta(0)
        assert ep.max_horizon is None
        assert ep.buffer_before == datetime.timedelta(0)
        assert ep.buffer_after == datetime.timedelta(0)

    def test_frozen(self):
        ep = EffectivePolicy.unconstrained()
        with pytest.raises((AttributeError, TypeError)):
            ep.lead_time = datetime.timedelta(1)  # type: ignore[misc]


class TestEffectivePolicyFromModel:
    @pytest.mark.django_db
    def test_zero_horizon_maps_to_none(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(
            calendar=cal,
            lead_time_seconds=3600,
            max_horizon_seconds=0,
            buffer_before_seconds=300,
            buffer_after_seconds=600,
        )
        ep = EffectivePolicy.from_model(policy)
        assert ep.lead_time == datetime.timedelta(hours=1)
        assert ep.max_horizon is None
        assert ep.buffer_before == datetime.timedelta(seconds=300)
        assert ep.buffer_after == datetime.timedelta(seconds=600)

    @pytest.mark.django_db
    def test_nonzero_horizon_maps_to_timedelta(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(
            calendar=cal,
            max_horizon_seconds=86400 * 7,  # 7 days
        )
        ep = EffectivePolicy.from_model(policy)
        assert ep.max_horizon == datetime.timedelta(days=7)

    @pytest.mark.django_db
    def test_all_zero_fields_map_to_unconstrained(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal)
        ep = EffectivePolicy.from_model(policy)
        assert ep == EffectivePolicy.unconstrained()


class TestEffectivePolicyMostRestrictive:
    def test_empty_returns_unconstrained(self):
        result = EffectivePolicy.most_restrictive([])
        assert result == EffectivePolicy.unconstrained()

    def test_single_policy_returned_as_is(self):
        p = EffectivePolicy(
            lead_time=datetime.timedelta(hours=2),
            max_horizon=datetime.timedelta(days=14),
            buffer_before=datetime.timedelta(minutes=15),
            buffer_after=datetime.timedelta(minutes=30),
        )
        result = EffectivePolicy.most_restrictive([p])
        assert result == p

    def test_max_lead_time_wins(self):
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=1),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=3),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.lead_time == datetime.timedelta(hours=3)

    def test_min_positive_horizon_wins(self):
        """Shorter (min) horizon is more restrictive."""
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=datetime.timedelta(days=7),
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=datetime.timedelta(days=30),
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.max_horizon == datetime.timedelta(days=7)

    def test_none_horizon_among_finite_is_ignored(self):
        """None (unbounded) should be ignored; the finite horizon wins."""
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=None,  # unbounded
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=datetime.timedelta(days=14),
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.max_horizon == datetime.timedelta(days=14)

    def test_all_none_horizons_returns_none(self):
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=1),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=2),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.max_horizon is None

    def test_max_buffer_before_wins(self):
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=None,
            buffer_before=datetime.timedelta(minutes=10),
            buffer_after=datetime.timedelta(0),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=None,
            buffer_before=datetime.timedelta(minutes=30),
            buffer_after=datetime.timedelta(0),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.buffer_before == datetime.timedelta(minutes=30)

    def test_max_buffer_after_wins(self):
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(minutes=5),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(0),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(minutes=15),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.buffer_after == datetime.timedelta(minutes=15)

    def test_combination_of_all_fields(self):
        p1 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=2),
            max_horizon=datetime.timedelta(days=7),
            buffer_before=datetime.timedelta(minutes=15),
            buffer_after=datetime.timedelta(minutes=5),
        )
        p2 = EffectivePolicy(
            lead_time=datetime.timedelta(hours=1),
            max_horizon=datetime.timedelta(days=30),
            buffer_before=datetime.timedelta(minutes=30),
            buffer_after=datetime.timedelta(minutes=10),
        )
        result = EffectivePolicy.most_restrictive([p1, p2])
        assert result.lead_time == datetime.timedelta(hours=2)
        assert result.max_horizon == datetime.timedelta(days=7)
        assert result.buffer_before == datetime.timedelta(minutes=30)
        assert result.buffer_after == datetime.timedelta(minutes=10)


# ---------------------------------------------------------------------------
# BookingPolicyService — initialization guard
# ---------------------------------------------------------------------------


def test_assert_initialized_raises_when_no_org():
    svc = BookingPolicyService()
    with pytest.raises(CalendarServiceOrganizationNotSetError):
        svc._assert_initialized()


# ---------------------------------------------------------------------------
# resolve_for_calendar — precedence matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveForCalendar:
    def test_calendar_policy_returned_first(self):
        org = _org()
        cal = _calendar(org)
        create_booking_policy(calendar=cal, lead_time_seconds=1800)
        # Org default also exists — should be shadowed.
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=60)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=1800)

    def test_membership_policy_used_when_no_calendar_policy(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        create_booking_policy(membership_user_id=uid, organization=org, lead_time_seconds=900)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=900)

    def test_org_default_used_when_no_calendar_or_membership_policy(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        # No membership policy; org default only.
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=300)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=300)

    def test_unconstrained_when_no_policy_anywhere(self):
        org = _org()
        cal = _calendar(org)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result == EffectivePolicy.unconstrained()

    def test_calendar_policy_beats_membership_policy(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        create_booking_policy(calendar=cal, lead_time_seconds=100)
        create_booking_policy(membership_user_id=uid, organization=org, lead_time_seconds=9000)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=100)

    def test_membership_policy_beats_org_default(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        create_booking_policy(membership_user_id=uid, organization=org, lead_time_seconds=200)
        create_booking_policy(
            is_organization_default=True, organization=org, lead_time_seconds=9000
        )

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=200)

    def test_no_policy_for_calendar_falls_through_to_org_default_without_owner(self):
        """When a calendar has no owner, membership layer is skipped."""
        org = _org()
        cal = _calendar(org)
        # No ownership created.
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=500)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        assert result.lead_time == datetime.timedelta(seconds=500)


# ---------------------------------------------------------------------------
# Owning-membership ambiguity
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOwningMembershipResolution:
    def test_zero_non_null_ownerships_returns_none(self):
        org = _org()
        cal = _calendar(org)
        # No ownerships at all.
        svc = _service(org)
        result = svc._resolve_owning_membership_user_id(cal)
        assert result is None

    def test_single_ownership_returns_its_user_id(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid, is_default=False)

        svc = _service(org)
        result = svc._resolve_owning_membership_user_id(cal)
        assert result == uid

    def test_multiple_ownerships_returns_is_default_user_id(self):
        org = _org()
        cal = _calendar(org)
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(cal, uid_a, is_default=False)
        _own(cal, uid_b, is_default=True)

        svc = _service(org)
        result = svc._resolve_owning_membership_user_id(cal)
        assert result == uid_b

    def test_multiple_ownerships_no_is_default_returns_none(self):
        """When multiple ownerships exist but none is_default → skip membership layer."""
        org = _org()
        cal = _calendar(org)
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(cal, uid_a, is_default=False)
        _own(cal, uid_b, is_default=False)

        svc = _service(org)
        result = svc._resolve_owning_membership_user_id(cal)
        assert result is None

    def test_only_orphan_ownership_null_membership_user_id_returns_none(self):
        """Ownerships with NULL membership_user_id (orphans) are excluded."""
        org = _org()
        cal = _calendar(org)
        # Manually create an orphan ownership without a membership_user_id.
        CalendarOwnership.objects.create(
            organization=org,
            calendar=cal,
            membership_user_id=None,
            is_default=False,
        )

        svc = _service(org)
        result = svc._resolve_owning_membership_user_id(cal)
        assert result is None

    def test_membership_layer_skipped_when_multiple_owners_no_default(self):
        """Resolve for calendar with no is_default owner falls through to org default."""
        org = _org()
        cal = _calendar(org)
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(cal, uid_a, is_default=False)
        _own(cal, uid_b, is_default=False)
        # Membership policies for both — should not be used.
        create_booking_policy(membership_user_id=uid_a, organization=org, lead_time_seconds=1000)
        create_booking_policy(membership_user_id=uid_b, organization=org, lead_time_seconds=2000)
        # Org default.
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=50)

        svc = _service(org)
        result = svc.resolve_for_calendar(cal)

        # Neither membership policy should be chosen; org default wins.
        assert result.lead_time == datetime.timedelta(seconds=50)


# ---------------------------------------------------------------------------
# resolve_for_bundle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveForBundle:
    def _make_bundle_with_children(self, org: Organization, n: int):
        """Create a bundle calendar with n child calendars, return (bundle, [children])."""
        bundle = _calendar(org, calendar_type=CalendarType.BUNDLE)
        children = []
        for i in range(n):
            child = _calendar(org)
            ChildrenCalendarRelationship.objects.create(
                organization=org,
                bundle_calendar=bundle,
                child_calendar=child,
                is_primary=(i == 0),
            )
            children.append(child)
        return bundle, children

    def test_explicit_bundle_policy_overrides_everything(self):
        org = _org()
        bundle, children = self._make_bundle_with_children(org, 2)
        # Child policies are more restrictive — the bundle policy wins.
        create_booking_policy(calendar=bundle, lead_time_seconds=60)
        create_booking_policy(calendar=children[0], lead_time_seconds=9000)
        create_booking_policy(calendar=children[1], lead_time_seconds=7200)

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        assert result.lead_time == datetime.timedelta(seconds=60)

    def test_most_restrictive_across_children_when_no_bundle_policy(self):
        org = _org()
        bundle, children = self._make_bundle_with_children(org, 2)
        create_booking_policy(calendar=children[0], lead_time_seconds=1800)
        create_booking_policy(calendar=children[1], lead_time_seconds=3600)

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        # max(1800, 3600) = 3600
        assert result.lead_time == datetime.timedelta(seconds=3600)

    def test_unconstrained_when_no_child_policies(self):
        org = _org()
        bundle, _ = self._make_bundle_with_children(org, 2)

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        assert result == EffectivePolicy.unconstrained()

    def test_unconstrained_when_no_children(self):
        org = _org()
        bundle = _calendar(org, calendar_type=CalendarType.BUNDLE)
        # No children added.

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        assert result == EffectivePolicy.unconstrained()

    def test_most_restrictive_horizon_across_children(self):
        org = _org()
        bundle, children = self._make_bundle_with_children(org, 2)
        create_booking_policy(calendar=children[0], max_horizon_seconds=7 * 86400)  # 7 days
        create_booking_policy(calendar=children[1], max_horizon_seconds=30 * 86400)  # 30 days

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        # min(7d, 30d) = 7d
        assert result.max_horizon == datetime.timedelta(days=7)

    def test_child_inherits_org_default_if_no_direct_policy(self):
        """Children without direct policies resolve via their own chain (org default)."""
        org = _org()
        bundle, _children = self._make_bundle_with_children(org, 2)
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=120)
        # No direct child policies — both children inherit org default.

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        assert result.lead_time == datetime.timedelta(seconds=120)

    def test_bundle_unconstrained_when_children_have_owners_but_no_policies(self):
        """Bundle whose children have CalendarOwnership rows but no policy anywhere
        (no calendar/membership/group/bundle policy AND no org-default) →
        resolve_for_bundle returns EffectivePolicy.unconstrained().

        This exercises the ``combined != unconstrained()`` short-circuit's false branch
        distinct from the no-children path.
        """
        org = _org()
        bundle, children = self._make_bundle_with_children(org, 2)
        # Give each child an owner (CalendarOwnership) but no policy anywhere.
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(children[0], uid_a)
        _own(children[1], uid_b)
        # No BookingPolicy created for any target, no org default.

        svc = _service(org)
        result = svc.resolve_for_bundle(bundle)

        assert result == EffectivePolicy.unconstrained()


# ---------------------------------------------------------------------------
# resolve_for_group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveForGroup:
    def _make_group_with_calendars(self, org: Organization, calendar_count: int):
        """Create a CalendarGroup with one slot and ``calendar_count`` calendars."""
        group = baker.make(CalendarGroup, organization=org, name=f"Group-{id(org)}")
        slot = baker.make(CalendarGroupSlot, organization=org, group=group, name="Slot A")
        calendars = []
        for _ in range(calendar_count):
            cal = _calendar(org)
            CalendarGroupSlotMembership.objects.create(
                organization=org,
                slot=slot,
                calendar=cal,
            )
            calendars.append(cal)
        return group, calendars

    def test_explicit_group_policy_overrides_participants(self):
        org = _org()
        group, calendars = self._make_group_with_calendars(org, 2)
        create_booking_policy(calendar_group=group, lead_time_seconds=30)
        create_booking_policy(calendar=calendars[0], lead_time_seconds=9000)

        svc = _service(org)
        result = svc.resolve_for_group(group)

        assert result.lead_time == datetime.timedelta(seconds=30)

    def test_most_restrictive_across_participants_when_no_group_policy(self):
        org = _org()
        group, calendars = self._make_group_with_calendars(org, 2)
        create_booking_policy(calendar=calendars[0], lead_time_seconds=1800)
        create_booking_policy(calendar=calendars[1], buffer_before_seconds=600)

        svc = _service(org)
        result = svc.resolve_for_group(group)

        assert result.lead_time == datetime.timedelta(seconds=1800)
        assert result.buffer_before == datetime.timedelta(seconds=600)

    def test_unconstrained_when_no_participant_policies(self):
        org = _org()
        group, _ = self._make_group_with_calendars(org, 2)

        svc = _service(org)
        result = svc.resolve_for_group(group)

        assert result == EffectivePolicy.unconstrained()

    def test_unconstrained_when_no_participants(self):
        org = _org()
        group = baker.make(CalendarGroup, organization=org, name="Empty Group")

        svc = _service(org)
        result = svc.resolve_for_group(group)

        assert result == EffectivePolicy.unconstrained()

    def test_participants_from_multiple_slots_all_considered(self):
        """All calendars across all slots participate in the combination."""
        org = _org()
        group = baker.make(CalendarGroup, organization=org, name="Multi Slot Group")
        slot_a = baker.make(CalendarGroupSlot, organization=org, group=group, name="Slot A")
        slot_b = baker.make(CalendarGroupSlot, organization=org, group=group, name="Slot B")
        cal_a = _calendar(org)
        cal_b = _calendar(org)
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot_a, calendar=cal_a)
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot_b, calendar=cal_b)

        create_booking_policy(calendar=cal_a, lead_time_seconds=900)
        create_booking_policy(calendar=cal_b, max_horizon_seconds=14 * 86400)

        svc = _service(org)
        result = svc.resolve_for_group(group)

        assert result.lead_time == datetime.timedelta(seconds=900)
        assert result.max_horizon == datetime.timedelta(days=14)


# ---------------------------------------------------------------------------
# Write API — create_booking_policy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateBookingPolicy:
    def test_creates_calendar_policy(self):
        org = _org()
        cal = _calendar(org)
        svc = _service(org)

        policy = svc.create_booking_policy(calendar=cal, lead_time_seconds=600)

        assert policy.pk is not None
        assert policy.calendar_fk_id == cal.id
        assert policy.lead_time_seconds == 600
        assert policy.organization == org

    def test_raises_on_duplicate_calendar_policy(self):
        org = _org()
        cal = _calendar(org)
        create_booking_policy(calendar=cal)
        svc = _service(org)

        with pytest.raises(DuplicateBookingPolicyError, match="calendar"):
            svc.create_booking_policy(calendar=cal)

    def test_raises_on_duplicate_membership_policy(self):
        org = _org()
        uid = _membership(org)
        create_booking_policy(membership_user_id=uid, organization=org)
        svc = _service(org)

        with pytest.raises(DuplicateBookingPolicyError, match="membership"):
            svc.create_booking_policy(membership_user_id=uid)

    def test_raises_on_duplicate_group_policy(self):
        org = _org()
        group = baker.make(CalendarGroup, organization=org, name="G")
        create_booking_policy(calendar_group=group)
        svc = _service(org)

        with pytest.raises(DuplicateBookingPolicyError, match="calendar group"):
            svc.create_booking_policy(calendar_group=group)

    def test_raises_on_duplicate_org_default(self):
        org = _org()
        create_booking_policy(is_organization_default=True, organization=org)
        svc = _service(org)

        with pytest.raises(DuplicateBookingPolicyError, match="organization-default"):
            svc.create_booking_policy(is_organization_default=True)

    def test_raises_on_no_target(self):
        org = _org()
        svc = _service(org)
        with pytest.raises(ValueError):
            svc.create_booking_policy()

    def test_raises_on_multiple_targets(self):
        org = _org()
        cal = _calendar(org)
        svc = _service(org)
        with pytest.raises(ValueError):
            svc.create_booking_policy(
                calendar=cal,
                is_organization_default=True,
            )

    def test_requires_initialized(self):
        svc = BookingPolicyService()
        with pytest.raises(CalendarServiceOrganizationNotSetError):
            svc.create_booking_policy(is_organization_default=True)

    def test_raises_value_error_when_membership_user_id_not_in_org(self):
        """create_booking_policy raises ValueError when membership_user_id is not a member of the bound org."""
        org = _org()
        other_org = _org()
        uid = _membership(other_org)  # member of other_org, not org
        svc = _service(org)

        with pytest.raises(
            ValueError, match=r"No membership with this user id in your organization\."
        ):
            svc.create_booking_policy(membership_user_id=uid)

    def test_emits_audit_record_on_create(self):
        org = _org()
        cal = _calendar(org)
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)

        policy = svc.create_booking_policy(calendar=cal, lead_time_seconds=60)

        mock_audit.record.assert_called_once()
        call_kwargs = mock_audit.record.call_args.kwargs
        assert call_kwargs["action"] == "create"
        assert call_kwargs["organization_id"] == org.id
        # subject_from_instance was called with the newly created policy.
        mock_audit.subject_from_instance.assert_called_once_with(policy)


# ---------------------------------------------------------------------------
# Write API — update_booking_policy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateBookingPolicy:
    def test_updates_lead_time(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)
        svc = _service(org)

        updated = svc.update_booking_policy(policy, lead_time_seconds=1800)

        policy.refresh_from_db()
        assert policy.lead_time_seconds == 1800
        assert updated.lead_time_seconds == 1800

    def test_partial_update_leaves_other_fields_unchanged(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(
            calendar=cal,
            lead_time_seconds=300,
            buffer_before_seconds=120,
            max_horizon_seconds=86400,
        )
        svc = _service(org)

        svc.update_booking_policy(policy, buffer_before_seconds=240)

        policy.refresh_from_db()
        assert policy.lead_time_seconds == 300
        assert policy.buffer_before_seconds == 240
        assert policy.max_horizon_seconds == 86400

    def test_emits_audit_record_with_diff(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)

        svc.update_booking_policy(policy, lead_time_seconds=300)

        mock_audit.record.assert_called_once()
        call_kwargs = mock_audit.record.call_args.kwargs
        assert call_kwargs["action"] == "update"
        diff = call_kwargs["diff"]
        assert diff is not None
        assert "lead_time_seconds" in diff
        assert diff["lead_time_seconds"]["old"] == 60
        assert diff["lead_time_seconds"]["new"] == 300

    def test_audit_record_emitted_even_when_no_change(self):
        """An update with no field changes still emits an audit record (same before/after)."""
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)

        svc.update_booking_policy(policy)

        # The audit record is always emitted; compute_diff returns None for identical
        # before/after, so the diff kwarg must be None.
        mock_audit.record.assert_called_once()
        call_kwargs = mock_audit.record.call_args.kwargs
        assert call_kwargs["diff"] is None


# ---------------------------------------------------------------------------
# Write API — delete_booking_policy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteBookingPolicy:
    def test_deletes_existing_policy(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal)
        pk = policy.pk
        svc = _service(org)

        svc.delete_booking_policy(policy)

        assert not BookingPolicy.objects.filter_by_organization(org.id).filter(pk=pk).exists()

    def test_no_op_when_none(self):
        """Passing None is a safe, idempotent no-op."""
        org = _org()
        svc = _service(org)
        # Should not raise.
        svc.delete_booking_policy(None)

    def test_emits_audit_record_on_delete(self):
        org = _org()
        cal = _calendar(org)
        policy = create_booking_policy(calendar=cal)
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)

        svc.delete_booking_policy(policy)

        mock_audit.record.assert_called_once()
        call_kwargs = mock_audit.record.call_args.kwargs
        assert call_kwargs["action"] == "delete"

    def test_no_audit_record_when_none(self):
        org = _org()
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)

        svc.delete_booking_policy(None)

        mock_audit.record.assert_not_called()

    def test_convenience_delete_policy_for_calendar_no_op_when_absent(self):
        org = _org()
        cal = _calendar(org)
        svc = _service(org)
        # No policy exists — should not raise.
        svc.delete_policy_for_calendar(cal)

    def test_convenience_delete_org_default_no_op_when_absent(self):
        org = _org()
        svc = _service(org)
        svc.delete_org_default_policy()  # No policy — no-op.

    def test_delete_policy_for_membership_noop_when_absent(self):
        """delete_policy_for_membership is a no-op and emits no audit record when absent."""
        org = _org()
        uid = _membership(org)
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)
        # No policy for this membership — should not raise.
        svc.delete_policy_for_membership(uid)
        mock_audit.record.assert_not_called()

    def test_delete_policy_for_group_noop_when_absent(self):
        """delete_policy_for_group is a no-op and emits no audit record when absent."""
        org = _org()
        group = baker.make(CalendarGroup, organization=org, name="G-noop")
        mock_audit = MagicMock()
        svc = _service(org, audit_service=mock_audit)
        # No policy for this group — should not raise.
        svc.delete_policy_for_group(group)
        mock_audit.record.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cross_org_isolation():
    """A policy in org_b is invisible when the service is initialized with org_a."""
    org_a = _org()
    org_b = _org()
    cal_a = _calendar(org_a)
    cal_b = _calendar(org_b)
    create_booking_policy(calendar=cal_b, lead_time_seconds=9999)

    svc = _service(org_a)
    result = svc.resolve_for_calendar(cal_a)

    assert result == EffectivePolicy.unconstrained()


@pytest.mark.django_db
def test_org_default_from_other_org_not_used():
    org_a = _org()
    org_b = _org()
    cal_a = _calendar(org_a)
    create_booking_policy(is_organization_default=True, organization=org_b, lead_time_seconds=9999)

    svc = _service(org_a)
    result = svc.resolve_for_calendar(cal_a)

    assert result == EffectivePolicy.unconstrained()


# ---------------------------------------------------------------------------
# DB-annotation equivalence — the resolution moved into SQL must produce the
# EXACT EffectivePolicy the original Python precedence rules prescribe.
# ---------------------------------------------------------------------------


def _ep(lead=0, horizon=None, before=0, after=0) -> EffectivePolicy:
    """Shorthand to build an expected EffectivePolicy from second-counts."""
    return EffectivePolicy(
        lead_time=datetime.timedelta(seconds=lead),
        max_horizon=(datetime.timedelta(seconds=horizon) if horizon else None),
        buffer_before=datetime.timedelta(seconds=before),
        buffer_after=datetime.timedelta(seconds=after),
    )


@pytest.mark.django_db
class TestResolveForCalendarAnnotationEquivalence:
    """Each scenario asserts the DB-annotated resolution equals the hand-computed
    EffectivePolicy the documented precedence prescribes."""

    def test_calendar_layer_whole_policy(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        # Calendar policy wins as a WHOLE — even fields it leaves at 0/unbounded
        # are taken from it, never merged with the membership/org layers.
        create_booking_policy(
            calendar=cal,
            lead_time_seconds=120,
            max_horizon_seconds=0,  # unbounded — must stay unbounded
            buffer_before_seconds=0,
            buffer_after_seconds=45,
        )
        create_booking_policy(
            membership_user_id=uid,
            organization=org,
            lead_time_seconds=9000,
            max_horizon_seconds=86400,
            buffer_before_seconds=600,
            buffer_after_seconds=600,
        )
        create_booking_policy(
            is_organization_default=True,
            organization=org,
            max_horizon_seconds=999,
            buffer_before_seconds=999,
        )

        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=120, horizon=None, before=0, after=45)

    def test_membership_layer_whole_policy(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid)
        create_booking_policy(
            membership_user_id=uid,
            organization=org,
            lead_time_seconds=300,
            max_horizon_seconds=0,
            buffer_before_seconds=15,
            buffer_after_seconds=0,
        )
        create_booking_policy(
            is_organization_default=True,
            organization=org,
            lead_time_seconds=9999,
            max_horizon_seconds=7,
        )
        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=300, horizon=None, before=15, after=0)

    def test_org_default_layer_whole_policy(self):
        org = _org()
        cal = _calendar(org)
        create_booking_policy(
            is_organization_default=True,
            organization=org,
            lead_time_seconds=60,
            max_horizon_seconds=3600,
            buffer_before_seconds=30,
            buffer_after_seconds=90,
        )
        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=60, horizon=3600, before=30, after=90)

    def test_no_policy_layer_unconstrained(self):
        org = _org()
        cal = _calendar(org)
        result = _service(org).resolve_for_calendar(cal)
        assert result == EffectivePolicy.unconstrained()

    def test_membership_zero_owners_skips_to_org_default(self):
        org = _org()
        cal = _calendar(org)
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=7)
        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=7)

    def test_membership_single_owner_used(self):
        org = _org()
        cal = _calendar(org)
        uid = _membership(org)
        _own(cal, uid, is_default=False)
        create_booking_policy(membership_user_id=uid, organization=org, lead_time_seconds=88)
        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=88)

    def test_membership_multiple_owners_default_used(self):
        org = _org()
        cal = _calendar(org)
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(cal, uid_a, is_default=False)
        _own(cal, uid_b, is_default=True)
        create_booking_policy(membership_user_id=uid_a, organization=org, lead_time_seconds=111)
        create_booking_policy(membership_user_id=uid_b, organization=org, lead_time_seconds=222)
        result = _service(org).resolve_for_calendar(cal)
        # The is_default owner (uid_b) drives resolution.
        assert result == _ep(lead=222)

    def test_membership_multiple_owners_no_default_skips_layer(self):
        org = _org()
        cal = _calendar(org)
        uid_a = _membership(org)
        uid_b = _membership(org)
        _own(cal, uid_a, is_default=False)
        _own(cal, uid_b, is_default=False)
        create_booking_policy(membership_user_id=uid_a, organization=org, lead_time_seconds=111)
        create_booking_policy(membership_user_id=uid_b, organization=org, lead_time_seconds=222)
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=9)
        result = _service(org).resolve_for_calendar(cal)
        # No is_default owner → membership layer skipped → org default wins.
        assert result == _ep(lead=9)

    def test_orphan_ownership_excluded(self):
        org = _org()
        cal = _calendar(org)
        CalendarOwnership.objects.create(
            organization=org, calendar=cal, membership_user_id=None, is_default=False
        )
        create_booking_policy(is_organization_default=True, organization=org, lead_time_seconds=5)
        result = _service(org).resolve_for_calendar(cal)
        assert result == _ep(lead=5)


@pytest.mark.django_db
class TestResolveForGroupAnnotationEquivalence:
    def _group_with_calendars(self, org: Organization, n: int):
        group = baker.make(CalendarGroup, organization=org, name=f"G-{id(org)}-{n}")
        slot = baker.make(CalendarGroupSlot, organization=org, group=group, name="S")
        calendars = []
        for _ in range(n):
            cal = _calendar(org)
            CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal)
            calendars.append(cal)
        return group, calendars

    def test_explicit_group_policy_whole(self):
        org = _org()
        group, cals = self._group_with_calendars(org, 2)
        # Group policy with an unbounded horizon must stay unbounded even though
        # participants have finite horizons (whole-policy short-circuit).
        create_booking_policy(
            calendar_group=group,
            lead_time_seconds=30,
            max_horizon_seconds=0,
            buffer_before_seconds=5,
            buffer_after_seconds=0,
        )
        create_booking_policy(calendar=cals[0], max_horizon_seconds=3600, lead_time_seconds=9000)
        result = _service(org).resolve_for_group(group)
        assert result == _ep(lead=30, horizon=None, before=5, after=0)

    def test_most_restrictive_field_by_field(self):
        org = _org()
        group, cals = self._group_with_calendars(org, 2)
        create_booking_policy(
            calendar=cals[0],
            lead_time_seconds=7200,
            max_horizon_seconds=7 * 86400,
            buffer_before_seconds=900,
            buffer_after_seconds=300,
        )
        create_booking_policy(
            calendar=cals[1],
            lead_time_seconds=3600,
            max_horizon_seconds=30 * 86400,
            buffer_before_seconds=1800,
            buffer_after_seconds=600,
        )
        result = _service(org).resolve_for_group(group)
        # max lead, min horizon, max before, max after.
        assert result == _ep(lead=7200, horizon=7 * 86400, before=1800, after=600)

    def test_all_unbounded_horizons_stay_unbounded(self):
        org = _org()
        group, cals = self._group_with_calendars(org, 2)
        create_booking_policy(calendar=cals[0], lead_time_seconds=60, max_horizon_seconds=0)
        create_booking_policy(calendar=cals[1], lead_time_seconds=120, max_horizon_seconds=0)
        result = _service(org).resolve_for_group(group)
        assert result == _ep(lead=120, horizon=None)

    def test_mixed_bounded_and_unbounded_horizon_takes_finite_min(self):
        org = _org()
        group, cals = self._group_with_calendars(org, 2)
        create_booking_policy(calendar=cals[0], max_horizon_seconds=0)  # unbounded
        create_booking_policy(calendar=cals[1], max_horizon_seconds=14 * 86400)
        result = _service(org).resolve_for_group(group)
        assert result == _ep(horizon=14 * 86400)

    def test_no_participant_policies_unconstrained(self):
        org = _org()
        group, _ = self._group_with_calendars(org, 3)
        result = _service(org).resolve_for_group(group)
        assert result == EffectivePolicy.unconstrained()

    def test_participant_resolved_through_own_chain(self):
        """A participant with no direct policy but an owner+membership policy
        contributes that membership policy to the combination."""
        org = _org()
        group, cals = self._group_with_calendars(org, 2)
        uid = _membership(org)
        _own(cals[0], uid)
        create_booking_policy(membership_user_id=uid, organization=org, lead_time_seconds=4500)
        create_booking_policy(calendar=cals[1], lead_time_seconds=600)
        result = _service(org).resolve_for_group(group)
        assert result == _ep(lead=4500)


@pytest.mark.django_db
class TestResolveForBundleAnnotationEquivalence:
    def _bundle(self, org: Organization, n: int):
        bundle = _calendar(org, calendar_type=CalendarType.BUNDLE)
        children = []
        for i in range(n):
            child = _calendar(org)
            ChildrenCalendarRelationship.objects.create(
                organization=org,
                bundle_calendar=bundle,
                child_calendar=child,
                is_primary=(i == 0),
            )
            children.append(child)
        return bundle, children

    def test_explicit_bundle_policy_whole(self):
        org = _org()
        bundle, children = self._bundle(org, 2)
        create_booking_policy(
            calendar=bundle,
            lead_time_seconds=15,
            max_horizon_seconds=0,
            buffer_before_seconds=0,
            buffer_after_seconds=5,
        )
        create_booking_policy(
            calendar=children[0], max_horizon_seconds=3600, lead_time_seconds=9999
        )
        result = _service(org).resolve_for_bundle(bundle)
        assert result == _ep(lead=15, horizon=None, before=0, after=5)

    def test_children_most_restrictive_field_by_field(self):
        org = _org()
        bundle, children = self._bundle(org, 2)
        create_booking_policy(
            calendar=children[0],
            lead_time_seconds=1800,
            max_horizon_seconds=10 * 86400,
            buffer_before_seconds=120,
            buffer_after_seconds=900,
        )
        create_booking_policy(
            calendar=children[1],
            lead_time_seconds=3600,
            max_horizon_seconds=5 * 86400,
            buffer_before_seconds=60,
            buffer_after_seconds=60,
        )
        result = _service(org).resolve_for_bundle(bundle)
        assert result == _ep(lead=3600, horizon=5 * 86400, before=120, after=900)

    def test_children_all_unbounded_horizon(self):
        org = _org()
        bundle, children = self._bundle(org, 2)
        create_booking_policy(calendar=children[0], lead_time_seconds=1, max_horizon_seconds=0)
        create_booking_policy(calendar=children[1], lead_time_seconds=2, max_horizon_seconds=0)
        result = _service(org).resolve_for_bundle(bundle)
        assert result == _ep(lead=2, horizon=None)


# ---------------------------------------------------------------------------
# Query-count: bundle/group resolution must be bounded regardless of the number
# of participant calendars — the N+1 walk collapsed into a bounded set of
# queries.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolutionQueryCount:
    def test_group_resolution_bounded_regardless_of_participant_count(
        self, django_assert_num_queries
    ):
        org = _org()
        group = baker.make(CalendarGroup, organization=org, name="Big Group")
        slot = baker.make(CalendarGroupSlot, organization=org, group=group, name="S")
        for _ in range(8):
            cal = _calendar(org)
            CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal)
            create_booking_policy(calendar=cal, lead_time_seconds=300)

        svc = _service(org)
        # Group resolution is a single annotated SELECT — it does NOT scale with
        # the participant count.
        with django_assert_num_queries(1):
            svc.resolve_for_group(group)

    def test_group_resolution_same_query_count_for_more_participants(
        self, django_assert_num_queries
    ):
        """Resolution for a 2-participant and a 12-participant group issues the
        same (constant) number of queries."""
        org = _org()

        def _build(n: int) -> CalendarGroup:
            group = baker.make(CalendarGroup, organization=org, name=f"QC-{n}")
            slot = baker.make(CalendarGroupSlot, organization=org, group=group, name="S")
            for _ in range(n):
                cal = _calendar(org)
                CalendarGroupSlotMembership.objects.create(
                    organization=org, slot=slot, calendar=cal
                )
                create_booking_policy(calendar=cal, max_horizon_seconds=86400)
            return group

        small = _build(2)
        large = _build(12)
        svc = _service(org)
        with django_assert_num_queries(1):
            svc.resolve_for_group(small)
        with django_assert_num_queries(1):
            svc.resolve_for_group(large)

    def test_bundle_resolution_bounded(self, django_assert_num_queries):
        org = _org()
        bundle = _calendar(org, calendar_type=CalendarType.BUNDLE)
        for i in range(8):
            child = _calendar(org)
            ChildrenCalendarRelationship.objects.create(
                organization=org,
                bundle_calendar=bundle,
                child_calendar=child,
                is_primary=(i == 0),
            )
            create_booking_policy(calendar=child, lead_time_seconds=600)

        svc = _service(org)
        # Bundle resolution: (1) explicit-bundle-policy lookup, (2) child-id
        # lookup, (3) the single annotated children SELECT — a small constant
        # independent of the child count.
        with django_assert_num_queries(3):
            svc.resolve_for_bundle(bundle)


# ---------------------------------------------------------------------------
# Org-scope: a second organization with conflicting parallel fixtures must not
# influence resolution for the first organization.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolutionOrgScope:
    def test_calendar_resolution_ignores_other_org(self):
        org_a = _org()
        org_b = _org()
        cal_a = _calendar(org_a)
        uid_a = _membership(org_a)
        _own(cal_a, uid_a)
        create_booking_policy(membership_user_id=uid_a, organization=org_a, lead_time_seconds=300)

        # Parallel, conflicting fixtures in org_b — a calendar policy AND an
        # org-default with very different numbers.
        cal_b = _calendar(org_b)
        create_booking_policy(calendar=cal_b, lead_time_seconds=99999)
        create_booking_policy(is_organization_default=True, organization=org_b, lead_time_seconds=1)

        result = _service(org_a).resolve_for_calendar(cal_a)
        assert result == _ep(lead=300)

    def test_group_resolution_ignores_other_org(self):
        org_a = _org()
        org_b = _org()

        group_a = baker.make(CalendarGroup, organization=org_a, name="GA")
        slot_a = baker.make(CalendarGroupSlot, organization=org_a, group=group_a, name="S")
        ca1 = _calendar(org_a)
        ca2 = _calendar(org_a)
        CalendarGroupSlotMembership.objects.create(organization=org_a, slot=slot_a, calendar=ca1)
        CalendarGroupSlotMembership.objects.create(organization=org_a, slot=slot_a, calendar=ca2)
        create_booking_policy(calendar=ca1, lead_time_seconds=600)
        create_booking_policy(calendar=ca2, lead_time_seconds=1200)

        # org_b: an explicit group-default and conflicting calendar policies.
        group_b = baker.make(CalendarGroup, organization=org_b, name="GB")
        slot_b = baker.make(CalendarGroupSlot, organization=org_b, group=group_b, name="S")
        cb1 = _calendar(org_b)
        CalendarGroupSlotMembership.objects.create(organization=org_b, slot=slot_b, calendar=cb1)
        create_booking_policy(calendar_group=group_b, lead_time_seconds=99999)
        create_booking_policy(calendar=cb1, lead_time_seconds=77777)
        create_booking_policy(is_organization_default=True, organization=org_b, lead_time_seconds=1)

        result = _service(org_a).resolve_for_group(group_a)
        # max(600, 1200) = 1200, untouched by org_b.
        assert result == _ep(lead=1200)

    def test_bundle_resolution_ignores_other_org(self):
        org_a = _org()
        org_b = _org()
        bundle_a = _calendar(org_a, calendar_type=CalendarType.BUNDLE)
        child_a = _calendar(org_a)
        ChildrenCalendarRelationship.objects.create(
            organization=org_a, bundle_calendar=bundle_a, child_calendar=child_a, is_primary=True
        )
        create_booking_policy(calendar=child_a, lead_time_seconds=450)

        # org_b parallel bundle with a different number + org default.
        create_booking_policy(is_organization_default=True, organization=org_b, lead_time_seconds=1)

        result = _service(org_a).resolve_for_bundle(bundle_a)
        assert result == _ep(lead=450)
