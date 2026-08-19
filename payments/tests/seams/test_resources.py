"""``payments.seams.resources`` -- registration, and counter parity against
the pre-migration ``_count_*`` functions still living on
``payments.services.entitlement_service``.

Phase 0 does not delete those predecessors -- this is the regression proof
that rewriting each counter against ``vinta_billing.counting`` changed
nothing observable. Both the new and the old counter run against the same
rows in every test below.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

import pytest
from model_bakery import baker
from vinta_billing.counting import UsageContext
from vinta_billing.registry import entitlements, resources

# Registration already happens at process start: ``di_core``'s DI wiring
# (``DICoreConfig.ready()``) imports every submodule under ``payments``,
# including ``payments.seams.resources``, before any test runs. This explicit
# import is a deliberate guarantee that does not rely on that wiring, so the
# test does not silently depend on DI internals. Re-registering an identical
# definition is a no-op (``Registry._add``), which makes this redundant import
# safe.
import payments.seams.resources  # noqa: F401,E402
from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.billing_constants import Entitlement, LimitedResource
from payments.models import BillingPlan, MeteredOccurrence, Subscription
from payments.services.entitlement_service import USAGE_COUNTERS
from payments.services.entitlement_service import UsageContext as LegacyUsageContext
from payments.services.subscription_service import current_billing_period_start
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


# This module builds its own `Subscription` rows for the event-occurrences
# counter, so it opts out of conftest's autouse `provision_default_subscription`
# -- see `payments/tests/services/test_entitlement_service.py` for the same
# marker on the same grounds.
pytestmark = pytest.mark.no_auto_subscription


class TestResourceAndEntitlementRegistration:
    """``payments.seams.resources`` now writes its keys and labels as its own
    literals, independent of ``LimitedResource`` / ``Entitlement`` -- see that
    module's docstring for why. These tests are the bridge: they compare the
    seam's literals against the enum they replace, so a typo'd key or a label
    that drifts from the enum's translation fails here, for as long as the
    enum still exists to compare against."""

    def test_all_eight_resources_register(self):
        assert {definition.key for definition in resources} == set(LimitedResource.values)

    def test_resource_keys_match_limited_resource_values_exactly(self):
        assert sorted(resources.keys()) == sorted(LimitedResource.values)

    def test_resource_labels_are_byte_identical_to_the_textchoices_members(self):
        for member in LimitedResource:
            assert resources.get(member.value).label == member.label

    def test_all_five_entitlements_register(self):
        assert {definition.key for definition in entitlements} == set(Entitlement.values)

    def test_entitlement_labels_are_byte_identical_to_the_textchoices_members(self):
        for member in Entitlement:
            assert entitlements.get(member.value).label == member.label

    def test_event_occurrences_is_the_only_postpaid_resource(self):
        from vinta_billing.constants import LimitKind

        postpaid = {d.key for d in resources.of_kind(LimitKind.POSTPAID)}
        assert postpaid == {LimitedResource.EVENT_OCCURRENCES}


@pytest.fixture
def organization_one():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def organization_two():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


def _make_subscription(organization: Organization) -> Subscription:
    now = timezone.now()
    return baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )


@pytest.mark.django_db
class TestCounterParity:
    """One test per resource: build the same rows for two organizations, and
    assert the new counter (``vinta_billing.counting.UsageContext``) and the
    old one (``payments.services.entitlement_service.UsageContext``) return
    the identical ``{organization_id: count}`` breakdown."""

    @staticmethod
    def _assert_parity(
        resource_key: str, organization_ids: list[int], **legacy_extra: object
    ) -> dict[int, int]:
        new_breakdown = resources.counter_for(resource_key)(
            UsageContext(organization_ids=organization_ids)
        )
        legacy_breakdown = USAGE_COUNTERS[resource_key](
            LegacyUsageContext(organization_ids=organization_ids, **legacy_extra)  # type: ignore[arg-type]
        )
        assert new_breakdown == legacy_breakdown
        return new_breakdown

    def test_organization_members(self, organization_one, organization_two):
        baker.make(
            OrganizationMembership, organization=organization_one, is_active=True, _quantity=2
        )
        baker.make(OrganizationMembership, organization=organization_two, is_active=True)
        baker.make(
            OrganizationInvitation,
            organization=organization_one,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        breakdown = self._assert_parity(
            LimitedResource.ORGANIZATION_MEMBERS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 3, organization_two.pk: 1}

    def test_organization_members_excludes_the_named_invitation(
        self, organization_one, organization_two
    ):
        """``exclude_invitation_id`` travels through ``UsageContext.extra`` on the
        new side and through the dedicated keyword on the old side -- both must
        agree it makes the accept path net zero."""
        invitation = baker.make(
            OrganizationInvitation,
            organization=organization_one,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        new_breakdown = resources.counter_for(LimitedResource.ORGANIZATION_MEMBERS)(
            UsageContext(
                organization_ids=[organization_one.pk, organization_two.pk],
                extra={"exclude_invitation_id": invitation.pk},
            )
        )
        legacy_breakdown = USAGE_COUNTERS[LimitedResource.ORGANIZATION_MEMBERS](
            LegacyUsageContext(
                organization_ids=[organization_one.pk, organization_two.pk],
                exclude_invitation_id=invitation.pk,
            )
        )
        assert new_breakdown == legacy_breakdown == {}

    def test_resource_and_bundle_calendars(self, organization_one, organization_two):
        baker.make(
            Calendar,
            organization=organization_one,
            calendar_type=CalendarType.RESOURCE,
            external_id="one-resource",
        )
        baker.make(
            Calendar,
            organization=organization_two,
            calendar_type=CalendarType.BUNDLE,
            external_id="two-bundle",
        )

        resource_breakdown = self._assert_parity(
            LimitedResource.RESOURCE_CALENDARS, [organization_one.pk, organization_two.pk]
        )
        bundle_breakdown = self._assert_parity(
            LimitedResource.BUNDLE_CALENDARS, [organization_one.pk, organization_two.pk]
        )
        assert resource_breakdown == {organization_one.pk: 1}
        assert bundle_breakdown == {organization_two.pk: 1}

    def test_calendar_groups(self, organization_one, organization_two):
        baker.make(CalendarGroup, organization=organization_one, _quantity=2)
        baker.make(CalendarGroup, organization=organization_two)

        breakdown = self._assert_parity(
            LimitedResource.CALENDAR_GROUPS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 2, organization_two.pk: 1}

    def test_availability_windows_merges_available_and_blocked_time(
        self, organization_one, organization_two
    ):
        baker.make(AvailableTime, organization=organization_one, timezone="UTC", _quantity=2)
        baker.make(BlockedTime, organization=organization_one, timezone="UTC")
        baker.make(AvailableTime, organization=organization_two, timezone="UTC")

        breakdown = self._assert_parity(
            LimitedResource.AVAILABILITY_WINDOWS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 3, organization_two.pk: 1}

    def test_webhook_subscriptions_excludes_soft_deleted(self, organization_one, organization_two):
        baker.make(WebhookConfiguration, organization=organization_one, deleted_at=None)
        baker.make(WebhookConfiguration, organization=organization_one, deleted_at=timezone.now())
        baker.make(
            WebhookConfiguration, organization=organization_two, deleted_at=None, _quantity=2
        )

        breakdown = self._assert_parity(
            LimitedResource.WEBHOOK_SUBSCRIPTIONS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 1, organization_two.pk: 2}

    def test_public_api_system_users(self, organization_one, organization_two):
        baker.make(SystemUser, organization=organization_one, is_active=True)
        baker.make(SystemUser, organization=organization_two, is_active=True, _quantity=2)

        breakdown = self._assert_parity(
            LimitedResource.PUBLIC_API_SYSTEM_USERS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 1, organization_two.pk: 2}

    def test_event_occurrences(self, organization_one, organization_two):
        subscription_one = _make_subscription(organization_one)
        subscription_two = _make_subscription(organization_two)
        period_one = current_billing_period_start(subscription_one)
        period_two = current_billing_period_start(subscription_two)
        now = timezone.now()
        baker.make(
            MeteredOccurrence,
            organization=organization_one,
            subscription=subscription_one,
            event_id=1,
            occurrence_start=now,
            billing_period_start=period_one,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )
        baker.make(
            MeteredOccurrence,
            organization=organization_one,
            subscription=subscription_one,
            event_id=2,
            occurrence_start=now,
            billing_period_start=period_one,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )
        baker.make(
            MeteredOccurrence,
            organization=organization_two,
            subscription=subscription_two,
            event_id=3,
            occurrence_start=now,
            billing_period_start=period_two,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )

        new_breakdown = resources.counter_for(LimitedResource.EVENT_OCCURRENCES)(
            UsageContext(organization_ids=[organization_one.pk], subscription=subscription_one)
        )
        legacy_breakdown = USAGE_COUNTERS[LimitedResource.EVENT_OCCURRENCES](
            LegacyUsageContext(
                organization_ids=[organization_one.pk], subscription=subscription_one
            )
        )
        assert new_breakdown == legacy_breakdown == {organization_one.pk: 2}

    def test_event_occurrences_with_no_subscription_is_empty(self):
        """Fail-open: a subscription-less pool is a broken invariant, and
        ``event_occurrences`` is post-paid, so under-reporting cannot block
        anybody."""
        new_breakdown = resources.counter_for(LimitedResource.EVENT_OCCURRENCES)(
            UsageContext(organization_ids=[1], subscription=None)
        )
        legacy_breakdown = USAGE_COUNTERS[LimitedResource.EVENT_OCCURRENCES](
            LegacyUsageContext(organization_ids=[1], subscription=None)
        )
        assert new_breakdown == legacy_breakdown == {}
