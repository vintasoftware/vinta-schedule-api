"""Closing acceptance check: every ``kind=prepaid`` ``LimitedResource`` member has
a guarded creation path.

This is the acceptance condition for spec objective 1 on pre-paid resources
("no unmetered creation path exists for any limited resource").

The set of prepaid resource keys is derived from the seeded ``unlimited`` plan's
own ``PlanLimit.kind`` classification -- the catalog's real data, not a hand-typed
Python set -- so a new ``LimitedResource`` member added later without a
registered probe fails this test loudly instead of silently passing. See
``payments/tests/test_plan_seed_migration.py`` for the seed migration guarantee
this relies on: every ``LimitedResource`` member has a row on ``unlimited``.

Each probe below drives the *real* guarded service method (not a hand-built
queryset assertion) against an organization sitting exactly at its ceiling, and
asserts both halves: the call raises ``OverLimitError`` naming the right
resource, and nothing was created.
"""

import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import CalendarGroupInputData
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from public_api.models import SystemUser
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration
from webhooks.services.webhook_service import WebhookService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


if TYPE_CHECKING:
    from di_core.containers import AppContainer


def _container() -> "AppContainer":
    """The wired DI container, narrowed for mypy.

    Imported inside the function body on purpose: ``di_core.containers.container`` is
    only *assigned* in ``DICoreConfig.ready()``, so a module-level ``from ... import
    container`` binds ``None`` forever. The root ``conftest.py``'s ``di_container``
    fixture defers the import for the same reason.
    """
    from di_core.containers import container

    assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
    return container


def _organization_with_limit(resource_key: str, limit_value: int) -> Organization:
    """A standalone (non-reseller) organization sitting exactly at ``limit_value``
    for ``resource_key`` -- one more of anything must be refused."""
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=resource_key,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    return organization


def _probe_organization_members() -> None:
    organization = _organization_with_limit(LimitedResource.ORGANIZATION_MEMBERS, 1)
    baker.make(OrganizationMembership, organization=organization, is_active=True)

    with pytest.raises(OverLimitError) as exc_info:
        _container().organization_service().invite_user_to_organization(
            email="blocked@example.com",
            first_name="Blocked",
            last_name="Invitee",
            organization=organization,
            send_email=False,
        )

    assert exc_info.value.resource_key == LimitedResource.ORGANIZATION_MEMBERS
    assert not OrganizationInvitation.objects.filter(
        organization=organization, email="blocked@example.com"
    ).exists()


def _probe_resource_calendars() -> None:
    organization = _organization_with_limit(LimitedResource.RESOURCE_CALENDARS, 1)
    baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        external_id="coverage-seed-resource",
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(OverLimitError) as exc_info:
        service.create_resource_calendar(name="Blocked Room")

    assert exc_info.value.resource_key == LimitedResource.RESOURCE_CALENDARS
    assert not Calendar.objects.filter(organization=organization, name="Blocked Room").exists()


def _probe_calendar_groups() -> None:
    organization = _organization_with_limit(LimitedResource.CALENDAR_GROUPS, 1)
    baker.make(CalendarGroup, organization=organization)

    service = CalendarGroupService()
    service.initialize(organization=organization)

    with pytest.raises(OverLimitError) as exc_info:
        service.create_group(CalendarGroupInputData(name="Blocked Group"))

    assert exc_info.value.resource_key == LimitedResource.CALENDAR_GROUPS
    assert not CalendarGroup.objects.filter(
        organization=organization, name="Blocked Group"
    ).exists()


def _probe_bundle_calendars() -> None:
    organization = _organization_with_limit(LimitedResource.BUNDLE_CALENDARS, 1)
    baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.BUNDLE,
        external_id="coverage-seed-bundle",
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(OverLimitError) as exc_info:
        service.create_bundle_calendar(name="Blocked Bundle")

    assert exc_info.value.resource_key == LimitedResource.BUNDLE_CALENDARS
    assert not Calendar.objects.filter(organization=organization, name="Blocked Bundle").exists()


def _probe_availability_windows() -> None:
    organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 1)
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
    )
    baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

    with pytest.raises(OverLimitError) as exc_info:
        service.create_available_time(
            calendar=calendar, start_time=start, end_time=end, timezone="UTC"
        )

    assert exc_info.value.resource_key == LimitedResource.AVAILABILITY_WINDOWS
    assert AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 1


