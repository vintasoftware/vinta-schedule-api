"""``payments.seams.resources`` -- registration, and what each counter counts.

Written in Phase 0 as a parity suite: every test ran the new counter *and* its
``_count_*`` predecessor on ``payments.services.entitlement_service`` over the
same rows and asserted the two agreed. Phase 1 deleted those predecessors along
with the rest of the host engine, so the legacy half is gone -- but the tests
below never leaned on it alone. Each one already pinned the expected
``{organization_id: count}`` breakdown as a literal, built from rows the test
itself created, and that literal is what actually holds the counter to its
contract. Removing the comparison removed a second opinion, not the gate.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

import pytest
from freezegun import freeze_time
from model_bakery import baker
from vinta_billing.counting import UsageContext
from vinta_billing.models import BillingPlan, MeteredOccurrence, Subscription
from vinta_billing.registry import entitlements, resources
from vinta_billing.services.subscription_service import current_billing_period_start

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
from payments.seams.resource_keys import (
    AVAILABILITY_WINDOWS,
    BUNDLE_CALENDARS,
    CALENDAR_GROUPS,
    ENTITLEMENT_KEYS,
    EVENT_OCCURRENCES,
    ORGANIZATION_MEMBERS,
    PUBLIC_API_SYSTEM_USERS,
    RESOURCE_CALENDARS,
    RESOURCE_KEYS,
    WEBHOOK_SUBSCRIPTIONS,
)
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


# This module builds its own `Subscription` rows for the event-occurrences
# counter, so it opts out of conftest's autouse `provision_default_subscription`
# -- see `payments/tests/services/test_entitlement_service.py` for the same
# marker on the same grounds.
pytestmark = pytest.mark.no_auto_subscription


#: Pinned, byte-identical to the labels ``payments/seams/resources.py`` registers.
#: Used to be a comparison against ``LimitedResource`` / ``Entitlement`` -- see
#: ``payments/seams/resource_keys.py``'s docstring for why those enums are gone --
#: but a pinned literal catches the identical drift (a typo'd or retranslated
#: label) without needing a second side to compare against.
EXPECTED_RESOURCE_LABELS: dict[str, str] = {
    ORGANIZATION_MEMBERS: "Organization members",
    RESOURCE_CALENDARS: "Resource calendars",
    CALENDAR_GROUPS: "Calendar groups",
    BUNDLE_CALENDARS: "Bundle calendars",
    AVAILABILITY_WINDOWS: "Availability windows",
    WEBHOOK_SUBSCRIPTIONS: "Webhook subscriptions",
    PUBLIC_API_SYSTEM_USERS: "Public API system users",
    EVENT_OCCURRENCES: "Event occurrences",
}
EXPECTED_ENTITLEMENT_LABELS: dict[str, str] = {
    "external_calendar_google": "Google Calendar sync",
    "external_calendar_microsoft": "Microsoft Calendar sync",
    "partner_api": "Partner / public API access",
    "white_label_branding": "White-label branding",
    "advanced_scheduling": "Advanced scheduling",
}


class TestResourceAndEntitlementRegistration:
    """``payments.seams.resources`` writes its keys and labels as its own
    literals -- see ``payments/seams/resource_keys.py``'s docstring for why there
    is no enum to compare against any more. These tests pin the expected keys and
    labels as literals instead, so a typo'd key or a drifted label still fails
    here."""

    def test_all_eight_resources_register(self):
        assert {definition.key for definition in resources} == set(RESOURCE_KEYS)

    def test_resource_keys_match_the_pinned_set_exactly(self):
        assert sorted(resources.keys()) == sorted(RESOURCE_KEYS)

    def test_resource_labels_are_byte_identical_to_the_pinned_literals(self):
        for key, expected_label in EXPECTED_RESOURCE_LABELS.items():
            assert str(resources.get(key).label) == expected_label

    def test_all_five_entitlements_register(self):
        assert {definition.key for definition in entitlements} == set(ENTITLEMENT_KEYS)

    def test_entitlement_labels_are_byte_identical_to_the_pinned_literals(self):
        for key, expected_label in EXPECTED_ENTITLEMENT_LABELS.items():
            assert str(entitlements.get(key).label) == expected_label

    def test_event_occurrences_is_the_only_postpaid_resource(self):
        from vinta_billing.constants import LimitKind

        postpaid = {d.key for d in resources.of_kind(LimitKind.POSTPAID)}
        assert postpaid == {EVENT_OCCURRENCES}


@pytest.fixture
def organization_one():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def organization_two():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


#: Fixed, one-cycle-stale period for ``test_event_occurrences``'s subscriptions,
#: and the frozen "now" the test runs at -- one calendar month past that stored
#: period. Deliberately *not* anchored to real ``timezone.now()``: stamping the
#: subscription with whatever "now" happens to be and then immediately reading
#: ``current_billing_period_start`` at essentially the same instant can never
#: distinguish a regression that reads ``Subscription.current_period_start``
#: directly from the correct reconstruction -- the two coincide for a freshly
#: created row. Pinning the stored period one cycle behind the frozen "now"
#: forces ``current_billing_period_start`` to reconstruct a *different* period
#: than the stale column, which is exactly the disagreement
#: ``test_event_occurrences`` needs to catch that regression -- see that test's
#: docstring.
_EVENT_OCCURRENCES_STALE_PERIOD_START = datetime.datetime(2025, 7, 1, tzinfo=datetime.UTC)
_EVENT_OCCURRENCES_STALE_PERIOD_END = datetime.datetime(2025, 8, 1, tzinfo=datetime.UTC)
_EVENT_OCCURRENCES_NOW = datetime.datetime(2025, 8, 15, 12, 0, tzinfo=datetime.UTC)


def _make_subscription(organization: Organization) -> Subscription:
    """A subscription stamped with the fixed, one-cycle-stale period above --
    see that constant's docstring for why."""
    return baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        current_period_start=_EVENT_OCCURRENCES_STALE_PERIOD_START,
        current_period_end=_EVENT_OCCURRENCES_STALE_PERIOD_END,
    )


