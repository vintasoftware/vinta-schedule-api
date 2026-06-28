"""Integration tests for the BookingPolicy REST API (Phase 3).

Covers:
- Authenticated CRUD happy paths (create, read, update, delete) — org admin only for writes.
- Unauthorized / forbidden paths (unauthenticated, non-admin member, wrong org via X-Organization-Id header).
- Duplicate-target create → 400 naming the conflict.
- Negative buffer field → 400 naming the field (all four rule fields).
- bogus membership_user_id → 400 (not 500).
- Delete-absent → 204 no-op (idempotent destroy).
- Audit record emitted on create / update / delete.
"""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.factories import create_booking_policy
from calendar_integration.models import BookingPolicy, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_with_member(*, is_admin: bool = False) -> tuple[Organization, OrganizationMembership]:
    """Return a fresh org + membership."""
    user = UserFactory().create_user()
    org = baker.make(Organization, name="Test Org")
    membership = OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=OrganizationRole.ADMIN if is_admin else OrganizationRole.MEMBER,
        is_active=True,
    )
    return org, membership


def _auth_client(membership: OrganizationMembership) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=membership.user)
    return client


def _make_calendar(org: Organization) -> Calendar:
    return baker.make(Calendar, organization=org, external_id=f"cal-{org.id}")


def _make_group(org: Organization) -> CalendarGroup:
    return baker.make(CalendarGroup, organization=org, name="Group")


LIST_URL = "api:BookingPolicies-list"
DETAIL_URL = "api:BookingPolicies-detail"


def _list_url() -> str:
    return reverse(LIST_URL)


def _detail_url(pk: int) -> str:
    return reverse(DETAIL_URL, args=[pk])


