"""Phase 11: the RESTRICTED write guard.

Spec use-case 5 (restricted half): a restricted organization cannot write --
independent of whether the resource in question is even at its numeric
ceiling -- while its data stays fully readable and it can still pay its way
out. This module proves the single predicate behind that,
``EntitlementService.is_billing_root_restricted``, resolved at the billing
root and consulted by every guarded create/update/delete path:

- Writes are blocked for a ``RESTRICTED`` organization: create, update, *and*
  delete, across a representative slice of the guarded resource surface
  (prepaid and postpaid alike).
- ``GRACE`` is never blocked -- only ``RESTRICTED`` write-blocks (the Phase 10
  inherited constraint).
- A missing subscription is not restricted -- absence of billing is not the
  same thing as a resolved "this org is restricted" answer.
- Reads and every ``/billing/`` endpoint stay open for a restricted org.

Every probe below drives the *real* guarded service method (not a hand-built
assertion against ``is_billing_root_restricted`` in isolation) against an
organization that is nowhere near its numeric ceiling, so a probe that only
happened to trip the ordinary limit guard would not pass here by accident.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarEvent, CalendarGroup
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import CalendarEventInputData, CalendarGroupInputData
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.billing_constants import BillingInterval, BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, PlanLimit, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService
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
    from di_core.containers import container

    assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
    return container


def _organization_with_billing_state(billing_state: str, *, unlimited: bool = True) -> Organization:
    """A standalone (non-reseller) organization on ``billing_state``, with
    every ``LimitedResource`` unlimited (NULL) unless ``unlimited=False``.

    Unlimited on purpose: a probe that blocked only because it also happened
    to be at its numeric ceiling would not prove the restricted-state guard
    specifically -- ``check_limit``/``check_postpaid_allowance`` must block a
    RESTRICTED organization even when nothing about capacity would.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=billing_state,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    if unlimited:
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
    return organization


@dataclass
class WriteProbe:
    """A guarded resource's create/update/delete entry points, each a bare
    ``Callable[[Organization], None]`` that performs its own setup (seeding
    prerequisite rows directly via ``baker.make``, never through another
    guarded path, so a probe's own setup can never itself be blocked) and then
    drives the real guarded method."""

    create: Callable[[Organization], None]
    update: Callable[[Organization], None] | None = None
    delete: Callable[[Organization], None] | None = None


def _invite_member(organization: Organization) -> None:
    _container().organization_service().invite_user_to_organization(
        email=f"invitee-{organization.pk}@example.com",
        first_name="Blocked",
        last_name="Invitee",
        organization=organization,
        send_email=False,
    )


def _reactivate_member(organization: Organization) -> None:
    from users.factories import UserFactory

    membership = baker.make(
        OrganizationMembership,
        organization=organization,
        user=UserFactory().create_user(),
        is_active=False,
    )
    _container().organization_service().reactivate_membership(membership)


def _revoke_invitation(organization: Organization) -> None:
    invitation = baker.make(
        OrganizationInvitation,
        organization=organization,
        email=f"revoke-{organization.pk}@example.com",
        expires_at=timezone.now() + datetime.timedelta(days=7),
    )
    _container().organization_service().revoke_invitation(str(invitation.id))


def _create_resource_calendar(organization: Organization) -> None:
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    # description="" (not the default None): Calendar.description is NOT NULL
    # at the DB level (TextField(blank=True) is a form-validation relaxation,
    # not a schema one) -- unrelated to this phase, but this is the first test
    # in the suite that lets this specific call reach a real INSERT rather than
    # being blocked before it (every existing coverage probe for this method
    # blocks at the ceiling first).
    service.create_resource_calendar(name="Blocked Room", description="")


def _update_resource_calendar(organization: Organization) -> None:
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        provider=CalendarProvider.INTERNAL,
        external_id="restricted-update-resource",
    )
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    service.update_resource_calendar(calendar.id, name="Renamed")


def _disable_resource_calendar(organization: Organization) -> None:
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        provider=CalendarProvider.INTERNAL,
        external_id="restricted-delete-resource",
    )
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    service.disable_resource_calendar(calendar.id)


