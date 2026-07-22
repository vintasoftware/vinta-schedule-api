"""``OrganizationService.invite_user_to_organization`` — the seat-limit check.

An organization at its seat limit cannot add another member.
``organization_members`` usage is active memberships **plus** pending
invitations (``EntitlementService._count_organization_members``), so a pending
invitation itself already occupies a seat. The second half of these tests pins
that down directly: a check that only looked at ``OrganizationMembership`` would
let an organization invite arbitrarily many people past its ceiling as long as
none of them had accepted yet.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import OrganizationService
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def _organization_with_seat_limit(
    seat_limit: int, existing_active_members: int = 0
) -> Organization:
    """A standalone (non-reseller) organization with a finite seat ceiling."""
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
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
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        limit_value=seat_limit,
        kind=LimitKind.PREPAID,
    )
    if existing_active_members:
        baker.make(
            OrganizationMembership,
            organization=organization,
            is_active=True,
            _quantity=existing_active_members,
        )
    return organization


@pytest.fixture
def service():
    # Untyped deliberately: OrganizationService's constructor params are DI-injected
    # (Provide[...]) and resolved at call time by the wired container, which mypy
    # cannot see -- an explicit `-> OrganizationService` return annotation would make
    # mypy check this body and flag the zero-arg call as missing every constructor
    # argument. See organizations/tests/test_organization_creation_billing.py for the
    # same pattern.
    return OrganizationService()


@pytest.mark.django_db
class TestInviteAtTheSeatLimit:
    def test_invite_at_the_limit_raises_and_creates_nothing(self, service):
        organization = _organization_with_seat_limit(seat_limit=2, existing_active_members=2)

        with pytest.raises(OverLimitError) as exc_info:
            service.invite_user_to_organization(
                email="blocked@example.com",
                first_name="Blocked",
                last_name="Invitee",
                organization=organization,
                send_email=False,
            )

        assert exc_info.value.resource_key == LimitedResource.ORGANIZATION_MEMBERS
        assert exc_info.value.current_usage == 2
        assert exc_info.value.limit == 2
        assert not OrganizationInvitation.objects.filter(email="blocked@example.com").exists()

    def test_invite_with_headroom_succeeds(self, service):
        organization = _organization_with_seat_limit(seat_limit=3, existing_active_members=1)

        invitation = service.invite_user_to_organization(
            email="fits@example.com",
            first_name="Fits",
            last_name="Invitee",
            organization=organization,
            send_email=False,
        )

        assert invitation.pk is not None
        assert OrganizationInvitation.objects.filter(email="fits@example.com").exists()

    def test_bypass_limits_creates_the_invitation_anyway(self, service):
        """``bypass_limits=True`` is for management commands / repair scripts only,
        but the escape hatch itself must work — the guarding ``if`` is the only
        thing standing between this and a normal invite."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)

        invitation = service.invite_user_to_organization(
            email="bypassed@example.com",
            first_name="Bypassed",
            last_name="Invitee",
            organization=organization,
            send_email=False,
            bypass_limits=True,
        )

        assert invitation.pk is not None
        assert OrganizationInvitation.objects.filter(email="bypassed@example.com").exists()

    def test_error_body_matches_the_shared_over_limit_contract(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)

        with pytest.raises(OverLimitError) as exc_info:
            service.invite_user_to_organization(
                email="blocked2@example.com",
                first_name="Blocked",
                last_name="Two",
                organization=organization,
                send_email=False,
            )

        assert exc_info.value.as_error_body() == {
            "detail": "Organization is at its limit for organization members.",
            "code": "limit_exceeded",
            "resource": "organization_members",
            "current_usage": 1,
            "limit": 1,
            "remedy": "purchase_add_on",
        }


