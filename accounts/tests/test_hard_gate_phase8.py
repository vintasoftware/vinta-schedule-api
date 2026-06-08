"""
Phase 8 — Hard-gate regression tests.

Ensures that authenticated users with NO OrganizationMembership (gated / onboarding
state) are uniformly refused (empty queryset or permission denial) at every
tenant-scoped REST endpoint rather than crashing with a 500.

Also confirms:
- Onboarding endpoints (create-org, accept-invite) remain reachable for gated users.
- Account-level self-management endpoints are not over-blocked.

Pattern: mirror the membership-less user helper from test_social_gated_onboarding.py.
"""

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gated_user(email: str) -> User:
    """Create an authenticated-but-membership-less user (the 'gated' state)."""
    user = baker.make(User, email=email, is_active=True)
    # Ensure Profile exists (normally created by allauth).
    Profile.objects.get_or_create(user=user, defaults={"first_name": "Gated", "last_name": "User"})
    # No OrganizationMembership row — that is the gated state.
    return user


def _gated_client(email: str) -> tuple[User, APIClient]:
    """Return (user, APIClient) for a membership-less authenticated user."""
    user = _make_gated_user(email)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, client


def _make_member(email: str) -> tuple[User, Organization, APIClient]:
    """Create a user with an ADMIN membership and return (user, org, client)."""
    user = baker.make(User, email=email, is_active=True)
    Profile.objects.get_or_create(user=user, defaults={"first_name": "Member", "last_name": "User"})
    org = baker.make(Organization, name="Member Org")
    baker.make(OrganizationMembership, user=user, organization=org, role=OrganizationRole.ADMIN)
    user.refresh_from_db()
    client = APIClient()
    client.force_authenticate(user=user)
    return user, org, client


# ---------------------------------------------------------------------------
# Onboarding surface — must remain reachable for gated users (no over-block)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnboardingSurfaceNotOverBlocked:
    """Onboarding endpoints respond for membership-less users."""

    def test_create_org_endpoint_accessible_for_gated_user(self):
        """POST /organizations/ returns 201 (not 403) for a membership-less user."""
        _, client = _gated_client("gated-org-create@test.example")
        url = reverse("api:Organizations-list")
        response = client.post(url, {"name": "My Org", "should_sync_rooms": False}, format="json")
        assert response.status_code == status.HTTP_201_CREATED, (
            f"Onboarding create-org must succeed for gated user, got "
            f"{response.status_code}: {response.json()}"
        )

    def test_accept_invite_endpoint_accessible_for_gated_user(self):
        """
        POST /invitations/accept is reachable for a gated user (it will return 400
        for an invalid token, not 403 — the endpoint itself is unblocked).
        """
        _, client = _gated_client("gated-accept@test.example")
        url = reverse("accept-invitation")
        response = client.post(url, {"token": "invalid-token"}, format="json")
        # 400 (bad token) — NOT 403 (forbidden due to no membership).
        assert response.status_code == status.HTTP_400_BAD_REQUEST, (
            f"accept-invite must be reachable for gated user (400 bad token expected), "
            f"got {response.status_code}: {response.json()}"
        )