def _create_calendar_group(organization: Organization) -> None:
    service = CalendarGroupService()
    service.initialize(organization=organization)
    service.create_group(CalendarGroupInputData(name="Blocked Group"))


def _update_calendar_group(organization: Organization) -> None:
    group = baker.make(CalendarGroup, organization=organization)
    service = CalendarGroupService()
    service.initialize(organization=organization)
    service.update_group(group.id, CalendarGroupInputData(name="Renamed Group"))


def _delete_calendar_group(organization: Organization) -> None:
    group = baker.make(CalendarGroup, organization=organization)
    service = CalendarGroupService()
    service.initialize(organization=organization)
    service.delete_group(group.id)


def _create_webhook_configuration(organization: Organization) -> None:
    WebhookService().create_configuration(
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/restricted-create",
        headers={},
    )


def _update_webhook_configuration(organization: Organization) -> None:
    configuration = baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/restricted-seed",
    )
    WebhookService().update_configuration(
        configuration,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/restricted-updated",
        headers={},
    )


def _delete_webhook_configuration(organization: Organization) -> None:
    configuration = baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/restricted-delete-seed",
    )
    WebhookService().delete_configuration(configuration)


def _bookable_calendar(organization: Organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.PERSONAL,
        provider=CalendarProvider.INTERNAL,
        accepts_public_scheduling=True,
        manage_available_windows=False,
    )


def _event_input(start: datetime.datetime, minutes: int = 60) -> CalendarEventInputData:
    return CalendarEventInputData(
        title="Restricted-guard event",
        description="",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _create_event(organization: Organization) -> None:
    calendar = _bookable_calendar(organization)
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    service.create_event(
        calendar.id,
        _event_input(timezone.now() + datetime.timedelta(days=1)),
    )


def _org_wide_system_user(organization: Organization) -> SystemUser:
    """An org-wide public-API token (``scoped_to_membership_user_id=None``) --
    the update/delete probes use this as ``user_or_token`` rather than a real
    ``User``, since a real ``User`` would need a per-event
    ``CalendarManagementToken`` (normally granted by ``create_event``, which
    these probes deliberately bypass by seeding the event directly via
    ``baker.make``) to pass ``can_perform_update``'s permission check. An
    org-wide token is authorized unconditionally within its own organization
    (``CalendarEventService._public_token_may_write``), so it reaches the
    restricted guard cleanly without needing that token machinery."""
    return baker.make(
        SystemUser,
        organization=organization,
        integration_name=f"restricted-guard-{organization.pk}",
        long_lived_token_hash="restricted-guard-hash",
    )


def _update_event(organization: Organization) -> None:
    calendar = _bookable_calendar(organization)
    start = timezone.now() + datetime.timedelta(days=1)
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(hours=1),
        timezone="UTC",
    )
    service = CalendarService()
    service.initialize_without_provider(
        organization=organization, user_or_token=_org_wide_system_user(organization)
    )
    service.update_event(calendar.id, event.id, _event_input(start + datetime.timedelta(hours=2)))


def _delete_event(organization: Organization) -> None:
    calendar = _bookable_calendar(organization)
    start = timezone.now() + datetime.timedelta(days=1)
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(hours=1),
        timezone="UTC",
    )
    service = CalendarService()
    service.initialize_without_provider(
        organization=organization, user_or_token=_org_wide_system_user(organization)
    )
    service.delete_event(calendar.id, event.id)


def _create_available_time(organization: Organization) -> None:
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
    )
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    service.create_available_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2030, 1, 1, 17, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )


def _delete_only_available_time_batch(organization: Organization) -> None:
    """A pure delete-only ``batch_modify_available_times`` call -- the one
    availability-window write ``create_available_time``'s guard does not cover
    (its underlying ``check_limit`` call is skipped entirely when the batch's
    net growth is zero or negative), proven separately here."""
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
    )
    available_time = baker.make(
        AvailableTime, organization=organization, calendar=calendar, timezone="UTC"
    )
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    service.batch_modify_available_times(
        calendar=calendar, operations=[{"action": "delete", "id": available_time.id}]
    )


