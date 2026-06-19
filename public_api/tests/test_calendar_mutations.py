"""Integration tests for createCalendar and updateCalendar mutations (Phase 6).

Covers:
- createCalendar: default is_private, explicit is_private=False, explicit is_private=True.
- updateCalendar: toggle is_private both directions, partial update (name/description only),
  omit is_private leaves privacy unchanged (both seeded directions).
- Authorization: token without the relevant write resource is denied; token with it succeeds.
  A token holding only CALENDAR (read) is denied createCalendar/updateCalendar (escalation
  prevention). A provider-scoped token cannot be granted CREATE_CALENDAR/UPDATE_CALENDAR
  because those resources are not in PROVIDER_SCOPED_RESOURCES.
- Cross-org negative: a token for org A cannot operate on a calendar in org B.
"""

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization
from public_api.constants import PROVIDER_SCOPED_RESOURCES, PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


# ---------------------------------------------------------------------------
# GraphQL strings
# ---------------------------------------------------------------------------

CREATE_CALENDAR_MUTATION = """
mutation CreateCalendar($input: CreateCalendarInput!) {
    createCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            name
            description
            calendarType
            isPrivate
        }
    }
}
"""

UPDATE_CALENDAR_MUTATION = """
mutation UpdateCalendar($input: UpdateCalendarInput!) {
    updateCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            name
            description
            calendarType
            isPrivate
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _setup_org_and_token(
    resources: list[str] | None = None,
    integration_name: str = "test_integration",
) -> tuple[Organization, object, str, PublicAPIAuthService]:
    """Return (org, system_user, token, auth_service) with the given resource grants."""
    if resources is None:
        resources = [PublicAPIResources.CREATE_CALENDAR]
    org = baker.make(Organization, name="Test Org")
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=integration_name, organization=org
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return org, system_user, token, auth_service


def _post_mutation(
    mutation: str,
    system_user,
    token: str,
    auth_service: PublicAPIAuthService,
    variables: dict,
):
    """Post a GraphQL mutation and return the response."""
    from di_core.containers import container

    assert container is not None  # noqa: S101
    client = APIClient()
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": mutation, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


# ---------------------------------------------------------------------------
# createCalendar tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarMutation:
    """Integration tests for the createCalendar mutation."""

    def test_create_calendar_defaults_is_private_true(self):
        """Omitting is_private creates a private calendar (accepts_public_scheduling=False)."""
        org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "name": "My Calendar"}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendar"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["calendar"] is not None
        assert result["calendar"]["name"] == "My Calendar"
        assert result["calendar"]["calendarType"] == CalendarType.PERSONAL

        # Default is_private=True -> accepts_public_scheduling=False
        calendar_id = int(result["calendar"]["id"])
        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.accepts_public_scheduling is False
        assert cal.calendar_type == CalendarType.PERSONAL
        assert cal.organization == org

        # GraphQL round-trip
        assert result["calendar"]["isPrivate"] is True

    def test_create_calendar_with_is_private_false(self):
        """is_private=False creates a public calendar (accepts_public_scheduling=True)."""
        org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Public Calendar",
                    "description": "Publicly bookable",
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendar"]
        assert result["success"] is True
        calendar_id = int(result["calendar"]["id"])

        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.accepts_public_scheduling is True
        assert result["calendar"]["isPrivate"] is False
        assert result["calendar"]["description"] == "Publicly bookable"

    def test_create_calendar_with_is_private_true(self):
        """Explicit is_private=True creates a private calendar."""
        org, system_user, token, auth_service = _setup_org_and_token()

        response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Explicit Private",
                    "isPrivate": True,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendar"]
        assert result["success"] is True
        calendar_id = int(result["calendar"]["id"])

        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.accepts_public_scheduling is False
        assert result["calendar"]["isPrivate"] is True

    def test_create_calendar_without_create_calendar_resource_denied(self):
        """A token without CREATE_CALENDAR is denied (permission class gate).

        Specifically, a token holding only CALENDAR (read) must NOT be permitted to
        create calendars — that would be a read→write privilege escalation.
        """
        # Grant only the read resource — NOT the write resource
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "name": "Should Fail"}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_calendar_write_resource_not_provider_scoped(self):
        """CREATE_CALENDAR is NOT in PROVIDER_SCOPED_RESOURCES.

        This means a per-owner scoped token cannot be granted it, so there is no
        path for a scoped token to call createCalendar. The resource restriction
        is the mechanism that closes the cross-owner write BLOCKER: scoped tokens
        are limited to PROVIDER_SCOPED_RESOURCES, and CREATE_CALENDAR is not in
        that set, so the mutation is org-wide-token-only.
        """
        assert PublicAPIResources.CREATE_CALENDAR not in PROVIDER_SCOPED_RESOURCES

    def test_create_calendar_unauthenticated_denied(self):
        """An unauthenticated call is denied."""
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_CALENDAR_MUTATION,
                "variables": {"input": {"organizationId": 1, "name": "Should Fail"}},
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0

    def test_create_calendar_org_scoping(self):
        """The calendar is created in the token's org, not the input organizationId.

        organizationId is present for client convention but the server always uses the
        token's org (resolved from public_api_organization).
        """
        org, system_user, token, auth_service = _setup_org_and_token()
        other_org = baker.make(Organization, name="Other Org")

        response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,  # deliberately different
                    "name": "Scoping Test",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendar"]
        assert result["success"] is True

        calendar_id = int(result["calendar"]["id"])
        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.organization == org
        assert cal.organization != other_org


# ---------------------------------------------------------------------------
# updateCalendar tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateCalendarMutation:
    """Integration tests for the updateCalendar mutation."""

    def _make_personal_calendar(
        self,
        org: Organization,
        name: str = "Personal Calendar",
        accepts_public_scheduling: bool = False,
    ) -> Calendar:
        return baker.make(
            Calendar,
            organization=org,
            name=name,
            calendar_type=CalendarType.PERSONAL,
            accepts_public_scheduling=accepts_public_scheduling,
        )

    def test_update_calendar_toggle_is_private_false_to_true(self):
        """Toggle is_private from False (public) to True (private)."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        cal = self._make_personal_calendar(org, accepts_public_scheduling=True)
        assert cal.accepts_public_scheduling is True

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "isPrivate": True,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendar"]
        assert result["success"] is True
        assert result["calendar"]["isPrivate"] is True

        cal.refresh_from_db()
        assert cal.accepts_public_scheduling is False

    def test_update_calendar_toggle_is_private_true_to_false(self):
        """Toggle is_private from True (private) to False (public)."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        cal = self._make_personal_calendar(org, accepts_public_scheduling=False)

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is True
        assert result["calendar"]["isPrivate"] is False

        cal.refresh_from_db()
        assert cal.accepts_public_scheduling is True

    def test_update_calendar_name_and_description(self):
        """Update name and description without touching is_private."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        cal = self._make_personal_calendar(org, accepts_public_scheduling=False)

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Updated Name",
                    "description": "New description",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is True
        assert result["calendar"]["name"] == "Updated Name"
        assert result["calendar"]["description"] == "New description"
        # Privacy unchanged
        assert result["calendar"]["isPrivate"] is True

        cal.refresh_from_db()
        assert cal.name == "Updated Name"
        assert cal.description == "New description"
        assert cal.accepts_public_scheduling is False

    def test_update_calendar_omit_is_private_leaves_unchanged_when_private(self):
        """Omitting is_private (None) leaves accepts_public_scheduling unchanged when private."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        # Seed as private
        cal = self._make_personal_calendar(org, accepts_public_scheduling=False)

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Name Only Update",
                    # is_private omitted
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is True

        cal.refresh_from_db()
        # Privacy must remain False (private) — the omit-is-no-op contract
        assert cal.accepts_public_scheduling is False
        assert result["calendar"]["isPrivate"] is True

    def test_update_calendar_omit_is_private_leaves_unchanged_when_public(self):
        """Omitting is_private (None) leaves accepts_public_scheduling unchanged when public."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        # Seed as public
        cal = self._make_personal_calendar(org, accepts_public_scheduling=True)

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Name Only Update Public",
                    # is_private omitted
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is True

        cal.refresh_from_db()
        # Privacy must remain True (public) — the omit-is-no-op contract
        assert cal.accepts_public_scheduling is True
        assert result["calendar"]["isPrivate"] is False

    def test_update_calendar_not_found(self):
        """Updating a non-existent calendar returns success=False with 'not found' message."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999999,
                    "name": "Ghost",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

    def test_update_calendar_wrong_type_rejected(self):
        """Updating a non-PERSONAL calendar returns success=False."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        # Create a RESOURCE calendar — must not be updatable via updateCalendar
        resource_cal = baker.make(
            Calendar,
            organization=org,
            name="Resource Cal",
            calendar_type=CalendarType.RESOURCE,
        )

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": resource_cal.id,
                    "name": "Should Fail",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_update_calendar_without_update_calendar_resource_denied(self):
        """A token without UPDATE_CALENDAR is denied.

        Specifically, a token holding only CALENDAR (read) must NOT be permitted to
        update calendars — that would be a read→write privilege escalation.
        """
        # Grant only the read resource — NOT the write resource
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )
        cal = self._make_personal_calendar(org)

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Should Fail",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_calendar_write_resource_not_provider_scoped(self):
        """UPDATE_CALENDAR is NOT in PROVIDER_SCOPED_RESOURCES.

        This means a per-owner scoped token cannot be granted it, so there is no
        path for a scoped token to call updateCalendar on any calendar in the org.
        The resource restriction is the mechanism that closes the cross-owner write
        BLOCKER: scoped tokens are limited to PROVIDER_SCOPED_RESOURCES, and
        UPDATE_CALENDAR is not in that set, so the mutation is org-wide-token-only.
        """
        assert PublicAPIResources.UPDATE_CALENDAR not in PROVIDER_SCOPED_RESOURCES

    def test_update_calendar_cross_org_denied(self):
        """A token for org A cannot update a calendar in org B.

        Calendar lookup is org-scoped, so a cross-org update returns 'Calendar not found.'
        — the same response as a genuinely missing calendar, revealing nothing.
        """
        _org_a, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.UPDATE_CALENDAR]
        )
        org_b = baker.make(Organization, name="Org B")
        # Create a calendar in org B
        cal_in_b = baker.make(
            Calendar,
            organization=org_b,
            name="Org B Calendar",
            calendar_type=CalendarType.PERSONAL,
        )

        response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org_b.id,
                    "calendarId": cal_in_b.id,
                    "name": "Cross-org attack",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendar"]
        assert result["success"] is False
        # Should look like a not-found, not a permission error that leaks existence
        assert result["errorMessage"] is not None

        # Confirm the calendar in org B is unchanged
        cal_in_b.refresh_from_db()
        assert cal_in_b.name == "Org B Calendar"

    def test_create_calendar_and_update_privacy_round_trip(self):
        """Create a calendar then update its privacy via updateCalendar; assert both round-trips."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.CREATE_CALENDAR, PublicAPIResources.UPDATE_CALENDAR]
        )

        # Step 1: create with default is_private (True)
        create_response = _post_mutation(
            CREATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "name": "Round-trip Calendar"}},
        )
        assert create_response.status_code == 200
        create_data = create_response.json()
        assert create_data["data"]["createCalendar"]["success"] is True
        calendar_id = int(create_data["data"]["createCalendar"]["calendar"]["id"])
        assert create_data["data"]["createCalendar"]["calendar"]["isPrivate"] is True

        # Step 2: update to is_private=False (public)
        update_response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_id,
                    "isPrivate": False,
                }
            },
        )
        assert update_response.status_code == 200
        update_data = update_response.json()
        assert update_data["data"]["updateCalendar"]["success"] is True
        assert update_data["data"]["updateCalendar"]["calendar"]["isPrivate"] is False

        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.accepts_public_scheduling is True

        # Step 3: update back to is_private=True (private)
        revert_response = _post_mutation(
            UPDATE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_id,
                    "isPrivate": True,
                }
            },
        )
        assert revert_response.status_code == 200
        revert_data = revert_response.json()
        assert revert_data["data"]["updateCalendar"]["success"] is True
        assert revert_data["data"]["updateCalendar"]["calendar"]["isPrivate"] is True

        cal.refresh_from_db()
        assert cal.accepts_public_scheduling is False