# ---------------------------------------------------------------------------
# Calendar-integration endpoints — tenant-scoped, must refuse gated users
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarIntegrationGatedRefusal:
    """Membership-less users get empty or permission-denied, never 500."""

    def test_calendar_list_returns_empty_for_gated_user(self):
        """GET /calendar/ → 200 with empty list (CalendarViewSet.get_queryset returns none)."""
        _, client = _gated_client("gated-cal-list@test.example")
        url = reverse("api:Calendars-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Calendar list must be empty for membership-less user"
        )

    def test_calendar_events_list_returns_empty_for_gated_user(self):
        """GET /calendar-events/ → 200 with empty list (get_queryset returns none)."""
        _, client = _gated_client("gated-events-list@test.example")
        url = reverse("api:CalendarEvents-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Calendar events list must be empty for membership-less user"
        )

    def test_blocked_times_list_returns_empty_for_gated_user(self):
        """GET /blocked-times/ → 200 with empty list."""
        _, client = _gated_client("gated-blocked-list@test.example")
        url = reverse("api:BlockedTimes-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Blocked times list must be empty for membership-less user"
        )

    def test_available_times_list_returns_empty_for_gated_user(self):
        """GET /available-times/ → 200 with empty list."""
        _, client = _gated_client("gated-avail-list@test.example")
        url = reverse("api:AvailableTimes-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Available times list must be empty for membership-less user"
        )

    def test_calendar_groups_list_returns_403_for_gated_user(self):
        """
        GET /calendar-groups/ → 403 (CalendarGroupPermission requires membership
        via getattr check in has_permission).
        """
        _, client = _gated_client("gated-groups-list@test.example")
        url = reverse("api:CalendarGroups-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 for membership-less user on calendar-groups, "
            f"got {response.status_code}: {response.data}"
        )

    def test_blocked_times_expanded_returns_empty_for_gated_user(self):
        """GET /blocked-times/expanded/ → 200 empty list (no 500)."""
        _, client = _gated_client("gated-blocked-exp@test.example")
        url = reverse("api:BlockedTimes-expanded")
        response = client.get(
            url,
            {
                "calendar_id": "1",
                "start_time": "2025-01-01T00:00:00Z",
                "end_time": "2025-01-31T23:59:59Z",
            },
        )
        # Gated user path returns 200 empty list before any calendar lookup.
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty for gated user on blocked-times/expanded, "
            f"got {response.status_code}: {response.data}"
        )
        assert response.data == [], (
            "expanded blocked-times must be empty [] for membership-less user"
        )

    def test_available_times_expanded_returns_empty_for_gated_user(self):
        """GET /available-times/expanded/ → 200 empty list (no 500)."""
        _, client = _gated_client("gated-avail-exp@test.example")
        url = reverse("api:AvailableTimes-expanded")
        response = client.get(
            url,
            {
                "calendar_id": "1",
                "start_time": "2025-01-01T00:00:00Z",
                "end_time": "2025-01-31T23:59:59Z",
            },
        )
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty for gated user on available-times/expanded, "
            f"got {response.status_code}: {response.data}"
        )
        assert response.data == [], (
            "expanded available-times must be empty [] for membership-less user"
        )


# ---------------------------------------------------------------------------
# Webhook endpoints — tenant-scoped, must refuse gated users
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWebhookGatedRefusal:
    """Membership-less users cannot create or list webhook configurations."""

    def test_webhook_configurations_list_returns_empty_for_gated_user(self):
        """GET /webhook-configurations/ → 200 empty list."""
        _, client = _gated_client("gated-webhook-list@test.example")
        url = reverse("api:WebhookConfigurations-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Webhook configuration list must be empty for membership-less user"
        )

    def test_webhook_events_list_returns_empty_for_gated_user(self):
        """GET /webhook-events/ → 200 empty list."""
        _, client = _gated_client("gated-webhook-events@test.example")
        url = reverse("api:WebhookEvents-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200 empty list, got {response.status_code}: {response.data}"
        )
        assert response.data["results"] == [], (
            "Webhook events list must be empty for membership-less user"
        )


# ---------------------------------------------------------------------------
# Organizations endpoints — must refuse gated users from member-only surfaces
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrganizationGatedRefusal:
    """
    Membership-less users are blocked from tenant-scoped organization endpoints
    (invitations management) while onboarding create remains open.
    """

    def test_invitation_list_returns_403_for_gated_user(self):
        """GET /organization-invitations/ → 403 (OrganizationInvitationPermission)."""
        _, client = _gated_client("gated-inv-list@test.example")
        url = reverse("api:OrganizationInvitations-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 for membership-less user on invitation list, "
            f"got {response.status_code}: {response.data}"
        )

    def test_invitation_create_returns_403_for_gated_user(self):
        """POST /organization-invitations/ → 403 (requires membership)."""
        _, client = _gated_client("gated-inv-create@test.example")
        url = reverse("api:OrganizationInvitations-list")
        response = client.post(
            url,
            {"email": "invitee@example.com", "first_name": "New", "last_name": "User"},
            format="json",
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 for gated user on invitation create, "
            f"got {response.status_code}: {response.data}"
        )


