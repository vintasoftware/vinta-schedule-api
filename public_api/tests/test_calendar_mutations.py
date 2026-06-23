"""Integration tests for createCalendar, updateCalendar, and updateResourceCalendar mutations.

Covers:
- createCalendar: default is_private, explicit is_private=False, explicit is_private=True.
- updateCalendar: toggle is_private both directions, partial update (name/description only),
  omit is_private leaves privacy unchanged (both seeded directions).
- updateResourceCalendar: happy path, capacity three states (omit/null/int), name/description/
  is_private/manage_available_windows/visibility edits, synced calendar rejected, wrong-type
  rejected, cross-org denied, input.organization_id ignored (token org wins), unauthenticated
  denied, token without grant denied.
- Authorization: token without the relevant write resource is denied; token with it succeeds.
  A token holding only CALENDAR (read) is denied createCalendar/updateCalendar (escalation
  prevention). A provider-scoped token cannot be granted CREATE_CALENDAR/UPDATE_CALENDAR
  because those resources are not in PROVIDER_SCOPED_RESOURCES.
- Cross-org negative: a token for org A cannot operate on a calendar in org B.
"""

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType, CalendarVisibility
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


# ---------------------------------------------------------------------------
# updateResourceCalendar tests
# ---------------------------------------------------------------------------

UPDATE_RESOURCE_CALENDAR_MUTATION = """
mutation UpdateResourceCalendar($input: UpdateResourceCalendarInput!) {
    updateResourceCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            name
            description
            calendarType
            isPrivate
            capacity
        }
    }
}
"""


def _make_resource_calendar(
    org: Organization,
    name: str = "Resource Calendar",
    capacity: int | None = None,
    manage_available_windows: bool = False,
    accepts_public_scheduling: bool = False,
    provider: str = CalendarProvider.INTERNAL,
    calendar_type: str = CalendarType.RESOURCE,
) -> Calendar:
    """Create a resource calendar for tests."""
    return baker.make(
        Calendar,
        organization=org,
        name=name,
        calendar_type=calendar_type,
        provider=provider,
        capacity=capacity,
        manage_available_windows=manage_available_windows,
        accepts_public_scheduling=accepts_public_scheduling,
    )