@pytest.mark.django_db
class TestCounterBreakdowns:
    """One test per resource: build rows for two organizations and pin the
    ``{organization_id: count}`` breakdown the registered counter returns."""

    @staticmethod
    def _breakdown(resource_key: str, organization_ids: list[int]) -> dict[int, int]:
        return resources.counter_for(resource_key)(UsageContext(organization_ids=organization_ids))

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

        breakdown = self._breakdown(
            ORGANIZATION_MEMBERS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 3, organization_two.pk: 1}

    def test_organization_members_excludes_the_named_invitation(
        self, organization_one, organization_two
    ):
        """``exclude_invitation_id`` travels through ``UsageContext.extra``, which
        the engine forwards without reading. Excluding the only pending
        invitation must make the accept path net zero."""
        invitation = baker.make(
            OrganizationInvitation,
            organization=organization_one,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        breakdown = resources.counter_for(ORGANIZATION_MEMBERS)(
            UsageContext(
                organization_ids=[organization_one.pk, organization_two.pk],
                extra={"exclude_invitation_id": invitation.pk},
            )
        )
        assert breakdown == {}

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

        resource_breakdown = self._breakdown(
            RESOURCE_CALENDARS, [organization_one.pk, organization_two.pk]
        )
        bundle_breakdown = self._breakdown(
            BUNDLE_CALENDARS, [organization_one.pk, organization_two.pk]
        )
        assert resource_breakdown == {organization_one.pk: 1}
        assert bundle_breakdown == {organization_two.pk: 1}

    def test_calendar_groups(self, organization_one, organization_two):
        baker.make(CalendarGroup, organization=organization_one, _quantity=2)
        baker.make(CalendarGroup, organization=organization_two)

        breakdown = self._breakdown(CALENDAR_GROUPS, [organization_one.pk, organization_two.pk])
        assert breakdown == {organization_one.pk: 2, organization_two.pk: 1}

    def test_availability_windows_merges_available_and_blocked_time(
        self, organization_one, organization_two
    ):
        baker.make(AvailableTime, organization=organization_one, timezone="UTC", _quantity=2)
        baker.make(BlockedTime, organization=organization_one, timezone="UTC")
        baker.make(AvailableTime, organization=organization_two, timezone="UTC")

        breakdown = self._breakdown(
            AVAILABILITY_WINDOWS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 3, organization_two.pk: 1}

    def test_webhook_subscriptions_excludes_soft_deleted(self, organization_one, organization_two):
        baker.make(WebhookConfiguration, organization=organization_one, deleted_at=None)
        baker.make(WebhookConfiguration, organization=organization_one, deleted_at=timezone.now())
        baker.make(
            WebhookConfiguration, organization=organization_two, deleted_at=None, _quantity=2
        )

        breakdown = self._breakdown(
            WEBHOOK_SUBSCRIPTIONS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 1, organization_two.pk: 2}

    def test_public_api_system_users(self, organization_one, organization_two):
        baker.make(SystemUser, organization=organization_one, is_active=True)
        baker.make(SystemUser, organization=organization_two, is_active=True, _quantity=2)

        breakdown = self._breakdown(
            PUBLIC_API_SYSTEM_USERS, [organization_one.pk, organization_two.pk]
        )
        assert breakdown == {organization_one.pk: 1, organization_two.pk: 2}

    def test_event_occurrences(self, organization_one, organization_two):
        """Frozen throughout: this test's own ``current_billing_period_start``
        call (used to stamp the seeded rows) and the counter's independent call
        (used to read them back) must resolve to the same period, or the test
        races the two against the wall clock -- see ``_EVENT_OCCURRENCES_NOW``'s
        docstring for why the subscriptions' stored period is also pinned stale
        rather than real-``now``-anchored, which is what keeps this test able to
        fail if the counter regresses to reading ``current_period_start`` off the
        row directly.
        """
        with freeze_time(_EVENT_OCCURRENCES_NOW):
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

            breakdown = resources.counter_for(EVENT_OCCURRENCES)(
                UsageContext(organization_ids=[organization_one.pk], subscription=subscription_one)
            )
            assert breakdown == {organization_one.pk: 2}

    def test_event_occurrences_with_no_subscription_is_empty(self):
        """Fail-open: a subscription-less pool is a broken invariant, and
        ``event_occurrences`` is post-paid, so under-reporting cannot block
        anybody."""
        breakdown = resources.counter_for(EVENT_OCCURRENCES)(
            UsageContext(organization_ids=[1], subscription=None)
        )
        assert breakdown == {}
