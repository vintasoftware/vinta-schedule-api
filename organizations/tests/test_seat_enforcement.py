"""Phase 6a integration: the seat-limit guard blocks identically across every
surface — REST invite, REST accept, the GraphQL ``createInvitation`` mutation,
and member reactivation — with a byte-identical error body between REST and
GraphQL, an ``unlimited`` organization is never blocked, and the row lock
genuinely serializes a race for the last seat.

Spec acceptance scenario 6 (race for the last seat) and scenario 7 (the partner
API is not a bypass) are automated here.
"""

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.services import OrganizationService
from payments.billing_constants import BillingState, Entitlement, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import (
    BillingPlan,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from payments.services.entitlement_service import EntitlementService
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


SHARED_OVER_LIMIT_BODY = {
    "detail": "Organization is at its limit for organization members.",
    "code": "limit_exceeded",
    "resource": "organization_members",
    "current_usage": 1,
    "limit": 1,
    "remedy": "purchase_add_on",
}

CREATE_INVITATION_MUTATION = """
mutation CreateInvitation($input: CreateInvitationInput!) {
    createInvitation(input: $input) {
        invitation { id email expiresAt }
        token
        inviteUrl
    }
}
"""

BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30
# Long enough that a non-locking implementation reliably interleaves its read
# with the other thread's; short enough not to slow the suite noticeably.
RACE_WINDOW_SECONDS = 0.5


def _organization_with_seat_limit(
    seat_limit: int,
    existing_active_members: int = 0,
    can_invite_organizations: bool = False,
) -> Organization:
    organization = baker.make(
        Organization, parent=None, can_invite_organizations=can_invite_organizations
    )
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
    if can_invite_organizations:
        # Phase 6c: a reseller org used to exercise the GraphQL surface needs
        # `partner_api` granted, or `PublicApiSystemUserMiddleware`'s entitlement
        # gate blocks it before the seat-limit guard this test targets ever runs.
        # A real reseller on any real plan carries this row (every seeded plan
        # grants it); this fixture only needs to say so explicitly because it
        # builds a bare `Subscription` rather than going through
        # `SubscriptionService`, which is what normally syncs it.
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=True,
        )
    if existing_active_members:
        baker.make(
            OrganizationMembership,
            organization=organization,
            is_active=True,
            _quantity=existing_active_members,
        )
    return organization