#: Registered probes for a representative slice of the guarded write surface:
#: every ``kind=prepaid`` resource ``test_prepaid_resource_coverage.py`` covers
#: for creation, plus the postpaid ``event_occurrences`` resource, each with an
#: update and/or delete probe where the guarded service exposes one. Not every
#: single write method across the codebase is enumerated here (see the phase
#: report for the drawn scope boundary) -- this proves the *pattern* holds
#: across prepaid, postpaid, and org-membership resources alike, at both
#: create and update/delete.
RESTRICTED_WRITE_PROBES: dict[str, WriteProbe] = {
    LimitedResource.ORGANIZATION_MEMBERS: WriteProbe(
        create=_invite_member, update=_reactivate_member, delete=_revoke_invitation
    ),
    LimitedResource.RESOURCE_CALENDARS: WriteProbe(
        create=_create_resource_calendar,
        update=_update_resource_calendar,
        delete=_disable_resource_calendar,
    ),
    LimitedResource.CALENDAR_GROUPS: WriteProbe(
        create=_create_calendar_group,
        update=_update_calendar_group,
        delete=_delete_calendar_group,
    ),
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: WriteProbe(
        create=_create_webhook_configuration,
        update=_update_webhook_configuration,
        delete=_delete_webhook_configuration,
    ),
    LimitedResource.AVAILABILITY_WINDOWS: WriteProbe(
        create=_create_available_time, delete=_delete_only_available_time_batch
    ),
    LimitedResource.EVENT_OCCURRENCES: WriteProbe(
        create=_create_event, update=_update_event, delete=_delete_event
    ),
}


def _probe_ids() -> list[str]:
    ids = []
    for resource_key, probe in RESTRICTED_WRITE_PROBES.items():
        ids.append(f"{resource_key}-create")
        if probe.update is not None:
            ids.append(f"{resource_key}-update")
        if probe.delete is not None:
            ids.append(f"{resource_key}-delete")
    return ids


def _probe_params() -> list[tuple[str, Callable[[Organization], None]]]:
    params = []
    for resource_key, probe in RESTRICTED_WRITE_PROBES.items():
        params.append((resource_key, probe.create))
        if probe.update is not None:
            params.append((resource_key, probe.update))
        if probe.delete is not None:
            params.append((resource_key, probe.delete))
    return params


@pytest.mark.django_db
class TestRestrictedOrganizationBlocksEveryWrite:
    @pytest.mark.parametrize("resource_key,action", _probe_params(), ids=_probe_ids())
    def test_restricted_blocks(self, resource_key, action):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)

        with pytest.raises(OverLimitError) as exc_info:
            action(organization)

        assert exc_info.value.remedy == "resolve_billing"

    @pytest.mark.parametrize("resource_key,action", _probe_params(), ids=_probe_ids())
    def test_active_is_unaffected(self, resource_key, action):
        """An ``ACTIVE`` organization (unlimited plan) sees byte-for-byte
        pre-Phase-11 behavior -- the rollout's own "no organization is blocked
        as a consequence of the rollout itself" rule, applied to this phase."""
        organization = _organization_with_billing_state(BillingState.ACTIVE)

        action(organization)  # must not raise

    @pytest.mark.parametrize("resource_key,action", _probe_params(), ids=_probe_ids())
    def test_grace_is_unaffected(self, resource_key, action):
        """Phase 10's inherited constraint: ``GRACE`` is never write-blocked --
        only ``RESTRICTED`` is. A ``GRACE`` organization with an unlimited plan
        keeps writing normally; escalation is the dunning ladder, not a write
        block."""
        organization = _organization_with_billing_state(BillingState.GRACE)

        action(organization)  # must not raise


@pytest.mark.django_db
class TestIsBillingRootRestricted:
    def test_missing_subscription_is_not_restricted(self):
        """Absence of billing is not the same thing as being restricted --
        conflating the two would lock out every organization with a broken
        billing invariant, which the fail-open convention this service
        otherwise follows forbids."""
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert EntitlementService().is_billing_root_restricted(organization) is False
        # And a guarded create actually goes through, not just the predicate:
        _create_resource_calendar(organization)

    def test_reseller_child_resolves_the_roots_state(self):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        now = timezone.now()
        baker.make(
            Subscription,
            organization=root,
            plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
            billing_state=BillingState.RESTRICTED,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )

        assert EntitlementService().is_billing_root_restricted(child) is True


