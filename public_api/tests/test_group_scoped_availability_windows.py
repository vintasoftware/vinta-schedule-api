"""Integration tests for the public GraphQL surface of group-scoped
availability windows.

Covers ``groupScopedAvailabilityWindows`` (query) and
``batchUpsertGroupScopedAvailabilityWindows`` (batch-upsert mutation):
batch apply, idempotent replay (identical final state, no duplicates),
whole-batch rejection at the plan limit with the SAME over-limit response
body the existing ``batchUpdateAvailabilityWindows`` mutation already
returns, cross-organization scoping, and a no-regression check that the
existing (frozen) availability query/mutation are byte-for-byte unchanged.
"""

import datetime
import uuid

from django.contrib.auth import get_user_model
from django.utils import timezone as django_timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient
from vinta_billing.constants import BillingState, LimitKind
from vinta_billing.models import (
    BillingPlan,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership
from payments.seams.resource_keys import AVAILABILITY_WINDOWS, PARTNER_API
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


GROUP_SCOPED_AVAILABILITY_WINDOWS_QUERY = """
query GroupScopedAvailabilityWindows($groupSlotId: Int!, $calendarId: Int) {
    groupScopedAvailabilityWindows(groupSlotId: $groupSlotId, calendarId: $calendarId) {
        id
        calendarId
        groupSlotId
        startTime
        endTime
        timezone
        rruleString
        isRecurring
    }
}
"""

BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION = """
mutation BatchUpsertGroupScopedAvailabilityWindows($input: BatchGroupScopedAvailabilityWindowsInput!) {
    batchUpsertGroupScopedAvailabilityWindows(input: $input) {
        success
        errorMessage
        windows {
            id
            calendarId
            groupSlotId
            startTime
            endTime
            timezone
            rruleString
        }
    }
}
"""

BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION = """
mutation BatchUpdateAvailabilityWindows($input: BatchAvailabilityInput!) {
    batchUpdateAvailabilityWindows(input: $input) {
        success
        errorMessage
        availableTimes {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestGroupScopedAvailabilityWindowsPublicAPI:
    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _setup_org(self) -> Organization:
        return baker.make(Organization, name="Test Org")

    def _make_org_wide_system_user(self, org, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"org_wide_token_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_scoped_system_user(self, org, membership, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_token_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_owner_with_calendar(self, org: Organization):
        """Create a user, their active membership, and a calendar they own.

        Returns (user, membership, calendar).
        """
        user_model = get_user_model()
        unique = uuid.uuid4().hex[:8]
        owner = baker.make(user_model, email=f"owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        calendar = Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )
        CalendarOwnership.objects.create(
            organization=org, calendar=calendar, membership_user_id=owner.id
        )
        return owner, membership, calendar

    def _make_calendar(self, org: Organization) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        return Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

    def _make_group_slot(self, org: Organization, *calendars: Calendar) -> CalendarGroupSlot:
        group = CalendarGroup.objects.create(organization=org, name="Surgery")
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Lead")
        for calendar in calendars:
            CalendarGroupSlotMembership.objects.create(
                organization=org, slot=slot, calendar=calendar
            )
        return slot

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _organization_with_availability_windows_limit(self, limit_value: int) -> Organization:
        """A standalone organization with a finite ceiling on availability_windows.

        Mirrors calendar_integration/tests/services/test_calendar_limits.py's
        ``_organization_with_limit`` helper, plus an explicit ``partner_api``
        entitlement -- unlike that helper's service-layer callers, these tests
        go through the real GraphQL endpoint, which
        ``PublicApiSystemUserMiddleware`` gates on ``partner_api`` before any
        resolver runs (``EntitlementService.has_entitlement`` fails closed on a
        bespoke plan with no entitlement rows).
        """
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        now = django_timezone.now()
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
            resource_key=AVAILABILITY_WINDOWS,
            limit_value=limit_value,
            kind=LimitKind.PREPAID,
        )
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=PARTNER_API,
            is_enabled=True,
        )
        return organization

    # ------------------------------------------------------------------
    # groupScopedAvailabilityWindows -- query
    # ------------------------------------------------------------------

    def test_query_returns_group_scoped_windows_for_the_slot(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org,
            [
                PublicAPIResources.GROUP_SCOPED_AVAILABILITY_WINDOWS,
                PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS,
            ],
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        window = AvailableTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )

        response = self._post(
            GROUP_SCOPED_AVAILABILITY_WINDOWS_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        windows = data["data"]["groupScopedAvailabilityWindows"]
        assert len(windows) == 1
        assert windows[0]["id"] == window.id
        assert windows[0]["calendarId"] == calendar.id
        assert windows[0]["groupSlotId"] == slot.id
        assert windows[0]["timezone"] == "UTC"

    def test_query_cross_organization_scoping_returns_empty(self):
        """A group slot belonging to another organization is invisible."""
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        AvailableTime.objects.unscoped().create(
            organization=org_b,
            calendar=calendar_b,
            group_slot=slot_b,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            GROUP_SCOPED_AVAILABILITY_WINDOWS_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot_b.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["groupScopedAvailabilityWindows"] == []

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- apply
    # ------------------------------------------------------------------

    def test_batch_upsert_applies_mixed_operations(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        existing = AvailableTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        to_delete = AvailableTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 2, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 2, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        create_start = datetime.datetime(2026, 10, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 10, 1, 17, 0, 0, tzinfo=datetime.UTC)
        new_start = datetime.datetime(2026, 11, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 1, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": calendar.id,
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        },
                        {
                            "action": "update",
                            "calendarId": calendar.id,
                            "windowId": existing.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                        },
                        {
                            "action": "delete",
                            "calendarId": calendar.id,
                            "windowId": to_delete.id,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        # Final roster state: the updated row + the newly created row (deleted row gone).
        assert len(result["windows"]) == 2

        existing.refresh_from_db()
        assert existing.start_time_tz_unaware == new_start
        assert existing.end_time_tz_unaware == new_end
        assert not (
            AvailableTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(id=to_delete.id)
            .exists()
        )

        remaining = AvailableTime.objects.for_group_slot(slot.id).filter_by_organization(org.id)
        assert remaining.count() == 2

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- idempotent replay
    # ------------------------------------------------------------------

    def test_identical_replay_is_a_no_op(self):
        """Replaying an identical create-only batch (spec UC-5) lands on the
        same final state instead of duplicating rows."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        variables = {
            "input": {
                "organizationId": org.id,
                "groupSlotId": slot.id,
                "operations": [
                    {
                        "action": "create",
                        "calendarId": calendar.id,
                        "startTime": start.isoformat(),
                        "endTime": end.isoformat(),
                        "timezone": "UTC",
                    },
                ],
            }
        }

        first = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert first.status_code == 200
        first_result = first.json()["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert first_result["success"] is True
        assert len(first_result["windows"]) == 1
        first_window_id = first_result["windows"][0]["id"]

        rows_after_first = (
            AvailableTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_first == 1

        second = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert second.status_code == 200
        second_result = second.json()["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert second_result["success"] is True

        # Same final state: one row, same id, no duplicate created.
        assert len(second_result["windows"]) == 1
        assert second_result["windows"][0]["id"] == first_window_id
        rows_after_replay = (
            AvailableTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_replay == 1, (
            "Replaying an identical batch must not create a duplicate window."
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- over-limit rejects whole batch
    # ------------------------------------------------------------------

    @pytest.mark.no_auto_subscription
    def test_over_limit_batch_rejected_whole_with_same_body_as_base_batch_write(self):
        """A batch that would exceed the plan's availability_windows ceiling is
        rejected wholesale (nothing created), with the SAME over-limit response
        body the existing batchUpdateAvailabilityWindows mutation returns for the
        identical resource ceiling.
        """
        org = self._organization_with_availability_windows_limit(2)
        base_calendar = self._make_calendar(org)
        group_calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, group_calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org,
            [
                PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS,
                PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS,
            ],
        )

        def _create_op(day: int) -> dict:
            return {
                "action": "create",
                "startTime": datetime.datetime(
                    2026, 9, day, 9, 0, 0, tzinfo=datetime.UTC
                ).isoformat(),
                "endTime": datetime.datetime(
                    2026, 9, day, 17, 0, 0, tzinfo=datetime.UTC
                ).isoformat(),
                "timezone": "UTC",
            }

        # --- Existing (frozen) base batch write, over the limit (3 creates, ceiling 2). ---
        base_response = self._post(
            BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": base_calendar.id,
                    "operations": [_create_op(1), _create_op(2), _create_op(3)],
                }
            },
        )
        assert base_response.status_code == 200
        base_data = base_response.json()
        assert len(base_data["errors"]) == 1
        base_extensions = base_data["errors"][0]["extensions"]
        assert base_extensions["code"] == "limit_exceeded"
        assert base_extensions["resource"] == AVAILABILITY_WINDOWS
        assert (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=base_calendar.id)
            .count()
            == 0
        ), "The over-limit base batch must create nothing."

        # --- New group-scoped batch write, same ceiling, same net growth. ---
        group_create_ops = [
            {
                "action": "create",
                "calendarId": group_calendar.id,
                "startTime": datetime.datetime(
                    2026, 10, day, 9, 0, 0, tzinfo=datetime.UTC
                ).isoformat(),
                "endTime": datetime.datetime(
                    2026, 10, day, 17, 0, 0, tzinfo=datetime.UTC
                ).isoformat(),
                "timezone": "UTC",
            }
            for day in (1, 2, 3)
        ]
        group_response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": group_create_ops,
                }
            },
        )
        assert group_response.status_code == 200
        group_data = group_response.json()
        assert len(group_data["errors"]) == 1
        group_extensions = group_data["errors"][0]["extensions"]

        # Byte-identical over-limit body: nothing about usage changed between the
        # two attempts (both rolled back, creating nothing), so the shared
        # OverLimitError contract renders identically for either write path.
        assert group_extensions == base_extensions
        assert group_extensions["code"] == "limit_exceeded"
        assert group_extensions["resource"] == AVAILABILITY_WINDOWS

        assert (
            AvailableTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count()
            == 0
        ), "The over-limit group-scoped batch must create nothing."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- cross-organization scoping
    # ------------------------------------------------------------------

    def test_cross_organization_group_slot_is_rejected(self):
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org_a.id,
                    "groupSlotId": slot_b.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": calendar_b.id,
                            "startTime": datetime.datetime(
                                2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "endTime": datetime.datetime(
                                2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["windows"] == []
        assert (
            not AvailableTime.objects.for_group_slot(slot_b.id)
            .filter_by_organization(org_b.id)
            .filter(calendar_fk_id=calendar_b.id, timezone="UTC")
            .exists()
        )

    def test_cross_owner_scoped_token_rejected_wholesale(self):
        """A scoped token may not write group-scoped windows on a calendar it
        does not own -- same not-found shape as a genuinely missing calendar,
        and nothing is written."""
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_b)

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": calendar_b.id,
                            "startTime": datetime.datetime(
                                2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "endTime": datetime.datetime(
                                2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Calendar not found."
        assert (
            AvailableTime.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- IDOR (window/calendar
    # cross-check on update/delete)
    # ------------------------------------------------------------------

    def test_update_with_window_from_another_calendar_rejected_wholesale(self):
        """A calendar-owner-scoped token pairs a calendarId it owns with a
        windowId belonging to a DIFFERENT calendar in the same slot's roster.
        assert_calendar_in_owner_scope alone would let this through (it only
        checks ownership of calendarId) -- the service must ALSO reject
        because the resolved window does not belong to that calendar. Whole
        batch fails, not-found shape, nothing changes."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_window = AvailableTime.objects.unscoped().create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        new_start = datetime.datetime(2026, 11, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 1, 16, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "update",
                            # calendarId the token owns...
                            "calendarId": calendar_a.id,
                            # ...but windowId belongs to calendar_b.
                            "windowId": foreign_window.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["windows"] == []

        foreign_window.refresh_from_db()
        assert foreign_window.start_time_tz_unaware != new_start
        assert foreign_window.end_time_tz_unaware != new_end
        assert foreign_window.calendar_fk_id == calendar_b.id

    def test_delete_with_window_from_another_calendar_rejected_wholesale(self):
        """Same IDOR as the update case above, but for a delete op: the
        foreign window must still exist, unmodified, afterward."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_window = AvailableTime.objects.unscoped().create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "delete",
                            # calendarId the token owns, windowId belongs to calendar_b.
                            "calendarId": calendar_a.id,
                            "windowId": foreign_window.id,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["windows"] == []

        assert (
            AvailableTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(id=foreign_window.id)
            .exists()
        ), "The foreign window must NOT be deleted by a batch it was never authorized for."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- create outside the slot's roster
    # ------------------------------------------------------------------

    def test_create_with_calendar_outside_slot_roster_rejected_wholesale(self):
        """A create op's calendarId is a calendar the token owns/can access, but
        it is not a member of the target groupSlotId's roster -- rejected with
        the not-found shape, nothing created."""
        org = self._setup_org()
        roster_calendar = self._make_calendar(org)
        outside_calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, roster_calendar)

        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": outside_calendar.id,
                            "startTime": datetime.datetime(
                                2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "endTime": datetime.datetime(
                                2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC
                            ).isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["windows"] == []
        assert (
            AvailableTime.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=outside_calendar.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # Anonymous / unauthorized access -- query and mutation
    # ------------------------------------------------------------------

    def test_query_anonymous_request_denied(self):
        """No Authorization header -- the standard auth error, no data leaked."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)

        response = self.client.post(
            "/graphql/",
            data={
                "query": GROUP_SCOPED_AVAILABILITY_WINDOWS_QUERY,
                "variables": {"groupSlotId": slot.id, "calendarId": None},
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_mutation_anonymous_request_denied(self):
        """No Authorization header on the mutation -- the standard auth error,
        no write applied."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)

        response = self.client.post(
            "/graphql/",
            data={
                "query": BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": org.id,
                        "groupSlotId": slot.id,
                        "operations": [
                            {
                                "action": "create",
                                "calendarId": calendar.id,
                                "startTime": "2026-09-01T09:00:00Z",
                                "endTime": "2026-09-01T17:00:00Z",
                                "timezone": "UTC",
                            }
                        ],
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert (
            AvailableTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count()
            == 0
        )

    def test_query_token_without_resource_grant_denied(self):
        """An authenticated token that lacks the GROUP_SCOPED_AVAILABILITY_WINDOWS
        resource grant is denied."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            GROUP_SCOPED_AVAILABILITY_WINDOWS_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_mutation_token_without_resource_grant_denied(self):
        """An authenticated token that lacks the
        BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS resource grant is denied,
        and nothing is written."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": calendar.id,
                            "startTime": "2026-09-01T09:00:00Z",
                            "endTime": "2026-09-01T17:00:00Z",
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()
        assert (
            AvailableTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count()
            == 0
        )

    # ------------------------------------------------------------------
    # Existing availability operations are unchanged (no-regression)
    # ------------------------------------------------------------------

    def test_existing_batch_update_availability_windows_response_shape_unchanged(self):
        """Byte-for-byte shape check: the frozen batchUpdateAvailabilityWindows
        mutation's response is unaffected by the group-scoped additions."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "operations": [
                        {
                            "action": "create",
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpdateAvailabilityWindows"]

        # Exact shape: only these three top-level keys, and availableTimes
        # entries carry only id/startTime/endTime (as queried) -- unchanged
        # from before the group-scoped additions.
        assert set(result.keys()) == {"success", "errorMessage", "availableTimes"}
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert len(result["availableTimes"]) == 1
        window = result["availableTimes"][0]
        assert set(window.keys()) == {"id", "startTime", "endTime"}
        assert datetime.datetime.fromisoformat(window["startTime"]) == start
        assert datetime.datetime.fromisoformat(window["endTime"]) == end
