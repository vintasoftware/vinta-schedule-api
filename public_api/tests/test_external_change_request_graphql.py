"""Integration tests for the ExternalEventChangeRequest GraphQL query and mutations.

Covers:
- externalEventChangeRequests query:
    - org-wide token sees all PENDING requests (admin semantics)
    - scoped token sees only requests resolvable by its membership
    - cross-org isolation (org B token sees nothing from org A)
    - token without EXTERNAL_EVENT_CHANGE_REQUEST resource is denied
- approveExternalEventChangeRequest mutation:
    - scoped token can approve a PENDING request it is eligible to resolve
    - ineligible membership raises GraphQLError (403 semantics)
    - non-PENDING request raises GraphQLError (409 semantics)
    - org-wide token raises GraphQLError (must be scoped)
- rejectExternalEventChangeRequest mutation:
    - scoped token can reject a PENDING request; outbound write adapter is mocked
    - ineligible membership raises GraphQLError (403 semantics)
    - non-PENDING request raises GraphQLError (409 semantics)
    - org-wide token raises GraphQLError (must be scoped)
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import MagicMock

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.factories import (
    create_event_attendance,
    create_external_event_change_request,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    ExternalEventChangeRequest,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# GraphQL query / mutation strings
# ---------------------------------------------------------------------------

_QUERY_EXTERNAL_CHANGE_REQUESTS = """
query ListExternalEventChangeRequests(
    $status: String,
    $eventId: Int,
    $offset: Int,
    $limit: Int
) {
    externalEventChangeRequests(
        status: $status,
        eventId: $eventId,
        offset: $offset,
        limit: $limit
    ) {
        id
        kind
        status
        provider
        eventId
    }
}
"""

_APPROVE_MUTATION = """
mutation ApproveExternalEventChangeRequest($id: Int!) {
    approveExternalEventChangeRequest(id: $id) {
        success
        errorMessage
        changeRequest {
            id
            status
        }
    }
}
"""

_REJECT_MUTATION = """
mutation RejectExternalEventChangeRequest($id: Int!) {
    rejectExternalEventChangeRequest(id: $id) {
        success
        errorMessage
        changeRequest {
            id
            status
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_with_member(*, is_admin: bool = False) -> tuple[Organization, OrganizationMembership]:
    """Create an organization and one active member."""
    user = UserFactory().create_user()
    org = baker.make(Organization, name=f"Org-{uuid.uuid4().hex[:6]}")
    membership = OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=OrganizationRole.ADMIN if is_admin else OrganizationRole.MEMBER,
        is_active=True,
    )
    return org, membership


def _make_calendar(org: Organization, provider: str = CalendarProvider.GOOGLE) -> Calendar:
    return baker.make(
        Calendar,
        organization=org,
        provider=provider,
        external_id=f"cal-{uuid.uuid4().hex[:8]}",
    )


_event_counter: int = 0


def _make_event(calendar: Calendar) -> CalendarEvent:
    global _event_counter
    _event_counter += 1
    return baker.make(
        CalendarEvent,
        calendar=calendar,
        organization=calendar.organization,
        title="Test Event",
        timezone="UTC",
        external_id=f"evt-{_event_counter}-{uuid.uuid4().hex[:6]}",
        start_time_tz_unaware=datetime.datetime(2025, 1, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2025, 1, 1, 11, 0),
    )


def _make_ownership(membership: OrganizationMembership, calendar: Calendar) -> CalendarOwnership:
    return CalendarOwnership.objects.create(
        membership_user_id=membership.user_id,
        calendar=calendar,
        organization=calendar.organization,
        is_default=True,
    )


def _make_social_account(
    membership: OrganizationMembership, provider: str = CalendarProvider.GOOGLE
) -> SocialAccount:
    social_account = SocialAccount.objects.create(user=membership.user, provider=provider)
    SocialToken.objects.create(
        account=social_account,
        token="fake-token",
        token_secret="fake-secret",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    return social_account


def _make_scoped_system_user(
    org: Organization,
    membership: OrganizationMembership,
    resources: list[str],
) -> tuple:
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
        organization=org,
        scoped_to_membership=membership,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def _make_org_wide_system_user(
    org: Organization,
    resources: list[str],
) -> tuple:
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"org_wide_{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def _graphql_post(
    client: APIClient,
    query: str,
    system_user,
    token: str,
    auth_service: PublicAPIAuthService,
    variables: dict,
):
    from di_core.containers import container

    assert container is not None
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExternalEventChangeRequestsQuery:
    """externalEventChangeRequests GraphQL query — eligibility, scoping, isolation."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, variables=None):
        return _graphql_post(
            self.client,
            _QUERY_EXTERNAL_CHANGE_REQUESTS,
            system_user,
            token,
            auth_service,
            variables or {},
        )

    def test_org_wide_token_sees_all_pending_requests(self):
        """An org-wide token (no scoped membership) acts as admin — sees all PENDING requests."""
        org, _admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event1 = _make_event(cal)
        event2 = _make_event(cal)

        cr1 = create_external_event_change_request(
            event=event1,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        cr2 = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        ids = {int(r["id"]) for r in data["data"]["externalEventChangeRequests"]}
        assert cr1.id in ids
        assert cr2.id in ids

    def test_scoped_token_sees_only_resolvable_requests(self):
        """A scoped token sees requests for events the acting membership can resolve."""
        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event_attended = _make_event(cal)
        event_other = _make_event(cal)

        # Attend the first event (eligibility gate)
        create_event_attendance(user=member.user, event=event_attended)

        cr_attended = create_external_event_change_request(
            event=event_attended,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        _cr_other = create_external_event_change_request(
            event=event_other,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, member, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        ids = {int(r["id"]) for r in data["data"]["externalEventChangeRequests"]}
        assert cr_attended.id in ids
        assert len(ids) == 1, f"Expected 1 result, got {len(ids)}: {ids}"

    def test_default_filter_is_pending(self):
        """Without a status param, only PENDING requests are returned."""
        org, _admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event_pending = _make_event(cal)
        event_approved = _make_event(cal)

        cr_pending = create_external_event_change_request(
            event=event_pending,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        cr_approved = create_external_event_change_request(
            event=event_approved,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        ids = {int(r["id"]) for r in data["data"]["externalEventChangeRequests"]}
        assert cr_pending.id in ids
        assert cr_approved.id not in ids

    def test_status_filter_approved(self):
        """status=approved returns only APPROVED requests."""
        org, _admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event_pending = _make_event(cal)
        event_approved = _make_event(cal)

        cr_pending = create_external_event_change_request(
            event=event_pending,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        cr_approved = create_external_event_change_request(
            event=event_approved,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, {"status": "approved"})
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        ids = {int(r["id"]) for r in data["data"]["externalEventChangeRequests"]}
        assert cr_approved.id in ids
        assert cr_pending.id not in ids

    def test_event_id_filter(self):
        """eventId filter narrows results to a specific event's requests."""
        org, _admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event1 = _make_event(cal)
        event2 = _make_event(cal)

        cr1 = create_external_event_change_request(
            event=event1,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        cr2 = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, {"eventId": event1.id})
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        ids = {int(r["id"]) for r in data["data"]["externalEventChangeRequests"]}
        assert cr1.id in ids
        assert cr2.id not in ids

    def test_cross_org_isolation(self):
        """A token from org B cannot see change requests from org A."""
        org_a, _admin_a = _make_org_with_member(is_admin=True)
        org_b, _admin_b = _make_org_with_member(is_admin=True)

        cal_a = _make_calendar(org_a)
        event_a = _make_event(cal_a)
        create_external_event_change_request(
            event=event_a,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user_b, token_b, auth_service_b = _make_org_wide_system_user(
            org_b, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user_b, token_b, auth_service_b)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        assert data["data"]["externalEventChangeRequests"] == []

    def test_token_without_resource_is_denied(self):
        """A token without EXTERNAL_EVENT_CHANGE_REQUEST resource gets a permission error."""
        org, _admin = _make_org_with_member(is_admin=True)

        # Grant a different resource, not the one needed.
        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR]
        )

        response = self._post(system_user, token, auth_service)
        assert response.status_code == 200
        data = response.json()
        # Strawberry returns permission errors in the "errors" list.
        assert "errors" in data and len(data["errors"]) > 0


# ---------------------------------------------------------------------------
# Approve mutation tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApproveExternalEventChangeRequestMutation:
    """approveExternalEventChangeRequest mutation tests."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, cr_id: int):
        return _graphql_post(
            self.client,
            _APPROVE_MUTATION,
            system_user,
            token,
            auth_service,
            {"id": cr_id},
        )

    def test_scoped_admin_can_approve_pending_request(self):
        """A scoped token for an admin member approves a PENDING request successfully."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "Updated title"},
            retained_values={"title": "Original title"},
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["approveExternalEventChangeRequest"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["changeRequest"] is not None
        assert result["changeRequest"]["status"] == ExternalEventChangeRequestStatus.APPROVED

        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.APPROVED

    def test_scoped_attendee_member_can_approve(self):
        """A scoped token for a member who attends the event can approve the request."""
        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event = _make_event(cal)
        create_event_attendance(user=member.user, event=event)

        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New title"},
            retained_values={"title": "Old title"},
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, member, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["approveExternalEventChangeRequest"]
        assert result["success"] is True

        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.APPROVED

    def test_non_attendee_member_gets_graphql_error(self):
        """A member who doesn't attend the event cannot approve → ineligible GraphQLError."""
        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event = _make_event(cal)
        # No attendance for member

        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, member, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        # approve() raises ChangeRequestIneligibleError → GraphQLError.
        # Strawberry surfaces this as data["errors"] non-empty (data["data"] may be null).
        assert "errors" in data and len(data["errors"]) > 0, data
        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.PENDING

    def test_non_pending_request_raises_graphql_error(self):
        """Approving a non-PENDING request raises a GraphQLError (409 semantics)."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        # GraphQL error in the errors list (409 semantics).
        assert "errors" in data and len(data["errors"]) > 0, data

    def test_org_wide_token_raises_graphql_error(self):
        """An org-wide (unscoped) token cannot approve — membership is required."""
        org, _admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0, data

    def test_token_without_resource_is_denied(self):
        """A scoped token without EXTERNAL_EVENT_CHANGE_REQUEST resource is denied."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.CALENDAR]
        )

        response = self._post(system_user, token, auth_service, cr.id)

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0, data
        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.PENDING


# ---------------------------------------------------------------------------
# Reject mutation tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRejectExternalEventChangeRequestMutation:
    """rejectExternalEventChangeRequest mutation tests.

    The outbound CalendarService write adapter is always mocked — no real provider calls.
    """

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, cr_id: int):
        return _graphql_post(
            self.client,
            _REJECT_MUTATION,
            system_user,
            token,
            auth_service,
            {"id": cr_id},
        )

    def _setup_reject_scenario(
        self,
        *,
        is_admin: bool = True,
        kind: str = ExternalEventChangeKind.UPDATE,
        req_status: str = ExternalEventChangeRequestStatus.PENDING,
    ) -> tuple[Organization, OrganizationMembership, ExternalEventChangeRequest]:
        """Create org, member, calendar, event, ownership, social account, and change request."""
        org, membership = _make_org_with_member(is_admin=is_admin)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(membership, cal)
        _make_social_account(membership, CalendarProvider.GOOGLE)

        cr = create_external_event_change_request(
            event=event,
            kind=kind,
            status=req_status,
            proposed_values={"title": "New title"}
            if kind == ExternalEventChangeKind.UPDATE
            else {},
            retained_values={"title": "Old title"},
        )
        return org, membership, cr

    def _mock_calendar_service(self, *, create_event_return=None):
        """Return a mock CalendarService that stubs authenticate + _get_write_adapter_for_calendar."""
        mock_write_adapter = MagicMock()
        mock_write_adapter.update_event.return_value = None
        if create_event_return is not None:
            mock_write_adapter.create_event.return_value = create_event_return

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service._get_write_adapter_for_calendar.return_value = mock_write_adapter
        return mock_calendar_service, mock_write_adapter

    def test_scoped_admin_can_reject_update_request(self):
        """A scoped admin token rejects a PENDING update request; outbound adapter is mocked."""
        from di_core.containers import container

        org, admin, cr = self._setup_reject_scenario(
            is_admin=True, kind=ExternalEventChangeKind.UPDATE
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_calendar_service, mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["rejectExternalEventChangeRequest"]
        assert result["success"] is True
        assert result["changeRequest"]["status"] == ExternalEventChangeRequestStatus.REJECTED
        mock_write_adapter.update_event.assert_called_once()

        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.REJECTED

    def test_scoped_admin_can_reject_delete_request(self):
        """A scoped admin token rejects a PENDING delete request; create_event is mocked."""
        from di_core.containers import container

        org, admin, cr = self._setup_reject_scenario(
            is_admin=True, kind=ExternalEventChangeKind.DELETE
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_created = MagicMock()
        mock_created.external_id = "new-ext-id-from-provider"
        mock_calendar_service, mock_write_adapter = self._mock_calendar_service(
            create_event_return=mock_created
        )

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["rejectExternalEventChangeRequest"]
        assert result["success"] is True
        assert result["changeRequest"]["status"] == ExternalEventChangeRequestStatus.REJECTED
        mock_write_adapter.create_event.assert_called_once()

    def test_scoped_attendee_member_can_reject(self):
        """A scoped member-attendee token can reject a request for their attended event."""
        from di_core.containers import container

        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(member, cal)
        create_event_attendance(user=member.user, event=event)
        _make_social_account(member, CalendarProvider.GOOGLE)

        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New title"},
            retained_values={"title": "Old title"},
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, member, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_calendar_service, _mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["rejectExternalEventChangeRequest"]
        assert result["success"] is True

        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.REJECTED

    def test_non_attendee_member_cannot_reject(self):
        """A member who doesn't attend the event cannot reject the change request."""
        from di_core.containers import container

        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(member, cal)
        _make_social_account(member, CalendarProvider.GOOGLE)
        # Deliberately no attendance for member

        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New title"},
            retained_values={"title": "Old title"},
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, member, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_calendar_service, _mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        # ChangeRequestIneligibleError → GraphQLError.
        # Strawberry surfaces this as data["errors"] non-empty (data["data"] may be null).
        assert "errors" in data and len(data["errors"]) > 0, data
        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.PENDING

    def test_non_pending_request_raises_graphql_error(self):
        """Rejecting a non-PENDING request raises a GraphQLError (409 semantics)."""
        from di_core.containers import container

        org, admin, cr = self._setup_reject_scenario(
            is_admin=True,
            req_status=ExternalEventChangeRequestStatus.REJECTED,
        )

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_calendar_service, _mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0, data

    def test_org_wide_token_raises_graphql_error(self):
        """An org-wide (unscoped) token cannot reject — membership is required."""
        from di_core.containers import container

        org, _admin, cr = self._setup_reject_scenario(is_admin=True)

        system_user, token, auth_service = _make_org_wide_system_user(
            org, [PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST]
        )

        mock_calendar_service, _mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0, data

    def test_token_without_resource_is_denied(self):
        """A scoped token without EXTERNAL_EVENT_CHANGE_REQUEST resource is denied."""
        from di_core.containers import container

        org, admin, cr = self._setup_reject_scenario(is_admin=True)

        system_user, token, auth_service = _make_scoped_system_user(
            org, admin, [PublicAPIResources.CALENDAR]
        )

        mock_calendar_service, _mock_write_adapter = self._mock_calendar_service()

        with container.calendar_service.override(mock_calendar_service):
            response = self._post(
                system_user,
                token,
                auth_service,
                cr.id,
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0, data
        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.PENDING
