"""Integration tests for the BookingPolicy GraphQL CRUD surface (Phase 4).

Covers:
- bookingPolicies query: happy path (all, filtered by target); cross-org
  isolation; missing-resource permission error; pagination.
- createBookingPolicy mutation: each target type; duplicate-target rejection;
  exactly-one-target validation; cross-org lookup of calendar/group; audit.
- updateBookingPolicy mutation: rule fields updated; policy not found;
  cross-org isolation.
- deleteBookingPolicy mutation: idempotent no-op (absent policy); actual
  delete; audit on writes.
- Auth: unauthenticated and wrong-resource token are rejected uniformly.
"""

from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.factories import create_booking_policy
from calendar_integration.models import BookingPolicy, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# GraphQL operation strings
# ---------------------------------------------------------------------------

BOOKING_POLICIES_QUERY = """
query BookingPolicies(
    $calendarId: Int,
    $membershipUserId: Int,
    $calendarGroupId: Int,
    $isOrganizationDefault: Boolean,
    $offset: Int,
    $limit: Int
) {
    bookingPolicies(
        calendarId: $calendarId,
        membershipUserId: $membershipUserId,
        calendarGroupId: $calendarGroupId,
        isOrganizationDefault: $isOrganizationDefault,
        offset: $offset,
        limit: $limit
    ) {
        id
        calendarId
        membershipUserId
        calendarGroupId
        isOrganizationDefault
        leadTimeSeconds
        maxHorizonSeconds
        bufferBeforeSeconds
        bufferAfterSeconds
        created
        modified
    }
}
"""

CREATE_BOOKING_POLICY_MUTATION = """
mutation CreateBookingPolicy($input: CreateBookingPolicyInput!) {
    createBookingPolicy(input: $input) {
        success
        errorMessage
        policy {
            id
            calendarId
            membershipUserId
            calendarGroupId
            isOrganizationDefault
            leadTimeSeconds
            maxHorizonSeconds
            bufferBeforeSeconds
            bufferAfterSeconds
        }
    }
}
"""

UPDATE_BOOKING_POLICY_MUTATION = """
mutation UpdateBookingPolicy($input: UpdateBookingPolicyInput!) {
    updateBookingPolicy(input: $input) {
        success
        errorMessage
        policy {
            id
            leadTimeSeconds
            maxHorizonSeconds
            bufferBeforeSeconds
            bufferAfterSeconds
        }
    }
}
"""