# ---------------------------------------------------------------------------
# Write/bulk paths — gated users must receive a clean refusal, never 500
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWriteBulkGatedRefusal:
    """
    Membership-less users hitting WRITE / bulk-create endpoints get a controlled
    4xx refusal — never an unhandled 500.
    """

    def test_calendar_create_returns_400_for_gated_user(self):
        """
        POST /calendar/ as a gated user → 400 (CalendarSerializer.create raises
        ValidationError: "User has no organization membership.").
        No Calendar row is created.
        """
        from calendar_integration.models import Calendar

        _, client = _gated_client("gated-cal-create@test.example")
        url = reverse("api:Calendars-list")
        before_count = Calendar.original_manager.count()
        response = client.post(
            url,
            {"name": "Gated Calendar", "manage_available_windows": False},
            format="json",
        )
        # CalendarSerializer.create guards membership and raises ValidationError → 400.
        assert response.status_code == status.HTTP_400_BAD_REQUEST, (
            f"Expected 400 for gated user on POST /calendar/, got "
            f"{response.status_code}: {response.data}"
        )
        assert Calendar.original_manager.count() == before_count, (
            "No Calendar row must be created for a gated user"
        )

    def test_blocked_times_bulk_create_returns_400_for_gated_user(self):
        """
        POST /blocked-times/bulk-create/ as a gated user → 400.
        The embedded BlockedTimeSerializer resolves an empty Calendar queryset for
        membership-less users, so any calendar FK fails validation before save().
        No BlockedTime row is created.
        """
        from calendar_integration.models import BlockedTime

        _, client = _gated_client("gated-bulk-bt@test.example")
        url = reverse("api:BlockedTimes-bulk-create")
        before_count = BlockedTime.original_manager.count()
        # Sending a non-empty list with a fake calendar id — the queryset is empty for
        # gated users so validation rejects the calendar reference → 400.
        response = client.post(
            url,
            {
                "blocked_times": [
                    {
                        "calendar": 9999,
                        "start_time": "2025-06-01T09:00:00Z",
                        "end_time": "2025-06-01T10:00:00Z",
                        "timezone": "UTC",
                    }
                ]
            },
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, (
            f"Expected 400 for gated user on POST /blocked-times/bulk-create/, got "
            f"{response.status_code}: {response.data}"
        )
        assert BlockedTime.original_manager.count() == before_count, (
            "No BlockedTime row must be created for a gated user"
        )

    def test_available_times_batch_returns_400_for_gated_user(self):
        """
        POST /available-times/batch/ as a gated user → 400.
        The batch serializer refuses membership-less users before any write.
        No AvailableTime row is created.
        """
        from calendar_integration.models import AvailableTime

        _, client = _gated_client("gated-bulk-at@test.example")
        url = reverse("api:AvailableTimes-batch")
        before_count = AvailableTime.original_manager.count()
        response = client.post(
            url,
            {
                "operations": [
                    {
                        "action": "create",
                        "start_time": "2025-06-01T09:00:00Z",
                        "end_time": "2025-06-01T10:00:00Z",
                        "timezone": "UTC",
                    }
                ]
            },
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, (
            f"Expected 400 for gated user on POST /available-times/batch/, got "
            f"{response.status_code}: {response.data}"
        )
        assert AvailableTime.original_manager.count() == before_count, (
            "No AvailableTime row must be created for a gated user"
        )

    def test_calendar_group_retrieve_does_not_500_for_gated_user(self):
        """
        GET /calendar-groups/<pk>/ as a gated user → 403 (never 500).
        CalendarGroupPermission.has_permission uses getattr to guard membership so
        the missing membership raises no exception.  The group belongs to another
        org; the gated user is denied at the list-level permission check (has_permission
        returns False) before has_object_permission is even invoked.
        Covers FIX 1: the guarded has_object_permission path.
        """
        from calendar_integration.models import CalendarGroup

        _, org, _ = _make_member("owner-cg@test.example")
        group = baker.make(CalendarGroup, organization=org)

        _, gated_client = _gated_client("gated-cg-retrieve@test.example")
        url = reverse("api:CalendarGroups-detail", args=[group.pk])
        response = gated_client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 (not 500) for gated user on GET /calendar-groups/<pk>/, "
            f"got {response.status_code}: {response.data}"
        )


# ---------------------------------------------------------------------------
# Verify members still work normally on key endpoints (no regressions)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMemberedUserUnaffected:
    """Confirm that users with a membership still receive correct responses."""

    def test_member_can_list_calendars(self):
        """GET /calendar/ → 200 for an authenticated member."""
        _, _, client = _make_member("member-cal@test.example")
        url = reverse("api:Calendars-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_member_can_list_calendar_events(self):
        """GET /calendar-events/ → 200 for an authenticated member."""
        _, _, client = _make_member("member-events@test.example")
        url = reverse("api:CalendarEvents-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_member_can_list_blocked_times(self):
        """GET /blocked-times/ → 200 for an authenticated member."""
        _, _, client = _make_member("member-blocked@test.example")
        url = reverse("api:BlockedTimes-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_member_can_list_available_times(self):
        """GET /available-times/ → 200 for an authenticated member."""
        _, _, client = _make_member("member-avail@test.example")
        url = reverse("api:AvailableTimes-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_member_can_list_webhook_configurations(self):
        """GET /webhook-configurations/ → 200 for an authenticated member."""
        _, _, client = _make_member("member-webhook@test.example")
        url = reverse("api:WebhookConfigurations-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK
