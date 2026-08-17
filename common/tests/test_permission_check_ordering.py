"""A permission class is asked about the organization the header names.

``APIView.initial`` runs, in order: content negotiation, versioning,
``perform_authentication``, ``check_permissions``, ``check_throttles``.
``TenantScopedViewMixin`` used to resolve
``X-Organization-Id`` *after* all of that -- so every permission class asking
the membership helper at ``has_permission`` time fell through to the caller's
**oldest** active membership. Meanwhile ``get_queryset``, object permissions,
and serializers re-asked *after* resolution and answered with the organization
the header named.

The shape that exploits is one user, two organizations: admin of the older one,
plain member of the newer one. The collection-level admin gate said yes about
the older organization; everything after it acted on the newer one.

``TenantScopedViewMixin.perform_authentication`` now resolves and binds between
authentication and ``check_permissions``, so the gate is asked about the named
organization.

**These tests are mutation-tested, in-repo and permanently**:
``TestRestoringTheOldOrderingReopensIt`` below dispatches the same request
through a view that reconstructs the old ordering exactly, and asserts
it is *admitted*. If the ordering fix were reverted,
``TestTheAdminGateFollowsTheHeader`` would report the same 200 that class
asserts -- so the two together discriminate, rather than one of them merely
passing.
"""

from typing import Any, cast

from django.contrib.auth import get_user_model

import pytest
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import (
    Organization,
    OrganizationMembership,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.permissions import IsOrganizationAdmin
from organizations.tests.helpers import grant_membership_groups


User = get_user_model()


class AdminGatedProbeView(TenantScopedViewMixin, APIView):
    """A collection-level ``IsOrganizationAdmin`` gate, and nothing else.

    Deliberately not a real endpoint: the defect is in the mixin and the
    permission class, and every endpoint that pairs the two inherits whatever
    this view demonstrates. ``organization_seen_by_the_body`` records which
    organization the *handler* would have acted on, so a test can show the gate
    and the body agreeing rather than only that the gate refused.
    """

    permission_classes = (IsOrganizationAdmin,)

    #: pk of the organization the body resolved, or ``None``. Class-level so a
    #: test can read it after ``as_view()`` has thrown the instance away.
    organization_seen_by_the_body: Any = None

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        membership = request.organization_membership
        type(self).organization_seen_by_the_body = (
            None if membership is None else membership.organization_id
        )
        return Response({"ok": True})


class OldOrderingAdminGatedProbeView(AdminGatedProbeView):
    """``AdminGatedProbeView`` with the old resolve-after-``check_permissions``
    ordering restored.

    Not a historical curiosity -- it is the mutant. It reconstructs the exact
    old request state as well as its ordering: authentication records the oldest
    membership before permissions, and header resolution replaces it only after
    ``super().initial()``. This makes the historical widened grant observable
    without retaining the compatibility helper in application code.
    """

    def perform_authentication(self, request: Request) -> None:
        # The stock DRF implementation, skipping the mixin's override.
        APIView.perform_authentication(self, request)
        request.organization_membership = (  # type: ignore[attr-defined]
            cast("Any", request.user).memberships.filter(is_active=True).order_by("created").first()
        )

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        self.resolve_organization(request)
        self.bind_organization(request.organization)  # type: ignore[attr-defined]


def _dispatch(view_class: type[AdminGatedProbeView], user: Any, header: str) -> Any:
    view_class.organization_seen_by_the_body = None
    request = APIRequestFactory().get("/probe/", None, HTTP_X_ORGANIZATION_ID=header)
    force_authenticate(request, user=user)
    return view_class.as_view()(request)


@pytest.fixture
def older_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Ordering Org A")


@pytest.fixture
def newer_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Ordering Org B")


@pytest.fixture
def admin_here_member_there(
    user: Any, older_organization: Organization, newer_organization: Organization
) -> Any:
    """Admin of the organization created first, plain member of the second.

    ``order_by("created")`` is what the old fallback used, and the memberships
    are created in this order, so the fallback answers ``older_organization``.
    """
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=older_organization,
            is_active=True,
        ),
        [GROUP_ORGANIZATION_ADMIN],
    )
    OrganizationMembership.objects.create(
        user=user,
        organization=newer_organization,
        is_active=True,
    )
    return user


@pytest.fixture
def member_here_admin_there(
    user: Any, older_organization: Organization, newer_organization: Organization
) -> Any:
    """The mirror of ``admin_here_member_there``: plain member of the
    organization created first, admin of the second.

    This is the one direction in which the reorder turns a previously-refused
    request into a served one. The old fallback (``order_by("created")``, the
    membership created first) answers ``older_organization`` -- a plain
    membership -- so the old ordering's gate refused a request naming
    ``newer_organization``, even though the caller administers it.
    """
    OrganizationMembership.objects.create(
        user=user,
        organization=older_organization,
        is_active=True,
    )
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=newer_organization,
            is_active=True,
        ),
        [GROUP_ORGANIZATION_ADMIN],
    )
    return user


