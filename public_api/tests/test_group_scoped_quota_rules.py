"""Integration tests for the public GraphQL surface of group-scoped quota
rules (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3c).

Direct mirror of ``test_group_scoped_blocked_times.py`` for quota rules,
minus the recurrence/reason machinery (quota rules are non-recurring and
have no time range -- just a period and a cap). Covers
``groupScopedQuotaRules`` (query) and ``batchUpsertGroupScopedQuotaRules``
(batch-upsert mutation): batch apply, idempotent replay (identical final
state, no duplicates), the (calendar, slot, period) uniqueness constraint
surfaced as a clean ``success=False`` result rather than a server error,
cross-organization scoping, and the IDOR rule/calendar cross-check. Quota
rules are NOT metered (spec: "Windows and blocks both consume the limit;
quota rules do not"), so there is no over-limit test here -- instead, a
``RESTRICTED`` billing root is asserted to still block the batch (the one
guard that DOES apply to an unmetered resource).
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType, QuotaPeriod
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState
from payments.models import Subscription
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


GROUP_SCOPED_QUOTA_RULES_QUERY = """
query GroupScopedQuotaRules($groupSlotId: Int!, $calendarId: Int) {
    groupScopedQuotaRules(groupSlotId: $groupSlotId, calendarId: $calendarId) {
        id
        calendarId
        groupSlotId
        period
        cap
    }
}
"""

BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION = """
mutation BatchUpsertGroupScopedQuotaRules($input: BatchGroupScopedQuotaRulesInput!) {
    batchUpsertGroupScopedQuotaRules(input: $input) {
        success
        errorMessage
        quotaRules {
            id
            calendarId
            groupSlotId
            period
            cap
        }
    }
}
"""


@pytest.mark.django_db
class TestGroupScopedQuotaRulesPublicAPI:
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
    # groupScopedQuotaRules -- query
    # ------------------------------------------------------------------

    def test_query_returns_group_scoped_quota_rules_for_the_slot(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org,
            [
                PublicAPIResources.GROUP_SCOPED_QUOTA_RULES,
                PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES,
            ],
        )

        rule = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        response = self._post(
            GROUP_SCOPED_QUOTA_RULES_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rules = data["data"]["groupScopedQuotaRules"]
        assert len(rules) == 1
        assert rules[0]["id"] == rule.id
        assert rules[0]["calendarId"] == calendar.id
        assert rules[0]["groupSlotId"] == slot.id
        assert rules[0]["period"] == QuotaPeriod.WEEK
        assert rules[0]["cap"] == 3

    def test_query_cross_organization_scoping_returns_empty(self):
        """A group slot belonging to another organization is invisible."""
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        CalendarGroupSlotQuotaRule.objects.create(
            organization=org_b,
            calendar=calendar_b,
            group_slot=slot_b,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            GROUP_SCOPED_QUOTA_RULES_QUERY,
            system_user,
            token,
            auth_service,
            {"groupSlotId": slot_b.id, "calendarId": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["groupScopedQuotaRules"] == []

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- apply
    # ------------------------------------------------------------------

    def test_batch_upsert_applies_mixed_operations(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        existing = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )
        to_delete = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.DAY,
            cap=1,
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.MONTH,
                            "cap": 10,
                        },
                        {
                            "action": "update",
                            "calendarId": calendar.id,
                            "ruleId": existing.id,
                            "cap": 5,
                        },
                        {
                            "action": "delete",
                            "calendarId": calendar.id,
                            "ruleId": to_delete.id,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        # Final roster state: the updated row + the newly created row (deleted row gone).
        assert len(result["quotaRules"]) == 2

        existing.refresh_from_db()
        assert existing.cap == 5
        assert not (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(id=to_delete.id)
            .exists()
        )

        remaining = CalendarGroupSlotQuotaRule.objects.for_group_slot(
            slot.id
        ).filter_by_organization(org.id)
        assert remaining.count() == 2

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- idempotent replay
    # ------------------------------------------------------------------

    def test_identical_replay_is_a_no_op(self):
        """Replaying an identical create-only batch (spec UC-5) lands on the
        same final state instead of duplicating rows."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        variables = {
            "input": {
                "organizationId": org.id,
                "groupSlotId": slot.id,
                "operations": [
                    {
                        "action": "create",
                        "calendarId": calendar.id,
                        "period": QuotaPeriod.WEEK,
                        "cap": 3,
                    },
                ],
            }
        }

        first = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert first.status_code == 200
        first_result = first.json()["data"]["batchUpsertGroupScopedQuotaRules"]
        assert first_result["success"] is True
        assert len(first_result["quotaRules"]) == 1
        first_rule_id = first_result["quotaRules"][0]["id"]

        rows_after_first = (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_first == 1

        second = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
            system_user,
            token,
            auth_service,
            variables,
        )
        assert second.status_code == 200
        second_result = second.json()["data"]["batchUpsertGroupScopedQuotaRules"]
        assert second_result["success"] is True

        # Same final state: one row, same id, no duplicate created.
        assert len(second_result["quotaRules"]) == 1
        assert second_result["quotaRules"][0]["id"] == first_rule_id
        rows_after_replay = (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
        )
        assert rows_after_replay == 1, (
            "Replaying an identical batch must not create a duplicate quota rule."
        )

    def test_cap_participates_in_the_idempotent_create_match_key(self):
        """``cap`` is part of the idempotent-create match key (calendar,
        group slot, period, cap). A ``create`` op naming the SAME period but
        a DIFFERENT cap must NOT match the existing rule -- it falls through
        to a real insert and trips the (calendar, slot, period) unique
        constraint, surfaced as a validation error rather than silently
        keeping the old cap."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        existing = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 5,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["quotaRules"] == []

        existing.refresh_from_db()
        assert existing.cap == 3
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
            == 1
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- atomicity on mid-batch failure
    # ------------------------------------------------------------------

    def test_batch_atomicity_rolls_back_all_ops_on_later_collision(self):
        """When a later operation in a batch collides on the uniqueness
        constraint, the entire batch is rolled back (all-or-nothing semantics).
        Specifically, op1 creates a rule for period=DAY, then op2 tries to create
        another with the same period (collision), which should fail; both op1
        (created) and op2 (attempted) must be rolled back.
        """
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.DAY,
                            "cap": 1,
                        },
                        {
                            "action": "create",
                            "calendarId": calendar.id,
                            "period": QuotaPeriod.DAY,
                            "cap": 2,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        # The batch must fail cleanly (not a 500).
        assert result["success"] is False
        assert result["errorMessage"]
        assert result["quotaRules"] == []
        # CRITICAL: zero rules exist for this (calendar, slot) -- op1 was ALSO
        # rolled back, proving the outer transaction rolled back everything.
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id, group_slot_fk_id=slot.id)
            .count()
            == 0
        ), "Batch atomicity: all operations must be rolled back on mid-batch collision."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- uniqueness -> validation error
    # ------------------------------------------------------------------

    def test_create_duplicate_period_is_surfaced_as_a_clean_failure_not_a_server_error(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.DAY,
            cap=1,
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.DAY,
                            "cap": 2,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        # A clean GraphQL-level "no", not an unhandled server error at the
        # transport level.
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"]
        assert result["quotaRules"] == []
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .count()
            == 1
        )

    def test_update_colliding_with_existing_period_is_surfaced_as_a_clean_failure(self):
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        day_rule = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.DAY,
            cap=1,
        )
        CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "update",
                            "calendarId": calendar.id,
                            "ruleId": day_rule.id,
                            "period": QuotaPeriod.WEEK,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["quotaRules"] == []

        day_rule.refresh_from_db()
        assert day_rule.period == QuotaPeriod.DAY

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- invalid period value
    # ------------------------------------------------------------------

    def test_batch_create_with_invalid_period_is_validation_error(self):
        """A batch create operation with an invalid period value (not in
        QuotaPeriod.values) must be rejected as a clean validation failure
        (success=False), not a 500."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": "invalid",
                            "cap": 3,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"]
        assert result["quotaRules"] == []
        # Nothing created.
        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id, group_slot_fk_id=slot.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- RESTRICTED billing root still blocks
    # ------------------------------------------------------------------

    def test_restricted_organization_batch_rejected_wholesale(self):
        """Quota rules are unmetered, so there is no plan-limit ceiling to hit
        here -- but the general RESTRICTED-billing-root guard every other
        guarded write goes through must still apply. Nothing is created."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )
        # Restrict AFTER the system-user token is created -- token creation is
        # itself a guarded, limit-checked path that a RESTRICTED billing root
        # would also block.
        self._restrict_organization(org)

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 3,
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
            CalendarGroupSlotQuotaRule.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .count()
            == 0
        ), "A RESTRICTED organization's batch must create nothing."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- cross-organization scoping
    # ------------------------------------------------------------------

    def test_cross_organization_group_slot_is_rejected(self):
        org_a = self._setup_org()
        org_b = self._setup_org()
        calendar_b = self._make_calendar(org_b)
        slot_b = self._make_group_slot(org_b, calendar_b)

        system_user, token, auth_service = self._make_org_wide_system_user(
            org_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 3,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["quotaRules"] == []
        assert (
            not CalendarGroupSlotQuotaRule.objects.for_group_slot(slot_b.id)
            .filter_by_organization(org_b.id)
            .filter(calendar_fk_id=calendar_b.id)
            .exists()
        )

    def test_cross_owner_scoped_token_rejected_wholesale(self):
        """A scoped token may not write group-scoped quota rules on a
        calendar it does not own -- same not-found shape as a genuinely
        missing calendar, and nothing is written."""
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_b)

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 3,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"] == "Calendar not found."
        assert (
            CalendarGroupSlotQuotaRule.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- IDOR (rule/calendar cross-check
    # on update/delete)
    # ------------------------------------------------------------------

    def test_update_with_rule_from_another_calendar_rejected_wholesale(self):
        """A calendar-owner-scoped token pairs a calendarId it owns with a
        ruleId belonging to a DIFFERENT calendar in the same slot's roster.
        assert_calendar_in_owner_scope alone would let this through (it only
        checks ownership of calendarId) -- the service must ALSO reject
        because the resolved rule does not belong to that calendar. Whole
        batch fails, not-found shape, nothing changes."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_rule = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            # ...but ruleId belongs to calendar_b.
                            "ruleId": foreign_rule.id,
                            "cap": 10,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["quotaRules"] == []

        foreign_rule.refresh_from_db()
        assert foreign_rule.cap == 3
        assert foreign_rule.calendar_fk_id == calendar_b.id

    def test_delete_with_rule_from_another_calendar_rejected_wholesale(self):
        """Same IDOR as the update case above, but for a delete op: the
        foreign rule must still exist, unmodified, afterward."""
        org = self._setup_org()
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        slot = self._make_group_slot(org, calendar_a, calendar_b)

        foreign_rule = CalendarGroupSlotQuotaRule.objects.create(
            organization=org,
            calendar=calendar_b,
            group_slot=slot,
            period=QuotaPeriod.WEEK,
            cap=3,
        )

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            # calendarId the token owns, ruleId belongs to calendar_b.
                            "calendarId": calendar_a.id,
                            "ruleId": foreign_rule.id,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["quotaRules"] == []

        assert (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(org.id)
            .filter(id=foreign_rule.id)
            .exists()
        ), "The foreign rule must NOT be deleted by a batch it was never authorized for."

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedQuotaRules -- create outside the slot's roster
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
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 3,
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedQuotaRules"]
        assert result["success"] is False
        assert result["errorMessage"] == "Group slot not found."
        assert result["quotaRules"] == []
        assert (
            CalendarGroupSlotQuotaRule.objects.for_group_slot(slot.id)
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
                "query": GROUP_SCOPED_QUOTA_RULES_QUERY,
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
                "query": BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": org.id,
                        "groupSlotId": slot.id,
                        "operations": [
                            {
                                "action": "create",
                                "calendarId": calendar.id,
                                "period": QuotaPeriod.WEEK,
                                "cap": 3,
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
            CalendarGroupSlotQuotaRule.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .count()
            == 0
        )

    def test_query_token_without_resource_grant_denied(self):
        """An authenticated token that lacks the GROUP_SCOPED_QUOTA_RULES
        resource grant is denied."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            GROUP_SCOPED_QUOTA_RULES_QUERY,
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
        BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES resource grant is denied, and
        nothing is written."""
        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        # Grant an unrelated resource only.
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.GROUP_SCOPED_QUOTA_RULES]
        )

        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES_MUTATION,
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
                            "period": QuotaPeriod.WEEK,
                            "cap": 3,
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
            CalendarGroupSlotQuotaRule.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # Existing block operations are unchanged (no-regression)
    # ------------------------------------------------------------------

    def test_existing_group_scoped_blocked_times_query_shape_unchanged(self):
        """Byte-for-byte shape check: the pre-existing groupScopedBlockedTimes
        query's response is unaffected by this phase's additions."""
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        calendar = self._make_calendar(org)
        slot = self._make_group_slot(org, calendar)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.GROUP_SCOPED_BLOCKED_TIMES]
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
            external_id=f"block-{uuid.uuid4()}",
        )

        query = """
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
        blocks = data["data"]["groupScopedBlockedTimes"]
        assert len(blocks) == 1
        assert blocks[0]["id"] == block.id