@pytest.mark.django_db
class TestUpdateResourceCalendarMutation:
    """Integration tests for the updateResourceCalendar mutation."""

    def _setup(self) -> tuple[Organization, object, str, PublicAPIAuthService]:
        return _setup_org_and_token(resources=[PublicAPIResources.UPDATE_RESOURCE_CALENDAR])

    # --- happy path ---

    def test_happy_path_edits_name(self):
        """Token with UPDATE_RESOURCE_CALENDAR can update name of an INTERNAL RESOURCE calendar."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, name="Original Name")

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Updated Name",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["calendar"]["name"] == "Updated Name"
        assert result["calendar"]["calendarType"] == CalendarType.RESOURCE

        cal.refresh_from_db()
        assert cal.name == "Updated Name"

    def test_happy_path_edits_capacity_to_integer(self):
        """Providing an integer capacity sets it on the DB row."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, capacity=None)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "capacity": 10,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["capacity"] == 10

        cal.refresh_from_db()
        assert cal.capacity == 10

    def test_capacity_explicit_null_clears_it(self):
        """Providing explicit null clears the capacity (sets to unlimited)."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, capacity=5)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "capacity": None,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["capacity"] is None

        cal.refresh_from_db()
        assert cal.capacity is None

    def test_capacity_omitted_leaves_unchanged(self):
        """Omitting capacity entirely leaves the existing value unchanged."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, capacity=7)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "name": "Name Only",
                    # capacity omitted — must leave 7 intact
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["capacity"] == 7

        cal.refresh_from_db()
        assert cal.capacity == 7

    def test_edits_description(self):
        """Providing a description updates it on the calendar."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "description": "New description",
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["description"] == "New description"

        cal.refresh_from_db()
        assert cal.description == "New description"

    def test_edits_is_private_to_false(self):
        """is_private=False sets accepts_public_scheduling=True."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, accepts_public_scheduling=False)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
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

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["isPrivate"] is False

        cal.refresh_from_db()
        assert cal.accepts_public_scheduling is True

    def test_edits_is_private_to_true(self):
        """is_private=True sets accepts_public_scheduling=False."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, accepts_public_scheduling=True)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
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

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["isPrivate"] is True

        cal.refresh_from_db()
        assert cal.accepts_public_scheduling is False

    def test_edits_manage_available_windows(self):
        """manage_available_windows=True is persisted."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, manage_available_windows=False)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "manageAvailableWindows": True,
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True

        cal.refresh_from_db()
        assert cal.manage_available_windows is True

    def test_edits_visibility(self):
        """Providing a valid lowercase visibility value updates it."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org)
        original_visibility = cal.visibility

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "visibility": CalendarVisibility.INACTIVE.value,
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True

        cal.refresh_from_db()
        assert cal.visibility != original_visibility
        assert cal.visibility == CalendarVisibility.INACTIVE

    def test_invalid_visibility_rejected(self):
        """An invalid visibility value returns success=False without mutating the calendar."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org)
        original_visibility = cal.visibility

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": cal.id,
                    "visibility": "not-a-real-value",
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "visibility" in result["errorMessage"].lower()

        cal.refresh_from_db()
        assert cal.visibility == original_visibility

    # --- guard: synced calendar rejected ---

    def test_synced_calendar_rejected(self):
        """A calendar synced from Google (provider=GOOGLE) returns success=False."""
        org, system_user, token, auth_service = self._setup()
        cal = _make_resource_calendar(org, provider=CalendarProvider.GOOGLE)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
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

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        # The error must mention the external provider
        assert "provider" in result["errorMessage"].lower()

        # DB row is unchanged
        cal.refresh_from_db()
        assert cal.name == "Resource Calendar"

    # --- guard: wrong type rejected ---

    def test_wrong_type_personal_calendar_rejected(self):
        """A PERSONAL calendar returns success=False."""
        org, system_user, token, auth_service = self._setup()
        personal_cal = baker.make(
            Calendar,
            organization=org,
            name="Personal Cal",
            calendar_type=CalendarType.PERSONAL,
        )

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": personal_cal.id,
                    "name": "Should Fail",
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    # --- org scoping ---

    def test_cross_org_returns_not_found(self):
        """A calendar in another org returns success=False + 'Calendar not found.'."""
        _org_a, system_user, token, auth_service = self._setup()
        org_b = baker.make(Organization, name="Org B")
        cal_in_b = _make_resource_calendar(org_b, name="Org B Resource")

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org_b.id,  # points at org B
                    "calendarId": cal_in_b.id,
                    "name": "Cross-org attack",
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

        # DB row is untouched
        cal_in_b.refresh_from_db()
        assert cal_in_b.name == "Org B Resource"

    def test_input_organization_id_is_ignored_token_org_wins(self):
        """input.organization_id pointing at another org is ignored; token org wins."""
        org, system_user, token, auth_service = self._setup()
        other_org = baker.make(Organization, name="Other Org")
        cal = _make_resource_calendar(org, name="Real Cal")

        # Pass other_org.id as organizationId — the calendar belongs to `org`
        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,  # deliberately wrong
                    "calendarId": cal.id,
                    "name": "Token Org Update",
                }
            },
        )

        data = response.json()
        result = data["data"]["updateResourceCalendar"]
        # The server always uses the token's org; org == token org, so this succeeds
        assert result["success"] is True
        cal.refresh_from_db()
        assert cal.name == "Token Org Update"

    # --- authorization ---

    def test_token_without_grant_denied(self):
        """A token without UPDATE_RESOURCE_CALENDAR is denied at the permission class."""
        org, system_user, token, auth_service = _setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]  # read-only, not the write grant
        )
        cal = _make_resource_calendar(org)

        response = _post_mutation(
            UPDATE_RESOURCE_CALENDAR_MUTATION,
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

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_unauthenticated_denied(self):
        """An unauthenticated call is denied."""
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": UPDATE_RESOURCE_CALENDAR_MUTATION,
                "variables": {"input": {"organizationId": 1, "calendarId": 1}},
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0

    def test_grant_not_in_provider_scoped_resources(self):
        """UPDATE_RESOURCE_CALENDAR is NOT in PROVIDER_SCOPED_RESOURCES (org-wide token only)."""
        assert PublicAPIResources.UPDATE_RESOURCE_CALENDAR not in PROVIDER_SCOPED_RESOURCES