@pytest.mark.django_db
class TestInviteBlockedIdenticallyAcrossRestAndGraphQL:
    """Use-case 2 (blocked with a useful message) and Use-case 7 (the partner API
    is not a bypass), against organizations built identically so the two bodies
    can be compared directly."""

    def test_rest_invite_is_blocked_with_the_shared_over_limit_body(self):
        admin = baker.make(get_user_model(), email="rest-admin@example.com")
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=admin)

        response = client.post(
            reverse("api:OrganizationInvitations-list"),
            {"email": "rest-blocked@example.com", "first_name": "R", "last_name": "B"},
            format="json",
        )

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.json() == SHARED_OVER_LIMIT_BODY
        assert not OrganizationInvitation.objects.filter(email="rest-blocked@example.com").exists()

    def test_graphql_invite_is_blocked_with_the_shared_over_limit_body(self):
        reseller_org = _organization_with_seat_limit(
            seat_limit=1, existing_active_members=1, can_invite_organizations=True
        )
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        from di_core.containers import container

        client = APIClient()
        with container.public_api_auth_service.override(auth_service):
            response = client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": "graphql-blocked@example.com",
                            "organizationId": str(reseller_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        # createInvitation is a non-nullable field, so a resolver error nulls the
        # whole `data` key per GraphQL error-propagation rules.
        assert data["data"] is None
        assert len(data["errors"]) == 1
        assert data["errors"][0]["extensions"] == SHARED_OVER_LIMIT_BODY
        assert not OrganizationInvitation.objects.filter(
            email="graphql-blocked@example.com"
        ).exists()

    def test_rest_and_graphql_bodies_are_byte_identical(self):
        """Not just each matching the same literal — the two responses compared
        directly against each other, which is the actual contract."""
        admin = baker.make(get_user_model(), email="compare-admin@example.com")
        rest_org = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=rest_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        rest_client = APIClient()
        rest_client.force_authenticate(user=admin)
        rest_response = rest_client.post(
            reverse("api:OrganizationInvitations-list"),
            {"email": "compare-rest@example.com", "first_name": "C", "last_name": "R"},
            format="json",
        )

        graphql_org = _organization_with_seat_limit(
            seat_limit=1, existing_active_members=1, can_invite_organizations=True
        )
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="compare_integration", organization=graphql_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        from di_core.containers import container

        graphql_client = APIClient()
        with container.public_api_auth_service.override(auth_service):
            graphql_response = graphql_client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": "compare-graphql@example.com",
                            "organizationId": str(graphql_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert rest_response.json() == graphql_response.json()["errors"][0]["extensions"]


@pytest.mark.django_db
class TestAcceptInvitationBlockedAtTheLimit:
    """Accepting an invitation must use the net-zero-safe entry point — see
    ``check_seat_limit_for_invitation_accept``. A limit of 1 with 1 pending
    invitation and 0 active members leaves the org exactly full of *pending*
    seats, so if accept incorrectly used ``check_limit`` directly the accept
    would be blocked at exactly the ceiling it is trying to fill."""

    def _pending_invitation(self, organization, email):
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

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

    def test_accepting_the_organizations_own_last_pending_invitation_succeeds(self):
        """The positive control: accept must NOT be blocked by the very
        invitation it is accepting."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        invitee = baker.make(get_user_model(), email="fills-last-seat@example.com")
        _invitation, token = self._pending_invitation(organization, invitee.email)

        client = APIClient()
        client.force_authenticate(user=invitee)
        response = client.post(reverse("accept-invitation"), {"token": token}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert OrganizationMembership.objects.filter(
            user=invitee, organization=organization
        ).exists()

    def test_accept_is_blocked_when_a_different_pending_invitation_would_overflow(self):
        """A *second*, unrelated pending invitation already fills the ceiling, so
        accepting this one would take the org over its limit — this must block."""
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        # A different pending invitation occupies the organization's one seat.
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email="other-pending@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        invitee = baker.make(get_user_model(), email="blocked-acceptor@example.com")
        invitation, token = self._pending_invitation(organization, invitee.email)

        client = APIClient()
        client.force_authenticate(user=invitee)
        response = client.post(reverse("accept-invitation"), {"token": token}, format="json")

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.json() == SHARED_OVER_LIMIT_BODY
        assert not OrganizationMembership.objects.filter(
            user=invitee, organization=organization
        ).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None


@pytest.mark.django_db
class TestAcceptInvitationMarksAcceptedInsideTheSameTransaction:
    """SHOULD-FIX 3: ``accept_invitation`` must mark the invitation accepted
    inside the same ``transaction.atomic()`` block as the guard and the
    membership create, not as a separate write after that block exits. Outside
    a request (Celery, management command, shell) there is no outer
    ``ATOMIC_REQUESTS`` transaction to fold a later failure into: if the
    membership create commits and the invitation-accepted write then fails, the
    membership exists while the invitation still reads as pending -- a
    permanent double-count of that seat."""

    def test_a_failure_marking_the_invitation_accepted_rolls_back_the_membership_too(self):
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        organization = _organization_with_seat_limit(seat_limit=2, existing_active_members=0)
        invitee = baker.make(get_user_model(), email="atomic-accept@example.com")
        token = generate_long_lived_token()
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email=invitee.email,
            token_hash=hash_long_lived_token(token),
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        service = OrganizationService()
        with (
            patch.object(OrganizationInvitation, "save", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            service.accept_invitation(token, invitee)

        assert not OrganizationMembership.objects.filter(
            user=invitee, organization=organization
        ).exists(), (
            "The membership committed even though marking the invitation accepted "
            "failed -- the two writes must share one transaction."
        )


@pytest.mark.django_db
class TestProvisionTenantForUserMarksAcceptedInsideTheSameTransaction:
    """SHOULD-FIX 1, Phase 6a verification review: ``provision_tenant_for_user``'s
    pending-invitation (signup) branch has the exact same twin bug
    ``accept_invitation`` had -- marking the invitation accepted after the inner
    ``transaction.atomic()`` block that creates the membership has already exited,
    instead of inside it. Signup is the higher-traffic of the two paths."""

    def test_a_failure_marking_the_invitation_accepted_rolls_back_the_membership_too(self):
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        organization = _organization_with_seat_limit(seat_limit=2, existing_active_members=0)
        invitee = baker.make(get_user_model(), email="atomic-provision@example.com")
        token = generate_long_lived_token()
        baker.make(
            OrganizationInvitation,
            organization=organization,
            email=invitee.email,
            token_hash=hash_long_lived_token(token),
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        service = OrganizationService()
        with (
            patch.object(OrganizationInvitation, "save", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            service.provision_tenant_for_user(invitee)

        assert not OrganizationMembership.objects.filter(
            user=invitee, organization=organization
        ).exists(), (
            "The membership committed even though marking the invitation accepted "
            "failed -- the two writes must share one transaction."
        )


@pytest.mark.django_db
class TestReactivationBlockedAtTheLimit:
    def test_reactivate_is_blocked_at_the_seat_limit(self):
        admin = baker.make(get_user_model(), email="reactivate-admin@example.com")
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        inactive_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )
        client = APIClient()
        client.force_authenticate(user=admin)

        url = reverse(
            "api:OrganizationMembers-reactivate", kwargs={"user_id": inactive_member.user_id}
        )
        response = client.post(url)

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.json() == SHARED_OVER_LIMIT_BODY
        inactive_member.refresh_from_db()
        assert inactive_member.is_active is False

    def test_reactivating_an_already_active_member_is_a_no_op_even_at_the_limit(self):
        """Idempotency must survive the guard: an already-active member's state
        does not change, so re-affirming it must not be blocked by a ceiling it
        does not push the organization past."""
        admin = baker.make(get_user_model(), email="reactivate-admin2@example.com")
        organization = _organization_with_seat_limit(seat_limit=1, existing_active_members=0)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        already_active_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=admin)

        url = reverse(
            "api:OrganizationMembers-reactivate",
            kwargs={"user_id": already_active_member.user_id},
        )
        response = client.post(url)

        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestResendAtTheCeiling:
    """BLOCKER 2: a resend creates nothing new — the pending invitation being
    resent already counts toward the ceiling — so it must be net-zero exactly
    like an accept, not a false block at the exact limit."""

    def test_resend_succeeds_at_the_seat_limit(self):
        # The requesting admin itself occupies a seat, so the ceiling has to
        # account for it: limit=2 covers the admin (1) plus the one pending
        # invitation (1) -- exactly full, mirroring the finding's "4 active
        # members + 1 pending invite at limit 5" scenario at a smaller scale.
        admin = baker.make(get_user_model(), email="resend-admin@example.com")
        organization = _organization_with_seat_limit(seat_limit=2, existing_active_members=0)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        pending_invitation = baker.make(
            OrganizationInvitation,
            organization=organization,
            email="already-pending@example.com",
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        original_token_hash = pending_invitation.token_hash

        client = APIClient()
        client.force_authenticate(user=admin)
        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": pending_invitation.pk})
        response = client.post(url)

        assert response.status_code == status.HTTP_200_OK
        pending_invitation.refresh_from_db()
        assert pending_invitation.token_hash != original_token_hash
        assert OrganizationInvitation.objects.filter(organization=organization).count() == 1, (
            "Resend must reuse the existing pending invitation, not create a second row."
        )


@pytest.mark.django_db
class TestUnlimitedPlanIsNeverBlocked:
    """The plan's own rollout switch: every organization starts on ``unlimited``,
    and this feature must not change behavior for one until it is migrated onto
    a real plan."""

    def test_invites_past_every_typical_threshold_succeed(self):
        creator = baker.make(get_user_model(), email="unlimited-creator@example.com")
        organization = OrganizationService().create_organization(
            creator=creator, name="Unlimited Seat Org"
        )
        assert Subscription.objects.get(organization=organization).plan.slug == "unlimited"

        service = OrganizationService()
        for index in range(25):
            invitation = service.invite_user_to_organization(
                email=f"unlimited-member-{index}@example.com",
                first_name="Member",
                last_name=str(index),
                organization=organization,
                send_email=False,
            )
            assert invitation.pk is not None

        assert OrganizationInvitation.objects.filter(organization=organization).count() == 25

    def test_query_count_does_not_scale_with_existing_pending_invitations(self):
        """Phase 5 deliberately skips usage counting on the unlimited path — this
        pins that an org already sitting on a pile of pending invitations pays no
        more query cost per invite than a brand-new org does."""
        creator = baker.make(get_user_model(), email="unlimited-creator2@example.com")
        organization = OrganizationService().create_organization(
            creator=creator, name="Unlimited Query Count Org"
        )
        service = OrganizationService()

        with CaptureQueriesContext(connection) as first_call:
            service.invite_user_to_organization(
                email="first-invite@example.com",
                first_name="First",
                last_name="Invite",
                organization=organization,
                send_email=False,
            )

        for index in range(20):
            baker.make(
                OrganizationInvitation,
                organization=organization,
                email=f"padding-{index}@example.com",
                expires_at=timezone.now() + datetime.timedelta(days=7),
                accepted_at=None,
                membership_user_id=None,
            )

        with CaptureQueriesContext(connection) as second_call:
            service.invite_user_to_organization(
                email="second-invite@example.com",
                first_name="Second",
                last_name="Invite",
                organization=organization,
                send_email=False,
            )

        # An absolute budget on each call, not a first-vs-second comparison:
        # usage counting is O(1) in queries regardless of row count, so if the
        # unlimited path started counting, both measurements would grow by the
        # same fixed amount and a relative "second == first" comparison would
        # still pass -- it pins nothing. 9 is the exact query count either call
        # makes today (checked against both calls independently instead of a
        # loose ceiling, so any change -- including a regression that pushes it
        # back up -- fails loudly instead of silently eating slack).
        for label, call in (
            ("first (empty org)", first_call),
            ("second (20 pending invitations already in the database)", second_call),
        ):
            assert len(call.captured_queries) == 9, (
                f"Query count changed for the {label} invite -- the unlimited path "
                f"must never count usage. Queries: {[q['sql'] for q in call.captured_queries]}"
            )
        assert not [
            query
            for query in second_call.captured_queries
            if "organizations_organizationinvitation" in query["sql"]
            and "COUNT" in query["sql"].upper()
        ], "The unlimited path counted pending-invitation usage nobody reads."


def _run_two_racing_invites(organization: Organization, force_unlocked: bool) -> list[bool]:
    """Two threads each try to invite into the last seat via the real,
    production ``OrganizationService.invite_user_to_organization`` — not a
    synthetic stand-in for it.

    ``force_unlocked`` patches the exact ``EntitlementService.check_limit`` call
    site the service uses to drop the row lock, and adds an artificial delay
    between the check and the write on *both* variants so the race window is
    deterministic rather than timing-dependent — mirroring
    ``payments/tests/services/test_limit_concurrency.py``'s harness, applied to
    the real invite path instead of a raw ``check_limit`` call.
    """
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
    original_check_limit = EntitlementService.check_limit

    def delayed_check_limit(self, *args, **kwargs):
        if force_unlocked:
            kwargs["lock"] = False
        result = original_check_limit(self, *args, **kwargs)
        # Widen the window between the check (and, when locked, the row lock it
        # takes) and the invitation write that follows it in the same
        # transaction, so the two threads are guaranteed to overlap.
        threading.Event().wait(RACE_WINDOW_SECONDS)
        return result

    # Untyped deliberately: OrganizationService's constructor params are DI-injected
    # (Provide[...]) and resolved at call time by the wired container, which mypy
    # cannot see -- a fully-typed signature here would make mypy check this body and
    # flag the zero-arg call as missing every constructor argument. See
    # organizations/tests/test_organization_creation_billing.py for the same pattern.
    def invite(index):
        service = OrganizationService()
        try:
            start_barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
            try:
                service.invite_user_to_organization(
                    email=f"seat-racer-{index}@example.com",
                    first_name="Racer",
                    last_name=str(index),
                    organization=organization,
                    send_email=False,
                )
                return True
            except OverLimitError:
                return False
        finally:
            # Each thread owns its own connection; leaking it holds the row
            # lock past the test and wedges the next one.
            connection.close()

    with patch.object(EntitlementService, "check_limit", delayed_check_limit):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invite, index) for index in (0, 1)]
            return [future.result(timeout=THREAD_JOIN_TIMEOUT_SECONDS) for future in futures]


@pytest.mark.django_db(transaction=True)
class TestConcurrentInvitesForTheLastSeat:
    """Spec acceptance scenario 6: two admins inviting for one remaining seat at
    the same time — exactly one succeeds, and the organization never exceeds its
    limit."""

    def test_two_threads_racing_for_the_last_seat_serialize_and_only_one_succeeds(self):
        organization = _organization_with_seat_limit(seat_limit=3, existing_active_members=2)

        verdicts = _run_two_racing_invites(organization, force_unlocked=False)

        assert sorted(verdicts) == [False, True], f"exactly one thread must win, got {verdicts}"
        assert OrganizationInvitation.objects.filter(organization=organization).count() == 1

    def test_without_the_lock_the_race_overshoots(self):
        """Negative control, proving the harness above genuinely races rather
        than passing by accident (e.g. because SQLite/test-transaction quirks
        serialize everything regardless of locking). With the row lock forced
        off, both threads read the same pre-insert count and both create,
        taking the organization over its limit."""
        organization = _organization_with_seat_limit(seat_limit=3, existing_active_members=2)

        verdicts = _run_two_racing_invites(organization, force_unlocked=True)

        assert verdicts == [True, True]
        assert OrganizationInvitation.objects.filter(organization=organization).count() == 2