def _probe_webhook_subscriptions() -> None:
    organization = _organization_with_limit(LimitedResource.WEBHOOK_SUBSCRIPTIONS, 1)
    baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/coverage-seed",
    )

    service = WebhookService()

    with pytest.raises(OverLimitError) as exc_info:
        service.create_configuration(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/coverage-blocked",
            headers={},
        )

    assert exc_info.value.resource_key == LimitedResource.WEBHOOK_SUBSCRIPTIONS
    assert not WebhookConfiguration.objects.filter(
        organization=organization, url="https://example.com/coverage-blocked"
    ).exists()


def _probe_public_api_system_users() -> None:
    organization = _organization_with_limit(LimitedResource.PUBLIC_API_SYSTEM_USERS, 1)
    baker.make(
        SystemUser,
        organization=organization,
        integration_name="coverage-seed-integration",
        long_lived_token_hash="coverage-seed-hash",
    )

    service = _container().public_api_auth_service()

    with pytest.raises(OverLimitError) as exc_info:
        service.create_system_user(
            integration_name="coverage-blocked-integration",
            organization=organization,
        )

    assert exc_info.value.resource_key == LimitedResource.PUBLIC_API_SYSTEM_USERS
    assert not SystemUser.objects.filter(
        organization=organization, integration_name="coverage-blocked-integration"
    ).exists()


# Every currently-known ``kind=prepaid`` ``LimitedResource`` member maps to a probe
# that drives its real guarded creation path. ``event_occurrences`` is the one
# ``LimitedResource`` member deliberately absent: it is ``kind=postpaid`` (a
# post-paid allowance, not a pre-paid ceiling), so it is correctly excluded below
# rather than missing by oversight -- the test asserts that distinction explicitly.
GUARDED_CREATION_PROBES = {
    LimitedResource.ORGANIZATION_MEMBERS: _probe_organization_members,
    LimitedResource.RESOURCE_CALENDARS: _probe_resource_calendars,
    LimitedResource.CALENDAR_GROUPS: _probe_calendar_groups,
    LimitedResource.BUNDLE_CALENDARS: _probe_bundle_calendars,
    LimitedResource.AVAILABILITY_WINDOWS: _probe_availability_windows,
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: _probe_webhook_subscriptions,
    LimitedResource.PUBLIC_API_SYSTEM_USERS: _probe_public_api_system_users,
}


@pytest.mark.django_db
class TestEveryPrepaidLimitedResourceHasAGuardedCreationPath:
    def _prepaid_resource_keys(self) -> set[str]:
        """The catalog's own classification, not a hand-typed literal: a new
        ``LimitedResource`` member seeded as ``kind=prepaid`` on ``unlimited``
        without a corresponding probe below fails this test loudly."""
        return set(
            BillingPlan.objects.get(slug="unlimited")
            .limits.filter(kind=LimitKind.PREPAID)
            .values_list("resource_key", flat=True)
        )

    def test_every_prepaid_resource_has_a_registered_probe(self):
        prepaid = self._prepaid_resource_keys()

        missing = prepaid - GUARDED_CREATION_PROBES.keys()
        assert not missing, (
            f"No guarded-creation-path probe registered for {sorted(missing)}. Every "
            "kind=prepaid LimitedResource member must have one registered in "
            "GUARDED_CREATION_PROBES -- this is the closing acceptance check for "
            "spec objective 1 on pre-paid resources."
        )

        # ...and the other direction, so the registry cannot rot the other way: a probe
        # left behind for a resource that was renamed, deleted, or flipped to postpaid
        # would otherwise keep passing while testing a path nothing routes through, and
        # the check above would still report full coverage.
        stale = GUARDED_CREATION_PROBES.keys() - prepaid
        assert not stale, (
            f"GUARDED_CREATION_PROBES has probes for {sorted(stale)}, which are not "
            "kind=prepaid LimitedResource members. Remove them, or fix the catalog if "
            "the resource was meant to stay pre-paid."
        )

    def test_event_occurrences_is_postpaid_not_prepaid(self):
        """Pins the one deliberate exclusion from ``GUARDED_CREATION_PROBES`` to a
        real assertion about the catalog, rather than leaving it as an unverified
        comment that could silently go stale."""
        assert LimitedResource.EVENT_OCCURRENCES not in self._prepaid_resource_keys()

    @pytest.mark.parametrize(
        "resource_key,probe",
        list(GUARDED_CREATION_PROBES.items()),
        ids=list(GUARDED_CREATION_PROBES.keys()),
    )
    def test_probe_blocks_at_the_ceiling(self, resource_key, probe):
        probe()