@pytest.mark.django_db
class TestTheAdminGateFollowsTheHeader:
    def test_a_request_naming_the_organization_they_only_belong_to_is_refused(
        self, admin_here_member_there: Any, newer_organization: Organization
    ) -> None:
        response = _dispatch(
            AdminGatedProbeView, admin_here_member_there, str(newer_organization.pk)
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert AdminGatedProbeView.organization_seen_by_the_body is None, (
            "the handler must not have run"
        )

    def test_the_same_request_naming_the_organization_they_administer_is_admitted(
        self, admin_here_member_there: Any, older_organization: Organization
    ) -> None:
        """The other half: the fix narrows to the named organization, it does not
        simply refuse multi-organization callers."""
        response = _dispatch(
            AdminGatedProbeView, admin_here_member_there, str(older_organization.pk)
        )

        assert response.status_code == status.HTTP_200_OK
        assert AdminGatedProbeView.organization_seen_by_the_body == older_organization.pk

    def test_the_gate_and_the_body_agree_on_which_organization_it_is(
        self, user: Any, older_organization: Organization, newer_organization: Organization
    ) -> None:
        """Admin of *both*, so the gate admits either -- and the body must then
        act on the one the header named, not on the older membership.

        This is the half of the defect that survives a gate that says yes: the
        two used to be able to disagree, and only the pair of them being read
        off the same resolution makes that impossible.
        """
        for organization in (older_organization, newer_organization):
            grant_membership_groups(
                OrganizationMembership.objects.create(
                    user=user,
                    organization=organization,
                    is_active=True,
                ),
                [GROUP_ORGANIZATION_ADMIN],
            )

        response = _dispatch(AdminGatedProbeView, user, str(newer_organization.pk))

        assert response.status_code == status.HTTP_200_OK
        assert AdminGatedProbeView.organization_seen_by_the_body == newer_organization.pk

    def test_a_plain_member_of_their_oldest_organization_who_administers_the_named_one_is_admitted(
        self, member_here_admin_there: Any, newer_organization: Organization
    ) -> None:
        """The admit direction, and the only one of the two ordering-fix flips
        that admits a served request the old ordering refused. The caller is a plain member
        of the organization the old fallback would have answered
        (``older_organization``) and admin of the organization the header
        names (``newer_organization``). The old ordering read the fallback --
        a non-admin membership -- and returned 403; the reorder reads the
        resolved membership and returns 200.
        """
        response = _dispatch(
            AdminGatedProbeView, member_here_admin_there, str(newer_organization.pk)
        )

        assert response.status_code == status.HTTP_200_OK
        assert AdminGatedProbeView.organization_seen_by_the_body == newer_organization.pk


@pytest.mark.django_db
class TestRestoringTheOldOrderingReopensIt:
    """Proof that the class above is not vacuous.

    The mutant differs from ``AdminGatedProbeView`` in nothing but *when*
    resolution happens. If it did not admit the request the class above refuses,
    that refusal would be coming from somewhere other than the reorder -- a
    missing membership, a role that is not admin, an unrelated permission.
    """

    def test_the_old_resolve_after_check_permissions_ordering_admits_the_request_the_fix_refuses(
        self, admin_here_member_there: Any, newer_organization: Organization
    ) -> None:
        response = _dispatch(
            OldOrderingAdminGatedProbeView, admin_here_member_there, str(newer_organization.pk)
        )

        assert response.status_code == status.HTTP_200_OK
        # And the body it admitted acted on the organization the caller is only
        # a plain member of -- the gate said yes about the *other* one.
        assert OldOrderingAdminGatedProbeView.organization_seen_by_the_body == newer_organization.pk

    def test_the_old_resolve_after_check_permissions_ordering_refuses_the_admit_direction_too(
        self, member_here_admin_there: Any, newer_organization: Organization
    ) -> None:
        """The admit-direction mirror. If this did not refuse the request
        ``test_a_plain_member_of_their_oldest_organization_who_administers_the_named_one_is_admitted``
        admits, that admission would be coming from somewhere other than the
        reorder.
        """
        response = _dispatch(
            OldOrderingAdminGatedProbeView, member_here_admin_there, str(newer_organization.pk)
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert OldOrderingAdminGatedProbeView.organization_seen_by_the_body is None