DELETE_BOOKING_POLICY_MUTATION = """
mutation DeleteBookingPolicy($input: DeleteBookingPolicyInput!) {
    deleteBookingPolicy(input: $input) {
        success
        errorMessage
    }
}
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _setup_org_and_token(
    resources: list[str] | None = None,
    integration_name: str = "test_bp_integration",
) -> tuple[Organization, object, str, PublicAPIAuthService]:
    """Return (org, system_user, token, auth_service) with the given resource grants."""
    if resources is None:
        resources = [PublicAPIResources.BOOKING_POLICY]
    org = baker.make(Organization, name="BP Test Org")
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=integration_name, organization=org
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return org, system_user, token, auth_service


def _post_graphql(
    operation: str,
    system_user,
    token: str,
    auth_service: PublicAPIAuthService,
    variables: dict,
):
    """Post a GraphQL operation and return the response."""
    from di_core.containers import container

    assert container is not None  # noqa: S101
    client = APIClient()
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": operation, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


def _post_graphql_anon(operation: str, variables: dict):
    """Post a GraphQL operation without authentication."""
    client = APIClient()
    return client.post(
        "/graphql/",
        data={"query": operation, "variables": variables},
        format="json",
    )


# ---------------------------------------------------------------------------
# bookingPolicies query
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPoliciesQuery:
    """Integration tests for the bookingPolicies query."""

    def test_list_all_policies(self):
        """All policies for the caller's org are returned."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        create_booking_policy(calendar=cal, lead_time_seconds=300)
        create_booking_policy(
            organization=org, is_organization_default=True, max_horizon_seconds=7200
        )

        response = _post_graphql(BOOKING_POLICIES_QUERY, system_user, token, auth_service, {})
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        policies = data["data"]["bookingPolicies"]
        assert len(policies) == 2

    def test_filter_by_calendar_id(self):
        """Filtering by calendarId returns only the matching policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal1 = baker.make(Calendar, organization=org, external_id="cal-filter-1")
        cal2 = baker.make(Calendar, organization=org, external_id="cal-filter-2")
        create_booking_policy(calendar=cal1, lead_time_seconds=60)
        create_booking_policy(calendar=cal2, lead_time_seconds=120)

        response = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"calendarId": cal1.id},
        )
        data = response.json()
        assert "errors" not in data, data.get("errors")
        policies = data["data"]["bookingPolicies"]
        assert len(policies) == 1
        assert policies[0]["calendarId"] == cal1.id
        assert policies[0]["leadTimeSeconds"] == 60

    def test_filter_by_is_organization_default(self):
        """Filtering by isOrganizationDefault=True returns only the default policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        create_booking_policy(calendar=cal)
        create_booking_policy(organization=org, is_organization_default=True, lead_time_seconds=900)

        response = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"isOrganizationDefault": True},
        )
        data = response.json()
        assert "errors" not in data, data.get("errors")
        policies = data["data"]["bookingPolicies"]
        assert len(policies) == 1
        assert policies[0]["isOrganizationDefault"] is True
        assert policies[0]["leadTimeSeconds"] == 900

    def test_filter_by_membership_user_id(self):
        """Filtering by membershipUserId returns only the matching policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        user = UserFactory().create_user()
        baker.make(OrganizationMembership, user=user, organization=org)
        create_booking_policy(membership_user_id=user.id, organization=org, lead_time_seconds=180)
        create_booking_policy(organization=org, is_organization_default=True)

        response = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"membershipUserId": user.id},
        )
        data = response.json()
        assert "errors" not in data, data.get("errors")
        policies = data["data"]["bookingPolicies"]
        assert len(policies) == 1
        assert policies[0]["membershipUserId"] == user.id

    def test_filter_by_calendar_group_id(self):
        """Filtering by calendarGroupId returns only the matching policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        group = baker.make(CalendarGroup, organization=org)
        create_booking_policy(calendar_group=group, buffer_before_seconds=600)
        create_booking_policy(organization=org, is_organization_default=True)

        response = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"calendarGroupId": group.id},
        )
        data = response.json()
        assert "errors" not in data, data.get("errors")
        policies = data["data"]["bookingPolicies"]
        assert len(policies) == 1
        assert policies[0]["calendarGroupId"] == group.id

    def test_cross_org_isolation(self):
        """Policies from another org are never returned."""
        _org, system_user, token, auth_service = _setup_org_and_token()
        other_org = baker.make(Organization, name="Other Org")
        other_cal = baker.make(Calendar, organization=other_org)
        create_booking_policy(calendar=other_cal)

        # Caller's org has no policies.
        response = _post_graphql(BOOKING_POLICIES_QUERY, system_user, token, auth_service, {})
        data = response.json()
        assert "errors" not in data, data.get("errors")
        assert data["data"]["bookingPolicies"] == []

    def test_missing_resource_permission_denied(self):
        """A token without BOOKING_POLICY resource is rejected."""
        org = baker.make(Organization, name="No BP Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_bp", organization=org
        )
        # Only give CALENDAR, not BOOKING_POLICY
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        client = APIClient()
        from di_core.containers import container

        assert container is not None  # noqa: S101
        with container.public_api_auth_service.override(auth_service):
            response = client.post(
                "/graphql/",
                data={"query": BOOKING_POLICIES_QUERY, "variables": {}},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        # Should return errors (permission denied)
        assert data.get("errors") or data["data"].get("bookingPolicies") is None

    def test_unauthenticated_rejected(self):
        """An unauthenticated request is rejected."""
        response = _post_graphql_anon(BOOKING_POLICIES_QUERY, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors") or data["data"].get("bookingPolicies") is None

    def test_pagination(self):
        """Offset and limit slice the result set."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cals = [
            baker.make(Calendar, organization=org, external_id=f"pag-cal-{i}") for i in range(5)
        ]
        for cal in cals:
            create_booking_policy(calendar=cal)

        response = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"offset": 0, "limit": 3},
        )
        data = response.json()
        assert "errors" not in data, data.get("errors")
        assert len(data["data"]["bookingPolicies"]) == 3

        response2 = _post_graphql(
            BOOKING_POLICIES_QUERY,
            system_user,
            token,
            auth_service,
            {"offset": 3, "limit": 3},
        )
        data2 = response2.json()
        assert "errors" not in data2, data2.get("errors")
        assert len(data2["data"]["bookingPolicies"]) == 2


# ---------------------------------------------------------------------------
# createBookingPolicy mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateBookingPolicyMutation:
    """Integration tests for the createBookingPolicy mutation."""

    def test_create_with_calendar_target(self):
        """Create a calendar-scoped policy and verify the response."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "calendarId": cal.id,
                    "leadTimeSeconds": 300,
                    "maxHorizonSeconds": 86400,
                    "bufferBeforeSeconds": 600,
                    "bufferAfterSeconds": 900,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["createBookingPolicy"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        policy = result["policy"]
        assert policy["calendarId"] == cal.id
        assert policy["leadTimeSeconds"] == 300
        assert policy["maxHorizonSeconds"] == 86400
        assert policy["bufferBeforeSeconds"] == 600
        assert policy["bufferAfterSeconds"] == 900
        assert policy["isOrganizationDefault"] is False

        # Verify DB state
        db_policy = BookingPolicy.objects.filter_by_organization(org.id).get(id=int(policy["id"]))
        assert db_policy.calendar_fk_id == cal.id
        assert db_policy.lead_time_seconds == 300

    def test_create_with_organization_default(self):
        """Create an org-default policy."""
        _org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"isOrganizationDefault": True, "leadTimeSeconds": 600}},
        )

        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["createBookingPolicy"]
        assert result["success"] is True
        policy = result["policy"]
        assert policy["isOrganizationDefault"] is True
        assert policy["calendarId"] is None

    def test_create_with_calendar_group_target(self):
        """Create a calendar-group-scoped policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        group = baker.make(CalendarGroup, organization=org)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"calendarGroupId": group.id, "bufferBeforeSeconds": 300}},
        )

        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["createBookingPolicy"]
        assert result["success"] is True
        assert result["policy"]["calendarGroupId"] == group.id

    def test_create_with_membership_user_id(self):
        """Create a membership-scoped policy."""
        org, system_user, token, auth_service = _setup_org_and_token()
        user = UserFactory().create_user()
        baker.make(OrganizationMembership, user=user, organization=org)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"membershipUserId": user.id, "leadTimeSeconds": 120}},
        )

        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["createBookingPolicy"]
        assert result["success"] is True
        assert result["policy"]["membershipUserId"] == user.id

    def test_create_duplicate_calendar_policy_rejected(self):
        """Creating a second policy for the same calendar returns a GraphQL error."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        create_booking_policy(calendar=cal)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"calendarId": cal.id}},
        )

        data = response.json()
        # Must surface an error for the duplicate
        assert data.get("errors"), "Expected a GraphQL error for duplicate policy"
        assert any("already exists" in e["message"] for e in data["errors"])

    def test_create_duplicate_org_default_rejected(self):
        """Creating a second org-default policy returns a GraphQL error."""
        org, system_user, token, auth_service = _setup_org_and_token()
        create_booking_policy(organization=org, is_organization_default=True)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"isOrganizationDefault": True}},
        )

        data = response.json()
        assert data.get("errors"), "Expected a GraphQL error for duplicate org-default policy"

    def test_create_zero_targets_rejected(self):
        """Passing no target returns a GraphQL error (exactly-one-target validation)."""
        _org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            # All targets absent — violates the exactly-one rule
            {"input": {}},
        )

        data = response.json()
        assert data.get("errors"), "Expected a GraphQL error for zero targets"

    def test_create_calendar_not_found_in_org(self):
        """Passing a calendar_id that belongs to another org returns a GraphQL error."""
        _org, system_user, token, auth_service = _setup_org_and_token()
        other_org = baker.make(Organization, name="Other Org")
        other_cal = baker.make(Calendar, organization=other_org)

        response = _post_graphql(
            CREATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"calendarId": other_cal.id}},
        )

        data = response.json()
        assert data.get("errors"), "Expected a GraphQL error for cross-org calendar"

    def test_create_audited(self, django_capture_on_commit_callbacks):
        """A CREATE audit record is enqueued on successful creation."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                _post_graphql(
                    CREATE_BOOKING_POLICY_MUTATION,
                    system_user,
                    token,
                    auth_service,
                    {"input": {"calendarId": cal.id, "leadTimeSeconds": 60}},
                )

        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "create"
        assert "BookingPolicy" in payload["subject"]["subject_type"]

    def test_create_missing_resource_denied(self):
        """A token without BOOKING_POLICY resource cannot create a policy."""
        org = baker.make(Organization, name="No BP Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_bp_create", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )
        cal = baker.make(Calendar, organization=org)

        client = APIClient()
        from di_core.containers import container

        assert container is not None  # noqa: S101
        with container.public_api_auth_service.override(auth_service):
            response = client.post(
                "/graphql/",
                data={
                    "query": CREATE_BOOKING_POLICY_MUTATION,
                    "variables": {"input": {"calendarId": cal.id}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        data = response.json()
        assert data.get("errors") or data["data"].get("createBookingPolicy") is None


# ---------------------------------------------------------------------------
# updateBookingPolicy mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateBookingPolicyMutation:
    """Integration tests for the updateBookingPolicy mutation."""

    def test_update_rule_fields(self):
        """Rule fields are updated; target fields are unchanged."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        policy = create_booking_policy(
            calendar=cal,
            lead_time_seconds=60,
            max_horizon_seconds=0,
            buffer_before_seconds=0,
            buffer_after_seconds=0,
        )

        response = _post_graphql(
            UPDATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "policyId": policy.id,
                    "leadTimeSeconds": 120,
                    "maxHorizonSeconds": 3600,
                    "bufferBeforeSeconds": 300,
                    "bufferAfterSeconds": 600,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["updateBookingPolicy"]
        assert result["success"] is True
        p = result["policy"]
        assert p["leadTimeSeconds"] == 120
        assert p["maxHorizonSeconds"] == 3600
        assert p["bufferBeforeSeconds"] == 300
        assert p["bufferAfterSeconds"] == 600

        policy.refresh_from_db()
        assert policy.lead_time_seconds == 120

    def test_update_partial_fields_only(self):
        """Omitting a field leaves it unchanged (partial update semantics)."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        policy = create_booking_policy(
            calendar=cal,
            lead_time_seconds=300,
            buffer_before_seconds=600,
        )

        response = _post_graphql(
            UPDATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            # Only update buffer_before_seconds; lead_time_seconds omitted.
            {"input": {"policyId": policy.id, "bufferBeforeSeconds": 900}},
        )

        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["updateBookingPolicy"]
        assert result["success"] is True
        p = result["policy"]
        # bufferBefore updated
        assert p["bufferBeforeSeconds"] == 900
        # leadTime unchanged
        assert p["leadTimeSeconds"] == 300

    def test_update_not_found_returns_error(self):
        """Updating a non-existent policy returns a GraphQL error."""
        _org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_graphql(
            UPDATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"policyId": 999999, "leadTimeSeconds": 60}},
        )

        data = response.json()
        assert data.get("errors"), "Expected a GraphQL error for missing policy"

    def test_update_cross_org_isolation(self):
        """Updating a policy from another org returns a GraphQL error (no existence leak)."""
        _org, system_user, token, auth_service = _setup_org_and_token()
        other_org = baker.make(Organization, name="Other Org")
        other_cal = baker.make(Calendar, organization=other_org)
        other_policy = create_booking_policy(calendar=other_cal)

        response = _post_graphql(
            UPDATE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"policyId": other_policy.id, "leadTimeSeconds": 60}},
        )

        data = response.json()
        assert data.get("errors"), "Expected a GraphQL error for cross-org policy"

    def test_update_audited(self, django_capture_on_commit_callbacks):
        """An UPDATE audit record is enqueued on successful update."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=0)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                _post_graphql(
                    UPDATE_BOOKING_POLICY_MUTATION,
                    system_user,
                    token,
                    auth_service,
                    {"input": {"policyId": policy.id, "leadTimeSeconds": 300}},
                )

        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "update"
        assert "BookingPolicy" in payload["subject"]["subject_type"]