@pytest.mark.django_db
class TestPendingInvitationsCountTowardTheCeiling:
    def test_one_pending_invitation_at_a_limit_of_one_blocks_a_second(self, service):
        """No active members at all — the ceiling is entirely consumed by a single
        still-pending invitation, proving the counter reads
        ``OrganizationInvitation``, not just ``OrganizationMembership``."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="already-pending@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        with pytest.raises(OverLimitError) as exc_info:
            service.invite_user_to_organization(
                email="second@example.com",
                first_name="Second",
                last_name="Invitee",
                organization=organization,
                send_email=False,
            )

        assert exc_info.value.current_usage == 1
        assert not OrganizationInvitation.objects.filter(email="second@example.com").exists()

    def test_expired_pending_invitations_do_not_count(self, service):
        """An expired invitation can never become a seat, so it must not occupy one."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="expired@example.com",
            expires_at=timezone.now() - datetime.timedelta(days=1),
            accepted_at=None,
            membership_user_id=None,
        )

        invitation = service.invite_user_to_organization(
            email="fits-now@example.com",
            first_name="Fits",
            last_name="Now",
            organization=organization,
            send_email=False,
        )

        assert invitation.pk is not None

    def test_accepted_invitations_do_not_double_count_with_their_membership(self, service):
        """An accepted invitation's seat is the resulting membership row — counting
        both would double-charge the same seat."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)
        member = OrganizationMembership.objects.filter(organization=organization).first()
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="already-accepted@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=timezone.now(),
            membership_user_id=member.user_id,
        )

        with pytest.raises(OverLimitError) as exc_info:
            service.invite_user_to_organization(
                email="blocked3@example.com",
                first_name="Blocked",
                last_name="Three",
                organization=organization,
                send_email=False,
            )

        # Usage is 1 (the active member), not 2 — the accepted invitation is not
        # double-counted alongside the membership it turned into.
        assert exc_info.value.current_usage == 1


def _pending_invitation_and_token(organization: Organization, email: str):
    from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token

    token = generate_long_lived_token()
    invitation = baker.make(
        OrganizationInvitation,
        organization=organization,
        email=email,
        token_hash=hash_long_lived_token(token),
        expires_at=timezone.now() + datetime.timedelta(days=7),
        accepted_at=None,
        membership_user_id=None,
    )
    return invitation, token


@pytest.mark.django_db
class TestAcceptInvitationSeatLimitGuard:
    """``accept_invitation`` must use ``check_seat_limit_for_invitation_accept``,
    not the generic ``check_limit`` — see the module docstring. These tests fail
    if that call is ever replaced with a plain ``check_limit(delta=1)``."""

    def test_accepting_the_last_pending_seat_succeeds(self, service):
        """The positive control: the invitation being accepted must not count
        against itself."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        user = baker.make("users.User", email="fills-seat@example.com")
        invitation, token = _pending_invitation_and_token(organization, user.email)

        membership = service.accept_invitation(token=token, user=user)

        assert membership.organization_id == organization.id
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None

    def test_accept_blocked_when_a_different_pending_invitation_fills_the_seat(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="unrelated-pending@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        user = baker.make("users.User", email="blocked-acceptor@example.com")
        invitation, token = _pending_invitation_and_token(organization, user.email)

        with pytest.raises(OverLimitError):
            service.accept_invitation(token=token, user=user)

        assert not OrganizationMembership.objects.filter(
            user=user, organization=organization
        ).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    def test_bypass_limits_accepts_anyway(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="unrelated-pending2@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        user = baker.make("users.User", email="bypassed-acceptor@example.com")
        _invitation, token = _pending_invitation_and_token(organization, user.email)

        membership = service.accept_invitation(token=token, user=user, bypass_limits=True)

        assert membership.organization_id == organization.id


@pytest.mark.django_db
class TestProvisionTenantForUserInviteBranchSeatLimitGuard:
    """The signup-path equivalent of ``accept_invitation`` — same net-zero-safe
    entry point, same guard placement inside the same transaction as the write."""

    def test_joining_via_the_last_pending_seat_succeeds(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        user = baker.make("users.User", email="signup-fills-seat@example.com")
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email=user.email,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        membership = service.provision_tenant_for_user(user=user)

        assert membership is not None
        assert membership.organization_id == organization.id

    def test_provision_blocked_when_a_different_pending_invitation_fills_the_seat(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="signup-unrelated-pending@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        user = baker.make("users.User", email="signup-blocked@example.com")
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email=user.email,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        with pytest.raises(OverLimitError):
            service.provision_tenant_for_user(user=user)

        assert not OrganizationMembership.objects.filter(
            user=user, organization=organization
        ).exists()

    def test_bypass_limits_joins_anyway(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="signup-unrelated-pending2@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        user = baker.make("users.User", email="signup-bypassed@example.com")
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email=user.email,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        membership = service.provision_tenant_for_user(user=user, bypass_limits=True)

        assert membership is not None
        assert membership.organization_id == organization.id


@pytest.mark.django_db
class TestReactivateMembershipSeatLimitGuard:
    """The seat-limit check lives in ``OrganizationService.reactivate_membership``,
    not the viewset: enforcement belongs in the service layer, not viewsets. A
    caller that bypasses the viewset entirely (management command, admin action,
    shell) must still hit the check by default, and must be able to opt out
    explicitly via ``bypass_limits``."""

    def test_reactivate_at_the_limit_raises_and_leaves_the_member_inactive(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)
        inactive_member = baker.make(
            OrganizationMembership, organization=organization, is_active=False
        )

        with pytest.raises(OverLimitError):
            service.reactivate_membership(inactive_member)

        inactive_member.refresh_from_db()
        assert inactive_member.is_active is False

    def test_reactivate_with_headroom_succeeds(self, service):
        organization = _organization_with_seat_limit(seat_limit=2, existing_active_members=1)
        inactive_member = baker.make(
            OrganizationMembership, organization=organization, is_active=False
        )

        reactivated = service.reactivate_membership(inactive_member)

        assert reactivated.is_active is True
        inactive_member.refresh_from_db()
        assert inactive_member.is_active is True

    def test_reactivating_an_already_active_member_is_a_no_op_even_at_the_limit(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)
        active_member = OrganizationMembership.objects.filter(organization=organization).first()

        reactivated = service.reactivate_membership(active_member)

        assert reactivated.is_active is True

    def test_bypass_limits_reactivates_anyway(self, service):
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=1)
        inactive_member = baker.make(
            OrganizationMembership, organization=organization, is_active=False
        )

        reactivated = service.reactivate_membership(inactive_member, bypass_limits=True)

        assert reactivated.is_active is True
