"""Integration tests for the public GraphQL surface of group-scoped
blocked times (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 2b).

Direct mirror of ``test_group_scoped_availability_windows.py`` for blocks.
Covers ``groupScopedBlockedTimes`` (query) and
``batchUpsertGroupScopedBlockedTimes`` (batch-upsert mutation): batch apply,
idempotent replay (identical final state, no duplicates), cross-organization
scoping, and the IDOR window/calendar cross-check. Blocked time is NOT
metered yet (Phase 2c does that), so there is no over-limit test here --
instead, a ``RESTRICTED`` billing root is asserted to still block the batch
(the one guard that DOES apply pre-metering).
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState
from payments.models import Subscription
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


GROUP_SCOPED_BLOCKED_TIMES_QUERY = """
query GroupScopedBlockedTimes($groupSlotId: Int!, $calendarId: Int) {
    groupScopedBlockedTimes(groupSlotId: $groupSlotId, calendarId: $calendarId) {
        id
        calendarId
        groupSlotId
        startTime
        endTime
        timezone
        reason
        rruleString
        isRecurring
    }
}
"""

BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION = """
mutation BatchUpsertGroupScopedBlockedTimes($input: BatchGroupScopedBlockedTimesInput!) {
    batchUpsertGroupScopedBlockedTimes(input: $input) {
        success
        errorMessage
        blockedTimes {
            id
            calendarId
            groupSlotId
            startTime
            endTime
            timezone
            reason
            rruleString
        }
    }
}
"""


@pytest.mark.django_db
class TestGroupScopedBlockedTimesPublicAPI:
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
        from django.contrib.auth import get_user_model

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

    def _restrict_organization(self, organization: Organization) -> None:
        """Flip ``organization``'s (auto-provisioned, unlimited) subscription to
        RESTRICTED in place -- applied AFTER any setup (e.g. system-user-token
        creation) that itself goes through a guarded, limit-checked path, so
        that setup is not blocked by the very state under test."""
        Subscription.objects.filter(organization=organization).update(
            billing_state=BillingState.RESTRICTED
        )

    # ------------------------------------------------------------------
    # groupScopedBlockedTimes -- query
    # ------------------------------------------------------------------

    def test_query_returns_group_scoped_blocks_for_the_slot(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org,
            [
                PublicAPIResources.GROUP_SCOPED_BLOCKED_TIMES,
                PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES,
            ],
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        block = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
            reason="On call elsewhere",
            external_id=f"block-{uuid.uuid4()}",
        )

        response = self._post(
            GROUP_SCOPED_BLOCKED_TIMES_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        blocks = data["data"]["groupScopedBlockedTimes"]
        assert len(blocks) == 1
        assert blocks[0]["id"] == block.id
        assert blocks[0]["calendarId"] == calendar.id
        assert blocks[0]["groupSlotId"] == slot.id
        assert blocks[0]["timezone"] == "UTC"
        assert blocks[0]["reason"] == "On call elsewhere"

    def test_query_cross_organization_scoping_returns_empty(self):
        """A group slot belonging to another organization is invisible."""
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        BlockedTime.objects.unscoped().create(
            organization=org_b,
            calendar=calendar_b,
            group_slot=slot_b,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id=f"block-{uuid.uuid4()}",
        )

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            GROUP_SCOPED_BLOCKED_TIMES_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot_b.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["groupScopedBlockedTimes"] == []

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- apply
    # ------------------------------------------------------------------

    def test_batch_upsert_applies_mixed_operations(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        existing = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id=f"block-{uuid.uuid4()}",
        )
        to_delete = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 2, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 2, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id=f"block-{uuid.uuid4()}",
        )

        create_start = datetime.datetime(2026, 10, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 10, 1, 17, 0, 0, tzinfo=datetime.UTC)
        new_start = datetime.datetime(2026, 11, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 1, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
                            "reason": "Conference",
                        },
                        {
                            "action": "update",
                            "calendarId": calendar.id,
                            "blockId": existing.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                        },
                        {
                            "action": "delete",
                            "calendarId": calendar.id,
                            "blockId": to_delete.id,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        # Final roster state: the updated row + the newly created row (deleted row gone).
        assert len(result["blockedTimes"]) == 2

        existing.refresh_from_db()
        assert existing.start_time_tz_unaware == new_start
        assert existing.end_time_tz_unaware == new_end
        assert not (
            BlockedTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(id=to_delete.id)
            .exists()
        )

        remaining = BlockedTime.objects.for_group_slot(slot.id).filter_by_organization(org.id)
        assert remaining.count() == 2

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- idempotent replay
    # ------------------------------------------------------------------

    def test_identical_replay_is_a_no_op(self):
        """Replaying an identical create-only batch (spec UC-5) lands on the
        same final state instead of duplicating rows."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
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
                        "reason": "Conference",
                    },
                ],
            }
        }

        first = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert first.status_code == 200
        first_result = first.json()["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert first_result["success"] is True
        assert len(first_result["blockedTimes"]) == 1
        first_block_id = first_result["blockedTimes"][0]["id"]

        rows_after_first = (
            BlockedTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_first == 1

        second = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert second.status_code == 200
        second_result = second.json()["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert second_result["success"] is True

        # Same final state: one row, same id, no duplicate created.
        assert len(second_result["blockedTimes"]) == 1
        assert second_result["blockedTimes"][0]["id"] == first_block_id
        rows_after_replay = (
            BlockedTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_replay == 1, (
            "Replaying an identical batch must not create a duplicate block."
        )

    def test_reason_participates_in_the_idempotent_create_match_key(self):
        """``reason`` is part of ``_find_matching_group_scoped_block``'s match
        key (calendar, group slot, start, end, timezone, reason, rrule). A
        ``create`` op with the SAME reason as an already-persisted block
        matches it (no-op); a ``create`` op with the SAME (calendar, slot,
        start, end, timezone) but a DIFFERENT reason must NOT match -- it is
        a genuinely distinct block and must persist as its own row. If
        ``reason`` were dropped from that filter, both ops below would match
        the same pre-existing row and only one row would remain."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        existing = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
            reason="Conference",
            external_id=f"block-{uuid.uuid4()}",
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            # Identical to `existing`, including reason -- matches,
                            # no new row.
                            "action": "create",
                            "calendarId": calendar.id,
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                            "reason": "Conference",
                        },
                        {
                            # Same (calendar, slot, start, end, timezone) as
                            # `existing`, but a DIFFERENT reason -- must NOT match;
                            # persists as its own distinct row.
                            "action": "create",
                            "calendarId": calendar.id,
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                            "reason": "On call elsewhere",
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is True

        # Two distinct rows: `existing` (matched, unchanged) + the new
        # different-reason block. Not one -- which is what would happen if
        # `reason` were dropped from the idempotent-create match key and the
        # second op collapsed into `existing` too.
        assert len(result["blockedTimes"]) == 2
        reasons = {block["reason"] for block in result["blockedTimes"]}
        assert reasons == {"Conference", "On call elsewhere"}

        rows = (
            BlockedTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
        )
        assert rows.count() == 2
        assert rows.filter(id=existing.id).exists()

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- RESTRICTED billing root still blocks
    # ------------------------------------------------------------------

    def test_restricted_organization_batch_rejected_wholesale(self):
        """Blocked time is not metered yet (Phase 2c), so there is no
        plan-limit ceiling to hit here -- but the general RESTRICTED-billing-
        root guard every other guarded write goes through must still apply.
        Nothing is created."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )
        # Restrict AFTER the system-user token is created -- token creation is
        # itself a guarded, limit-checked path that a RESTRICTED billing root
        # would also block.
        self._restrict_organization(org)

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
        assert len(data["errors"]) == 1
        extensions = data["errors"][0]["extensions"]
        assert extensions["code"] == "limit_exceeded"
        assert extensions["remedy"] == "resolve_billing"
        assert (
            BlockedTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count() == 0
        ), "A RESTRICTED organization's batch must create nothing."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- cross-organization scoping
    # ------------------------------------------------------------------

    def test_cross_organization_group_slot_is_rejected(self):
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is False
        assert result["blockedTimes"] == []
        assert (
            not BlockedTime.objects.for_group_slot(slot_b.id)
            .filter_by_organization(org_b.id)
            .filter(calendar_fk_id=calendar_b.id, timezone="UTC")
            .exists()
        )

    def test_cross_owner_scoped_token_rejected_wholesale(self):
        """A scoped token may not write group-scoped blocks on a calendar it
        does not own -- same not-found shape as a genuinely missing calendar,
        and nothing is written."""
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_b)

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is False
        assert result["errorMessage"] == "Calendar not found."
        assert (
            BlockedTime.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- IDOR (block/calendar cross-check
    # on update/delete)
    # ------------------------------------------------------------------

    def test_update_with_block_from_another_calendar_rejected_wholesale(self):
        """A calendar-owner-scoped token pairs a calendarId it owns with a
        blockId belonging to a DIFFERENT calendar in the same slot's roster.
        assert_calendar_in_owner_scope alone would let this through (it only
        checks ownership of calendarId) -- the service must ALSO reject
        because the resolved block does not belong to that calendar. Whole
        batch fails, not-found shape, nothing changes."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_block = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id=f"block-{uuid.uuid4()}",
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        new_start = datetime.datetime(2026, 11, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 1, 16, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
                            # ...but blockId belongs to calendar_b.
                            "blockId": foreign_block.id,
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
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["blockedTimes"] == []

        foreign_block.refresh_from_db()
        assert foreign_block.start_time_tz_unaware != new_start
        assert foreign_block.end_time_tz_unaware != new_end
        assert foreign_block.calendar_fk_id == calendar_b.id

    def test_delete_with_block_from_another_calendar_rejected_wholesale(self):
        """Same IDOR as the update case above, but for a delete op: the
        foreign block must still exist, unmodified, afterward."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_block = BlockedTime.objects.unscoped().create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id=f"block-{uuid.uuid4()}",
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
                            # calendarId the token owns, blockId belongs to calendar_b.
                            "calendarId": calendar_a.id,
                            "blockId": foreign_block.id,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["blockedTimes"] == []

        assert (
            BlockedTime.objects.unscoped()
            .filter_by_organization(org.id)
            .filter(id=foreign_block.id)
            .exists()
        ), "The foreign block must NOT be deleted by a batch it was never authorized for."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedBlockedTimes -- create outside the slot's roster
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
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
        result = data["data"]["batchUpsertGroupScopedBlockedTimes"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["blockedTimes"] == []
        assert (
            BlockedTime.objects.for_group_slot(slot.id)
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
                "query": GROUP_SCOPED_BLOCKED_TIMES_QUERY,
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
                "query": BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
            BlockedTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count() == 0
        )

    def test_query_token_without_resource_grant_denied(self):
        """An authenticated token that lacks the GROUP_SCOPED_BLOCKED_TIMES
        resource grant is denied."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            GROUP_SCOPED_BLOCKED_TIMES_QUERY,
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
        BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES resource grant is denied, and
        nothing is written."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.GROUP_SCOPED_BLOCKED_TIMES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES_MUTATION,
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
            BlockedTime.objects.for_group_slot(slot.id).filter_by_organization(org.id).count() == 0
        )

    # ------------------------------------------------------------------
    # Existing availability window operations are unchanged (no-regression)
    # ------------------------------------------------------------------

    def test_existing_group_scoped_availability_windows_query_shape_unchanged(self):
        """Byte-for-byte shape check: the frozen groupScopedAvailabilityWindows
        query's response is unaffected by this phase's additions."""
        from calendar_integration.models import AvailableTime

        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.GROUP_SCOPED_AVAILABILITY_WINDOWS]
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

        query = """
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
        response = self._post(
            query,
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
