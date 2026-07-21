"""Phase 11: the reseller cascade -- Use-case 6 combined with the restricted
half of Use-case 5.

``resolve_billing_root`` already routes a reseller child to its root's
``Subscription`` (Phase 5), so a restricted root restricts the whole subtree
"by construction": every write guard and sync-pause site resolves
``EntitlementService.is_billing_root_restricted`` at the *root*, never at the
child asking the question. Nothing here re-implements the cascade -- these
tests exist to prove it holds, not to build it.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from webhooks.constants import WebhookEventType
from webhooks.services.webhook_service import WebhookService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def _reseller_tree(root_billing_state: str) -> tuple[Organization, Organization]:
    """A reseller root on ``root_billing_state`` (unlimited plan) plus one
    ordinary (non-billing-root) child pooling against it."""
    root = baker.make(Organization, parent=None, can_invite_organizations=True)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=root,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=root_billing_state,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    for resource_key in LimitedResource.values:
        kind = (
            LimitKind.POSTPAID
            if resource_key == LimitedResource.EVENT_OCCURRENCES
            else LimitKind.PREPAID
        )
        baker.make(
            SubscriptionPlanLimit,
            subscription=subscription,
            resource_key=resource_key,
            limit_value=None,
            kind=kind,
        )
    child = baker.make(Organization, parent=root, can_invite_organizations=False)
    return root, child


@pytest.mark.django_db
class TestReservedCascadeBlocksTheWholeSubtree:
    def test_child_resource_calendar_create_is_blocked_by_the_roots_restriction(self):
        _root, child = _reseller_tree(BillingState.RESTRICTED)
        service = CalendarService()
        service.initialize_without_provider(organization=child)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_resource_calendar(name="Blocked in child", description="")

        assert exc_info.value.remedy == "resolve_billing"
        assert not Calendar.objects.filter(organization=child).exists()

    def test_child_resource_calendar_update_is_blocked_by_the_roots_restriction(self):
        _root, child = _reseller_tree(BillingState.RESTRICTED)
        calendar = baker.make(
            Calendar,
            organization=child,
            calendar_type=CalendarType.RESOURCE,
            provider=CalendarProvider.INTERNAL,
            external_id="reseller-child-update",
        )
        service = CalendarService()
        service.initialize_without_provider(organization=child)

        with pytest.raises(OverLimitError):
            service.update_resource_calendar(calendar.id, name="Renamed in child")

    def test_child_resource_calendar_delete_is_blocked_by_the_roots_restriction(self):
        _root, child = _reseller_tree(BillingState.RESTRICTED)
        calendar = baker.make(
            Calendar,
            organization=child,
            calendar_type=CalendarType.RESOURCE,
            provider=CalendarProvider.INTERNAL,
            external_id="reseller-child-delete",
        )
        service = CalendarService()
        service.initialize_without_provider(organization=child)

        with pytest.raises(OverLimitError):
            service.disable_resource_calendar(calendar.id)

    def test_child_webhook_configuration_create_is_blocked_by_the_roots_restriction(self):
        _root, child = _reseller_tree(BillingState.RESTRICTED)

        with pytest.raises(OverLimitError) as exc_info:
            WebhookService().create_configuration(
                organization=child,
                event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
                url="https://example.com/reseller-child-blocked",
                headers={},
            )

        assert exc_info.value.remedy == "resolve_billing"

    def test_child_writes_are_unaffected_when_the_root_is_only_in_grace(self):
        """The cascade is specific to RESTRICTED -- a GRACE root must not
        restrict its subtree (Phase 10's inherited constraint, proven again
        here at the pooled-child level, not just at the root)."""
        _root, child = _reseller_tree(BillingState.GRACE)
        service = CalendarService()
        service.initialize_without_provider(organization=child)

        service.create_resource_calendar(name="Still allowed in child", description="")

        assert Calendar.objects.filter(organization=child, name="Still allowed in child").exists()

    def test_child_writes_are_unaffected_when_the_root_is_active(self):
        _root, child = _reseller_tree(BillingState.ACTIVE)
        service = CalendarService()
        service.initialize_without_provider(organization=child)

        service.create_resource_calendar(name="Active root allows this", description="")

        assert Calendar.objects.filter(organization=child, name="Active root allows this").exists()

    def test_is_billing_root_restricted_resolves_the_child_against_the_root(self):
        root, child = _reseller_tree(BillingState.RESTRICTED)

        assert EntitlementService().is_billing_root_restricted(child) is True
        assert EntitlementService().is_billing_root_restricted(root) is True

    def test_a_grandchild_two_levels_below_the_root_is_also_restricted(self):
        """The cascade holds through more than one level of nesting -- a
        grandchild pools against the same root a direct child does (unless it
        is itself a nested billing root, which ``resolve_billing_root`` stops
        at deliberately -- not exercised here, out of this test's scope)."""
        _root, child = _reseller_tree(BillingState.RESTRICTED)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)

        assert EntitlementService().is_billing_root_restricted(grandchild) is True

    def test_nested_reseller_root_pays_for_its_own_subtree_not_restricted_by_the_parent(self):
        """A nested reseller (``can_invite_organizations=True`` with a parent
        set) is its own billing root (``is_billing_root``) -- restricting the
        outer root must not restrict the nested reseller's own subtree, which
        pays (and is restricted) independently."""
        outer_root, _child = _reseller_tree(BillingState.RESTRICTED)
        nested_root = baker.make(Organization, parent=outer_root, can_invite_organizations=True)
        now = timezone.now()
        nested_subscription = baker.make(
            Subscription,
            organization=nested_root,
            plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
            billing_state=BillingState.ACTIVE,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )
        for resource_key in LimitedResource.values:
            kind = (
                LimitKind.POSTPAID
                if resource_key == LimitedResource.EVENT_OCCURRENCES
                else LimitKind.PREPAID
            )
            baker.make(
                SubscriptionPlanLimit,
                subscription=nested_subscription,
                resource_key=resource_key,
                limit_value=None,
                kind=kind,
            )
        nested_child = baker.make(Organization, parent=nested_root, can_invite_organizations=False)

        assert EntitlementService().is_billing_root_restricted(nested_root) is False
        assert EntitlementService().is_billing_root_restricted(nested_child) is False

        service = CalendarService()
        service.initialize_without_provider(organization=nested_child)
        service.create_resource_calendar(name="Nested reseller still open", description="")

        assert Calendar.objects.filter(
            organization=nested_child, name="Nested reseller still open"
        ).exists()