@pytest.mark.django_db
class TestRestrictedOrganizationReadsStayOpen:
    def test_get_current_usage_and_effective_limit_do_not_raise(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        service = EntitlementService()

        # Must not raise for a restricted organization -- only writes are guarded.
        service.get_current_usage(organization, LimitedResource.RESOURCE_CALENDARS)
        service.get_effective_limit(organization, LimitedResource.RESOURCE_CALENDARS)

    def test_calendar_reads_are_open(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        calendar = baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id="restricted-read",
        )
        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        events = service.get_calendar_events_expanded(
            calendar,
            timezone.now(),
            timezone.now() + datetime.timedelta(days=1),
        )
        assert list(events) == []


# ---------------------------------------------------------------------------
# /billing/ stays reachable for a restricted organization
# ---------------------------------------------------------------------------


def _plan(
    limit_values: dict[str, int | None] | None = None, monthly_price: Decimal = Decimal("0")
) -> BillingPlan:
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=monthly_price,
        annual_price=None,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


@pytest.mark.django_db
class TestRestrictedOrganizationBillingSurfaceStaysOpen:
    """The single worst failure this phase could ship: a restricted org that
    cannot reach ``/billing/`` to pay its way out. None of the ``/billing/``
    viewsets call ``EntitlementService`` at all (confirmed by reading
    ``payments/billing_views.py``) -- these tests prove that from the outside,
    driving the real HTTP surface for a RESTRICTED organization."""

    def _client_for_restricted_org(self):
        from users.factories import UserFactory

        user = UserFactory().create_user()
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        pro_plan = _plan({LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50"))
        free_plan = _plan({LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0"))
        subscription = SubscriptionService().create_subscription_for_organization(
            organization, plan=pro_plan
        )
        assert subscription is not None
        subscription.billing_state = BillingState.RESTRICTED
        subscription.external_id = ""
        subscription.save(update_fields=["billing_state", "external_id"])
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            is_active=True,
            is_billing_owner=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, organization, subscription, free_plan

    def test_plans_usage_and_subscription_reads_stay_open(self):
        client, _organization, _subscription, _free_plan = self._client_for_restricted_org()

        assert client.get(reverse("api:BillingPlan-list")).status_code == status.HTTP_200_OK
        assert client.get(reverse("api:BillingUsage-retrieve")).status_code == status.HTTP_200_OK
        response = client.get(reverse("api:BillingSubscription-retrieve"))
        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.RESTRICTED

    def test_cancel_is_reachable_and_resolves_the_restriction(self):
        client, _organization, _subscription, _free_plan = self._client_for_restricted_org()

        response = client.post(reverse("api:BillingSubscription-cancel"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.CANCELLED

    def test_downgrade_change_plan_is_reachable_while_restricted(self):
        """A downgrade needs no provider round trip (no cash refund, no
        upgrade-side charge), so this is reachable with no payment adapter
        mocked -- proving ``/billing/subscription/change-plan/`` itself never
        401/402s a restricted org. Also exercises the defensive path in
        ``SubscriptionService._schedule_downgrade``: a RESTRICTED subscription
        has no GRACE edge on the diagram from RESTRICTED, so the downgrade's
        own billing_state-transition attempt is caught and logged rather than
        raised, and the downgrade itself still applies."""
        client, _organization, subscription, free_plan = self._client_for_restricted_org()

        response = client.post(
            reverse("api:BillingSubscription-change-plan"),
            {
                "plan_slug": free_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "restricted-downgrade-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        subscription.refresh_from_db()
        assert subscription.pending_plan_id == free_plan.pk
        # billing_state has no legal RESTRICTED -> GRACE edge; left unchanged.
        assert subscription.billing_state == BillingState.RESTRICTED