# ---------------------------------------------------------------------------
# deleteBookingPolicy mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteBookingPolicyMutation:
    """Integration tests for the deleteBookingPolicy mutation."""

    def test_delete_existing_policy(self):
        """Deleting an existing policy removes it from the DB."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        policy = create_booking_policy(calendar=cal)

        response = _post_graphql(
            DELETE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"policyId": policy.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["deleteBookingPolicy"]
        assert result["success"] is True

        assert (
            not BookingPolicy.objects.filter_by_organization(org.id).filter(id=policy.id).exists()
        )

    def test_delete_absent_is_idempotent(self):
        """Deleting a policy id that doesn't exist returns success (idempotent no-op)."""
        _org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_graphql(
            DELETE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"policyId": 999999}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["deleteBookingPolicy"]
        assert result["success"] is True

    def test_delete_cross_org_is_idempotent_no_op(self):
        """Deleting a policy from another org is treated as absent (idempotent no-op)."""
        _org, system_user, token, auth_service = _setup_org_and_token()
        other_org = baker.make(Organization, name="Other Org")
        other_cal = baker.make(Calendar, organization=other_org)
        other_policy = create_booking_policy(calendar=other_cal)

        # Pass the other org's policy id; the org-scoped lookup won't find it.
        response = _post_graphql(
            DELETE_BOOKING_POLICY_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"policyId": other_policy.id}},
        )

        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["deleteBookingPolicy"]
        # Idempotent no-op — no error, other org's policy untouched.
        assert result["success"] is True
        assert (
            BookingPolicy.objects.filter_by_organization(other_org.id)
            .filter(id=other_policy.id)
            .exists()
        )

    def test_delete_audited(self, django_capture_on_commit_callbacks):
        """A DELETE audit record is enqueued on actual deletion."""
        org, system_user, token, auth_service = _setup_org_and_token()
        cal = baker.make(Calendar, organization=org)
        policy = create_booking_policy(calendar=cal)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                _post_graphql(
                    DELETE_BOOKING_POLICY_MUTATION,
                    system_user,
                    token,
                    auth_service,
                    {"input": {"policyId": policy.id}},
                )

        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "delete"
        assert "BookingPolicy" in payload["subject"]["subject_type"]

    def test_delete_absent_not_audited(self, django_capture_on_commit_callbacks):
        """No audit record is enqueued when the policy was already absent (no-op)."""
        _org, system_user, token, auth_service = _setup_org_and_token()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                _post_graphql(
                    DELETE_BOOKING_POLICY_MUTATION,
                    system_user,
                    token,
                    auth_service,
                    {"input": {"policyId": 999999}},
                )

        # No audit task should have been dispatched.
        assert not mock_task.delay.called