# ---------------------------------------------------------------------------
# List / Retrieve
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPolicyList:
    """GET /booking-policies/ — list and cross-org isolation."""

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        response = client.get(_list_url())
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_member_sees_own_org_policies_only(self):
        org, membership = _make_org_with_member()
        client = _auth_client(membership)

        cal = _make_calendar(org)
        create_booking_policy(calendar=cal, lead_time_seconds=300)

        # Another org with its own policy
        other_org, _ = _make_org_with_member()
        other_cal = baker.make(Calendar, organization=other_org, external_id="other-cal")
        create_booking_policy(calendar=other_cal, lead_time_seconds=600)

        response = client.get(_list_url())
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        results = data.get("results", data)
        assert len(results) == 1
        assert results[0]["lead_time_seconds"] == 300

    def test_returns_all_fields(self):
        org, membership = _make_org_with_member()
        client = _auth_client(membership)

        cal = _make_calendar(org)
        policy = create_booking_policy(
            calendar=cal,
            lead_time_seconds=60,
            max_horizon_seconds=3600,
            buffer_before_seconds=120,
            buffer_after_seconds=180,
        )

        response = client.get(_detail_url(policy.pk))
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == policy.pk
        assert data["lead_time_seconds"] == 60
        assert data["max_horizon_seconds"] == 3600
        assert data["buffer_before_seconds"] == 120
        assert data["buffer_after_seconds"] == 180
        assert data["is_organization_default"] is False

    def test_membership_less_user_returns_empty_list(self):
        """A user without any org membership sees an empty list, not a 403."""
        user = UserFactory().create_user()
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get(_list_url())
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        results = data.get("results", data)
        assert results == []

    def test_wrong_org_header_returns_403(self):
        """X-Organization-Id that names a non-member org returns 403."""
        _org, membership = _make_org_with_member()
        other_org, _ = _make_org_with_member()  # The caller has NO membership here

        client = _auth_client(membership)
        response = client.get(_list_url(), HTTP_X_ORGANIZATION_ID=str(other_org.id))
        assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPolicyCreate:
    """POST /booking-policies/ — happy paths, validation, duplicates, audit."""

    def test_create_calendar_policy(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {
                "calendar": cal.pk,
                "lead_time_seconds": 300,
                "max_horizon_seconds": 0,
                "buffer_before_seconds": 60,
                "buffer_after_seconds": 0,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        data = response.json()
        assert data["lead_time_seconds"] == 300
        assert data["buffer_before_seconds"] == 60

        policy = BookingPolicy.objects.filter_by_organization(org.id).get(pk=data["id"])
        assert policy.calendar_fk_id == cal.id

    def test_create_org_default_policy(self):
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        response = client.post(
            _list_url(),
            {
                "is_organization_default": True,
                "lead_time_seconds": 0,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["is_organization_default"] is True

    def test_create_group_policy(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        group = _make_group(org)

        response = client.post(
            _list_url(),
            {"calendar_group": group.pk, "lead_time_seconds": 1800},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["lead_time_seconds"] == 1800

    def test_create_membership_policy(self):
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        response = client.post(
            _list_url(),
            {
                "membership_user_id": membership.user_id,
                "max_horizon_seconds": 7200,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["membership_user_id"] == membership.user_id

    def test_create_without_target_returns_400(self):
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        response = client.post(_list_url(), {"lead_time_seconds": 60})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_with_two_targets_returns_400(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {
                "calendar": cal.pk,
                "is_organization_default": True,
                "lead_time_seconds": 60,
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_duplicate_calendar_target_returns_400(self):
        """Creating a second policy for the same calendar returns 400."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        create_booking_policy(calendar=cal)

        response = client.post(
            _list_url(),
            {"calendar": cal.pk, "lead_time_seconds": 99},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        # Should surface the DuplicateBookingPolicyError message
        error_text = str(body)
        assert "BookingPolicy" in error_text or "already exists" in error_text

    def test_duplicate_org_default_returns_400(self):
        """Creating a second org-default policy returns 400."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        create_booking_policy(is_organization_default=True, organization=org)

        response = client.post(
            _list_url(),
            {"is_organization_default": True, "lead_time_seconds": 100},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_negative_buffer_returns_400(self):
        """Negative rule field values are rejected with a 400 naming the field."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {
                "calendar": cal.pk,
                "buffer_before_seconds": -1,
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        # The error should mention the offending field name.
        assert "buffer_before_seconds" in body

    def test_negative_lead_time_returns_400_naming_field(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {"calendar": cal.pk, "lead_time_seconds": -300},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "lead_time_seconds" in body

    def test_negative_max_horizon_returns_400_naming_field(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {"calendar": cal.pk, "max_horizon_seconds": -1},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "max_horizon_seconds" in body

    def test_negative_buffer_after_returns_400_naming_field(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {"calendar": cal.pk, "buffer_after_seconds": -1},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "buffer_after_seconds" in body

    def test_unauthenticated_create_returns_401(self):
        client = APIClient()
        response = client.post(_list_url(), {"is_organization_default": True})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_member_cannot_create_policy(self):
        """A non-admin member POST → 403."""
        org, membership = _make_org_with_member(is_admin=False)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        response = client.post(
            _list_url(),
            {"calendar": cal.pk, "lead_time_seconds": 60},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_emits_audit_record(self, django_capture_on_commit_callbacks):
        """create writes an audit CREATE record."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    _list_url(),
                    {"calendar": cal.pk, "lead_time_seconds": 60},
                )

        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "create"
        assert payload["subject"]["subject_type"] == "calendar_integration.BookingPolicy"

    def test_cross_org_calendar_not_accepted(self):
        """Passing a calendar from a different org is rejected (FK queryset check)."""
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        other_org, _ = _make_org_with_member()
        other_cal = baker.make(Calendar, organization=other_org, external_id="other-cal-2")

        response = client.post(
            _list_url(),
            {"calendar": other_cal.pk, "lead_time_seconds": 60},
        )
        # The serializer's org-scoped queryset will reject the cross-org calendar PK.
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_membership_policy_bogus_user_id_returns_400(self):
        """membership_user_id that is not a member of the org → 400, not 500."""
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        bogus_user_id = 999_999_999

        response = client.post(
            _list_url(),
            {"membership_user_id": bogus_user_id, "lead_time_seconds": 60},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "membership_user_id" in body


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPolicyUpdate:
    """PUT/PATCH /booking-policies/{id}/ — rule fields only, audit."""

    def test_patch_updates_rule_fields(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)

        response = client.patch(
            _detail_url(policy.pk),
            {"lead_time_seconds": 120, "buffer_after_seconds": 30},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()
        data = response.json()
        assert data["lead_time_seconds"] == 120
        assert data["buffer_after_seconds"] == 30

    def test_target_fields_ignored_on_update(self):
        """Passing 'is_organization_default' on update is silently ignored (target immutable)."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal)

        response = client.patch(
            _detail_url(policy.pk),
            {"is_organization_default": True, "lead_time_seconds": 200},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()
        # Target should not have changed.
        policy.refresh_from_db()
        assert policy.is_organization_default is False
        assert policy.lead_time_seconds == 200

    def test_negative_buffer_on_update_returns_400(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal)

        response = client.patch(
            _detail_url(policy.pk),
            {"buffer_before_seconds": -5},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "buffer_before_seconds" in body

    def test_update_cross_org_returns_404(self):
        """Updating a policy from a different org returns 404 (not found in queryset)."""
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        other_org, _ = _make_org_with_member()
        other_cal = baker.make(Calendar, organization=other_org, external_id="other-cal-u")
        other_policy = create_booking_policy(calendar=other_cal)

        response = client.patch(_detail_url(other_policy.pk), {"lead_time_seconds": 999})
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_member_cannot_update_policy(self):
        """A non-admin member PATCH → 403."""
        org, _admin_membership = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)

        # Create a non-admin member in the same org
        non_admin_user = UserFactory().create_user()
        non_admin_membership = OrganizationMembership.objects.create(
            user=non_admin_user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = _auth_client(non_admin_membership)

        response = client.patch(_detail_url(policy.pk), {"lead_time_seconds": 999})
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_emits_audit_record(self, django_capture_on_commit_callbacks):
        """update writes an audit UPDATE record with a diff."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal, lead_time_seconds=60)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.patch(
                    _detail_url(policy.pk),
                    {"lead_time_seconds": 300},
                )

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "update"
        assert payload["diff"] is not None


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPolicyDestroy:
    """DELETE /booking-policies/{id}/ — idempotent no-op + audit."""

    def test_delete_existing_policy(self):
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal)

        response = client.delete(_detail_url(policy.pk))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            not BookingPolicy.objects.filter_by_organization(org.id).filter(pk=policy.pk).exists()
        )

    def test_delete_absent_returns_204_no_op(self):
        """Deleting a non-existent policy pk returns 204 (idempotent no-op)."""
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        non_existent_pk = 9_999_999
        response = client.delete(_detail_url(non_existent_pk))
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_cross_org_returns_204_no_op(self):
        """Deleting a pk that belongs to a different org is an idempotent no-op, not a 403.

        The plan's delete-absent semantics mean: if the policy isn't resolvable in
        the caller's org, treat it the same as absent → 204 no-op.
        """
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        other_org, _ = _make_org_with_member()
        other_cal = baker.make(Calendar, organization=other_org, external_id="other-cal-d")
        other_policy = create_booking_policy(calendar=other_cal)

        response = client.delete(_detail_url(other_policy.pk))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        # The other org's policy must still exist.
        assert (
            BookingPolicy.objects.filter_by_organization(other_org.id)
            .filter(pk=other_policy.pk)
            .exists()
        )

    def test_member_cannot_delete_policy(self):
        """A non-admin member DELETE → 403."""
        org, _admin_membership = _make_org_with_member(is_admin=True)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal)

        non_admin_user = UserFactory().create_user()
        non_admin_membership = OrganizationMembership.objects.create(
            user=non_admin_user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = _auth_client(non_admin_membership)

        response = client.delete(_detail_url(policy.pk))
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_emits_audit_record(self, django_capture_on_commit_callbacks):
        """delete writes an audit DELETE record."""
        org, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)
        cal = _make_calendar(org)
        policy = create_booking_policy(calendar=cal)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.delete(_detail_url(policy.pk))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert mock_task.delay.called
        payload = mock_task.delay.call_args[0][0]
        assert payload["action"] == "delete"

    def test_delete_absent_does_not_emit_audit_record(self, django_capture_on_commit_callbacks):
        """No audit record when the policy was already absent (truly idempotent)."""
        _, membership = _make_org_with_member(is_admin=True)
        client = _auth_client(membership)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.delete(_detail_url(9_999_998))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        # The service's delete_booking_policy(None) should not call audit.
        assert not mock_task.delay.called

    def test_unauthenticated_delete_returns_401(self):
        client = APIClient()
        response = client.delete(_detail_url(1))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
