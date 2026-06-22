"""Integration tests for the ExternalEventChangeRequest REST API.

Covers:
- GET /change-requests/ list (default PENDING, eligibility scoping, filters, cross-org isolation)
- POST /change-requests/{id}/approve/ (happy path, ineligible, non-pending)
- POST /change-requests/{id}/reject/ (happy path with mocked outbound adapter, ineligible, non-pending)
- Unauthenticated access returns 401
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

from django.urls import reverse

import pytest
from rest_framework import status
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
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_with_member(*, is_admin: bool = False) -> tuple[Organization, OrganizationMembership]:
    """Create an organization and an active membership for a fresh user."""
    from model_bakery import baker

    user = UserFactory().create_user()
    org = baker.make(Organization, name="Test Org")
    membership = OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=OrganizationRole.ADMIN if is_admin else OrganizationRole.MEMBER,
        is_active=True,
    )
    return org, membership


def _make_calendar(org: Organization, provider: str = CalendarProvider.GOOGLE) -> Calendar:
    from model_bakery import baker

    return baker.make(
        Calendar,
        organization=org,
        provider=provider,
        external_id="cal-ext-id",
    )


_event_counter: int = 0


def _make_event(calendar: Calendar) -> CalendarEvent:
    """Create a CalendarEvent with a valid timezone and unique external_id."""
    global _event_counter
    _event_counter += 1

    from model_bakery import baker

    return baker.make(
        CalendarEvent,
        calendar=calendar,
        organization=calendar.organization,
        title="Test Event",
        timezone="UTC",
        external_id=f"event-ext-id-{_event_counter}",
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


def _auth_client(membership: OrganizationMembership) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=membership.user)
    return client


LIST_URL = "api:ChangeRequests-list"
APPROVE_URL = "api:ChangeRequests-approve"
REJECT_URL = "api:ChangeRequests-reject"


# ---------------------------------------------------------------------------
# List endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestChangeRequestList:
    """GET /change-requests/ — list, filtering, eligibility, isolation."""

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        response = client.get(reverse(LIST_URL))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_member_sees_only_own_events_requests(self):
        """Non-admin members only see requests for events they attend."""
        org, member_ship = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event = _make_event(cal)

        # Another event in the same org that the member does NOT attend.
        other_event = _make_event(cal)

        # Create a PENDING request for the attended event.
        attended_request = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New title"},
            retained_values={"title": "Old title"},
        )
        # Create a PENDING request for the other event (member doesn't attend).
        create_external_event_change_request(
            event=other_event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        # Attach the member as an attendee of the first event.
        create_event_attendance(user=member_ship.user, event=event)

        client = _auth_client(member_ship)
        response = client.get(reverse(LIST_URL))

        assert response.status_code == status.HTTP_200_OK
        ids = [r["id"] for r in response.data["results"]]
        assert attended_request.id in ids
        # The request for the event they don't attend must not be visible.
        assert len(ids) == 1

    def test_admin_sees_all_org_requests(self):
        """Admins see all change requests in their organization."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event1 = _make_event(cal)
        event2 = _make_event(cal)

        req1 = create_external_event_change_request(
            event=event1,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        req2 = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(admin)
        response = client.get(reverse(LIST_URL))

        assert response.status_code == status.HTTP_200_OK
        ids = {r["id"] for r in response.data["results"]}
        assert req1.id in ids
        assert req2.id in ids

    def test_default_filter_is_pending(self):
        """List returns only PENDING by default; non-PENDING are excluded."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)

        pending_req = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        # A second event with a non-PENDING request (status=APPROVED).
        event2 = _make_event(cal)
        approved_req = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        client = _auth_client(admin)
        response = client.get(reverse(LIST_URL))

        assert response.status_code == status.HTTP_200_OK
        ids = {r["id"] for r in response.data["results"]}
        assert pending_req.id in ids
        assert approved_req.id not in ids

    def test_status_filter_returns_matching_requests(self):
        """?status=approved returns only APPROVED requests."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event1 = _make_event(cal)
        event2 = _make_event(cal)

        pending_req = create_external_event_change_request(
            event=event1,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        approved_req = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        client = _auth_client(admin)
        response = client.get(reverse(LIST_URL), {"status": "approved"})

        assert response.status_code == status.HTTP_200_OK
        ids = {r["id"] for r in response.data["results"]}
        assert approved_req.id in ids
        assert pending_req.id not in ids

    def test_event_filter_returns_only_matching_event_requests(self):
        """?event=<id> narrows to requests for that specific event."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event1 = _make_event(cal)
        event2 = _make_event(cal)

        req1 = create_external_event_change_request(
            event=event1,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        req2 = create_external_event_change_request(
            event=event2,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(admin)
        response = client.get(reverse(LIST_URL), {"event": event1.id})

        assert response.status_code == status.HTTP_200_OK
        ids = {r["id"] for r in response.data["results"]}
        assert req1.id in ids
        assert req2.id not in ids

    def test_cross_org_isolation(self):
        """A member of another org cannot see requests from this org."""
        org_a, _ = _make_org_with_member(is_admin=True)
        _org_b, member_b = _make_org_with_member(is_admin=True)

        cal_a = _make_calendar(org_a)
        event_a = _make_event(cal_a)
        create_external_event_change_request(
            event=event_a,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        # Member of org B should see nothing from org A.
        client = _auth_client(member_b)
        response = client.get(reverse(LIST_URL))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["results"] == []


# ---------------------------------------------------------------------------
# Approve action tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestChangeRequestApprove:
    """POST /change-requests/{id}/approve/ — eligibility + happy path."""

    def test_unauthenticated_returns_401(self):
        org, _ = _make_org_with_member()
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )
        client = APIClient()
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_admin_can_approve(self):
        """An admin can approve any PENDING request in their org."""
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

        client = _auth_client(admin)
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == ExternalEventChangeRequestStatus.APPROVED
        assert response.data["event_id"] == event.id
        assert response.data["kind"] == ExternalEventChangeKind.UPDATE
        assert response.data["proposed_values"] == {"title": "Updated title"}
        assert response.data["retained_values"] == {"title": "Original title"}
        cr.refresh_from_db()
        assert cr.status == ExternalEventChangeRequestStatus.APPROVED

    def test_member_attendee_can_approve(self):
        """A member who attends the event can approve the change request."""
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

        client = _auth_client(member)
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == ExternalEventChangeRequestStatus.APPROVED

    def test_non_attendee_member_gets_404(self):
        """A member who does not attend the event cannot see the request → 404."""
        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(member)
        # The request is not visible to the member (no attendance) — get_object() 404s.
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_pending_returns_409(self):
        """Approving a non-PENDING (e.g. APPROVED) request returns 409."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        client = _auth_client(admin)
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_cross_org_request_not_found(self):
        """An admin from another org cannot see or approve a request."""
        org_a, _ = _make_org_with_member(is_admin=True)
        _org_b, admin_b = _make_org_with_member(is_admin=True)

        cal_a = _make_calendar(org_a)
        event_a = _make_event(cal_a)
        cr = create_external_event_change_request(
            event=event_a,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(admin_b)
        response = client.post(reverse(APPROVE_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Reject action tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestChangeRequestReject:
    """POST /change-requests/{id}/reject/ — outbound adapter mocked."""

    def _setup_reject_scenario(
        self,
        *,
        is_admin: bool = True,
        kind: str = ExternalEventChangeKind.UPDATE,
        req_status: str = ExternalEventChangeRequestStatus.PENDING,
    ) -> tuple[OrganizationMembership, ExternalEventChangeRequest]:
        """Create org, member, calendar, event, ownership, and a change request."""
        from allauth.socialaccount.models import SocialAccount, SocialToken

        org, membership = _make_org_with_member(is_admin=is_admin)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(membership, cal)

        # SocialAccount for the calendar owner (same user as the test actor here).
        social_account = SocialAccount.objects.create(
            user=membership.user, provider=CalendarProvider.GOOGLE
        )
        SocialToken.objects.create(
            account=social_account,
            token="fake-token",
            token_secret="fake-secret",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        cr = create_external_event_change_request(
            event=event,
            kind=kind,
            status=req_status,
            proposed_values={"title": "New title"}
            if kind == ExternalEventChangeKind.UPDATE
            else {},
            retained_values={"title": "Old title"},
        )
        return membership, cr

    def test_unauthenticated_returns_401(self):
        _, cr = self._setup_reject_scenario()
        client = APIClient()
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_admin_can_reject_update_request(self):
        """An admin can reject a PENDING update request; mocked outbound call fires."""
        membership, cr = self._setup_reject_scenario(
            is_admin=True, kind=ExternalEventChangeKind.UPDATE
        )

        from di_core.containers import container

        # Mock the write adapter returned by CalendarService.
        mock_write_adapter = MagicMock()
        mock_write_adapter.update_event.return_value = None

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service._get_write_adapter_for_calendar.return_value = mock_write_adapter

        client = _auth_client(membership)
        with container.calendar_service.override(mock_calendar_service):
            response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["status"] == ExternalEventChangeRequestStatus.REJECTED
        mock_write_adapter.update_event.assert_called_once()

    def test_admin_can_reject_delete_request(self):
        """An admin can reject a PENDING delete request; mocked create_event fires."""
        membership, cr = self._setup_reject_scenario(
            is_admin=True, kind=ExternalEventChangeKind.DELETE
        )

        from di_core.containers import container

        new_ext_id = "new-event-id-from-provider"
        mock_created = MagicMock()
        mock_created.external_id = new_ext_id

        mock_write_adapter = MagicMock()
        mock_write_adapter.create_event.return_value = mock_created

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service._get_write_adapter_for_calendar.return_value = mock_write_adapter

        client = _auth_client(membership)
        with container.calendar_service.override(mock_calendar_service):
            response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["status"] == ExternalEventChangeRequestStatus.REJECTED
        mock_write_adapter.create_event.assert_called_once()

    def test_member_attendee_can_reject(self):
        """A member-attendee can reject a change request on their event."""
        org, member = _make_org_with_member(is_admin=False)
        from allauth.socialaccount.models import SocialAccount, SocialToken

        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(member, cal)
        create_event_attendance(user=member.user, event=event)

        social_account = SocialAccount.objects.create(
            user=member.user, provider=CalendarProvider.GOOGLE
        )
        SocialToken.objects.create(
            account=social_account,
            token="token",
            token_secret="secret",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New"},
            retained_values={"title": "Old"},
        )

        from di_core.containers import container

        mock_write_adapter = MagicMock()
        mock_write_adapter.update_event.return_value = None
        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service._get_write_adapter_for_calendar.return_value = mock_write_adapter

        client = _auth_client(member)
        with container.calendar_service.override(mock_calendar_service):
            response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["status"] == ExternalEventChangeRequestStatus.REJECTED

    def test_non_attendee_member_gets_404(self):
        """A member who does not attend the event cannot see the request → 404."""
        org, member = _make_org_with_member(is_admin=False)
        cal = _make_calendar(org)
        event = _make_event(cal)
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(member)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_pending_returns_409(self):
        """Rejecting a non-PENDING request returns 409."""
        membership, cr = self._setup_reject_scenario(
            is_admin=True,
            req_status=ExternalEventChangeRequestStatus.REJECTED,
        )

        from di_core.containers import container

        mock_write_adapter = MagicMock()
        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service._get_write_adapter_for_calendar.return_value = mock_write_adapter

        client = _auth_client(membership)
        with container.calendar_service.override(mock_calendar_service):
            response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_reject_no_calendar_owner_returns_400(self):
        """PENDING request whose calendar has no CalendarOwnership → reject returns 400."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        # Deliberately NOT creating a CalendarOwnership for this calendar.
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New"},
            retained_values={"title": "Old"},
        )

        client = _auth_client(admin)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "owner" in response.data["detail"].lower()

    def test_reject_no_social_account_returns_400(self):
        """Owner exists but no SocialAccount for the provider → reject returns 400."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        _make_ownership(admin, cal)
        # Deliberately NOT creating a SocialAccount for the owner.
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New"},
            retained_values={"title": "Old"},
        )

        client = _auth_client(admin)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert CalendarProvider.GOOGLE in response.data["detail"]

    def test_reject_non_pending_returns_409_without_auth(self):
        """A non-PENDING (APPROVED) request → reject returns 409, no owner/account setup needed."""
        org, admin = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org, provider=CalendarProvider.GOOGLE)
        event = _make_event(cal)
        # No CalendarOwnership, no SocialAccount — the 409 guard fires before auth work.
        cr = create_external_event_change_request(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.APPROVED,
        )

        client = _auth_client(admin)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_reject_deleted_event_returns_403(self):
        """A PENDING request with event=None (event was deleted) → reject returns 403."""
        org, admin = _make_org_with_member(is_admin=True)
        # Create a change request with event_fk=None (simulates a deleted event).
        cr = ExternalEventChangeRequest.objects.create(
            organization=org,
            event_fk=None,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
            proposed_values={"title": "New"},
            proposed_payload={},
            retained_values={"title": "Old"},
            provider=CalendarProvider.GOOGLE,
        )

        client = _auth_client(admin)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_cross_org_request_not_found(self):
        """An admin from another org cannot see or reject a request."""
        org_a, _ = _make_org_with_member(is_admin=True)
        _org_b, admin_b = _make_org_with_member(is_admin=True)

        cal_a = _make_calendar(org_a)
        event_a = _make_event(cal_a)
        cr = create_external_event_change_request(
            event=event_a,
            kind=ExternalEventChangeKind.UPDATE,
            status=ExternalEventChangeRequestStatus.PENDING,
        )

        client = _auth_client(admin_b)
        response = client.post(reverse(REJECT_URL, kwargs={"pk": cr.id}))
        assert response.status_code == status.HTTP_404_NOT_FOUND
