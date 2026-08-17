"""The resolution table, restated against the reordered ``initial()``.

Phase 3.5 moved organization resolution from *after* the whole of
``APIView.initial`` to between ``perform_authentication`` and
``check_permissions``, and then handed the seam and the table themselves to
``vinta_orgs.drf.OrganizationScopedAPIViewMixin`` /
``common.organization_services.memberships.resolve_for_user``. **This file is
ours and stays ours** even though the package now implements the table: it pins
the status codes and the response bodies our clients depend on, across the
pk-to-slug translation and the exception translation -- which is exactly the
seam a package upgrade can silently move.

Two things had to survive that move, and this file is where each is asserted:

1. **Every row of the table keeps its code.** 0 / 1 / 2+ active memberships,
   crossed with header absent / present-and-matching / present-and-not-a-member
   / present-and-not-an-integer, plus the class-level and per-action opt-outs.
   ``common/tests/test_tenant_scoped_binding.py`` covers the same grid from
   inside the view body; the difference here is that these views carry the
   project's default ``IsAuthenticated`` permission class, so resolution and
   permission checking are exercised *in the new order* rather than in
   isolation.

2. **401 still precedes 400 and 403.** Resolution now runs before
   ``check_permissions``, which is the thing that answers 401 for a caller with
   no credentials. Two independent guarantees keep the 401 in front, and
   ``TestUnauthenticatedIsAnsweredFirst`` asserts both: a *bad* credential
   raises out of ``super().perform_authentication`` before the resolver is
   reached at all, and for a merely *absent* one ``memberships.resolve_for_user``
   short-circuits on ``user.is_anonymous`` rather than consulting the table. Lose
   either and an anonymous request would be told which organizations exist (403)
   or that its caller belongs to several (400) before being told it is not
   logged in.

Two rows carry more weight than the rest, because they are the ones the
pk-to-slug translation can silently get wrong. Both send a header the package's
slug lookup will not match, and the *right* answer differs between them:

* **A non-integer header** is "the caller named nothing" -- it must fall through
  to the absent-header rules (1 -> resolve, 2+ -> 400, 0 -> gated).
* **An integer naming no organization** is "the caller named something they may
  not have" -- it must be the same 403 as a header naming a real organization
  they are not a member of. Answering it as "absent" would let a
  single-membership caller quietly succeed against an organization they never
  asked for, and would turn the endpoint into an oracle for which ids exist.

``TestAHeaderNamingNoOrganizationAtAll`` and the ``non-integer`` tests scattered
through the membership-count classes are the pair that keeps those apart.
"""

from typing import Any

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

import pytest
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet
from vinta_orgs.exceptions import OrganizationAccessDeniedError
from vinta_orgs.resolution import UNRESOLVED_ORGANIZATION, OrganizationSelection

from common.organization_services import memberships
from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permissions import IsOrganizationAdmin


User = get_user_model()


class _RecordingMixin:
    """Capture the organization the body resolved, so 'gated' is distinguishable
    from 'the body never ran'."""

    resolved: Any = None
    ran: bool = False

    def _record(self, request: Any) -> None:
        type(self).ran = True
        organization = request.organization
        type(self).resolved = None if organization is None else organization.pk


class AuthenticatedProbeView(_RecordingMixin, TenantScopedViewMixin, APIView):
    """The project default (``IsAuthenticated``) plus the mixin -- i.e. what a
    tenant-scoped endpoint is, minus a queryset."""

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._record(request)
        return Response({"ok": True})


class OptOutProbeView(AuthenticatedProbeView):
    organization_resolution_optional = True


class PerActionProbeViewSet(_RecordingMixin, TenantScopedViewMixin, ViewSet):
    organization_optional_actions = ("lenient",)

    def strict(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._record(request)
        return Response({"ok": True})

    def lenient(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._record(request)
        return Response({"ok": True})


class AdminGatedResolutionProbeView(_RecordingMixin, TenantScopedViewMixin, APIView):
    """``IsOrganizationAdmin`` instead of the project default -- the shape of
    every mixin endpoint the 403-to-400 flip in Finding 3 is about."""

    permission_classes = (IsOrganizationAdmin,)

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._record(request)
        return Response({"ok": True})


class OpenProbeView(AuthenticatedProbeView):
    """Reachable without credentials -- the control for the 401 tests below.

    Same mixin, same resolver, no authentication gate: whatever the resolver
    does with an anonymous request is visible here instead of being hidden
    behind the 401.
    """

    authentication_classes = ()  # type: ignore[assignment]
    permission_classes = (AllowAny,)


class _AlwaysFailsAuthentication(BaseAuthentication):
    """A bad credential, not merely an absent one -- raises before
    ``request.user`` is ever set.

    Implements ``authenticate_header`` (as ``JWTAuthentication``, the project's
    real authentication class, does) so DRF answers **401** for the raised
    exception rather than **403**: ``APIView.handle_exception`` downgrades
    ``AuthenticationFailed`` to a 403 when no authentication class on the view
    offers a challenge header, which would otherwise make this test assert the
    wrong status for the wrong reason.
    """

    def authenticate(self, request: Any) -> Any:
        raise AuthenticationFailed("bad credentials")

    def authenticate_header(self, request: Any) -> str:
        return "Bearer"


class BadCredentialProbeView(AuthenticatedProbeView):
    """Same mixin, same resolver -- but every request fails authentication
    with a raised exception rather than resolving to ``AnonymousUser``. The
    control for ``super().perform_authentication`` raising out of the mixin's
    override before the resolver's second line ever runs."""

    authentication_classes = (_AlwaysFailsAuthentication,)


class NoSentinelProbeView(AuthenticatedProbeView):
    """``AuthenticatedProbeView`` with the "not found" sentinel reverted to ``None``.

    Not a historical curiosity -- it is the mutant for the
    ``present, no such org -> 403`` row, and it is the whole reason that row can
    be trusted. It differs from ``AuthenticatedProbeView`` in exactly one thing:
    the answer ``get_organization_slug`` gives when the header's pk names no
    organization. Everything else -- the header parse, the pk lookup, the
    package's table, the exception translation -- is shared.

    ``None`` is the answer the package reads as "the caller named nothing", so
    the mutant is precisely the mistake the sentinel exists to prevent: it must
    *serve* the request that the real view refuses.
    """

    def get_organization_slug(self, request: Request) -> OrganizationSelection:
        selection = super().get_organization_slug(request)

        return None if selection is UNRESOLVED_ORGANIZATION else selection


def _dispatch(view_class: Any, user: Any = None, header: str | None = None, **initkwargs: Any):
    view_class.ran = False
    view_class.resolved = None
    extra: dict[str, Any] = {"HTTP_X_ORGANIZATION_ID": header} if header is not None else {}
    request = APIRequestFactory().get("/probe/", None, **extra)
    if user is not None:
        force_authenticate(request, user=user)
    return view_class.as_view(**initkwargs)(request)


def _membership(user: Any, organization: Organization, *, is_active: bool = True):
    return OrganizationMembership.objects.create(
        user=user, organization=organization, is_active=is_active
    )


@pytest.fixture
def org_a(db: Any) -> Organization:
    return Organization.objects.create(name="Reorder Org A")


@pytest.fixture
def org_b(db: Any) -> Organization:
    return Organization.objects.create(name="Reorder Org B")


@pytest.mark.django_db
class TestZeroActiveMemberships:
    def test_no_header_is_gated_not_400(self, user: Any) -> None:
        response = _dispatch(AuthenticatedProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.ran is True
        assert AuthenticatedProbeView.resolved is None

    def test_a_header_naming_any_organization_is_403(self, user: Any, org_a: Organization) -> None:
        response = _dispatch(AuthenticatedProbeView, user, header=str(org_a.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert AuthenticatedProbeView.ran is False

    def test_an_inactive_membership_counts_as_none(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a, is_active=False)

        response = _dispatch(AuthenticatedProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.resolved is None

    def test_a_non_integer_header_is_treated_as_absent_too(self, user: Any) -> None:
        """The ``any | present, non-integer`` row, at the ``0`` membership
        count -- the table's note says a non-integer header falls through to
        the absent-header rule for every membership count, and 0 is the one
        the other two ``TestXActiveMembership*`` classes don't reach."""
        response = _dispatch(AuthenticatedProbeView, user, header="not-an-integer")

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.ran is True
        assert AuthenticatedProbeView.resolved is None


@pytest.mark.django_db
class TestExactlyOneActiveMembership:
    def test_no_header_resolves_it(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.resolved == org_a.pk

    def test_a_matching_header_resolves_it(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.resolved == org_a.pk

    def test_a_header_naming_another_organization_is_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_header_naming_an_inactive_membership_is_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b, is_active=False)

        response = _dispatch(AuthenticatedProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_non_integer_header_is_treated_as_absent(
        self, user: Any, org_a: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header="not-an-integer")

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.resolved == org_a.pk


@pytest.mark.django_db
class TestTwoOrMoreActiveMemberships:
    def test_no_header_is_400(self, user: Any, org_a: Organization, org_b: Organization) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(AuthenticatedProbeView, user)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"detail": "X-Organization-Id header required."}
        assert AuthenticatedProbeView.ran is False

    def test_a_non_integer_header_is_400_too(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(AuthenticatedProbeView, user, header="not-an-integer")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_matching_header_resolves_the_named_one(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        _dispatch(AuthenticatedProbeView, user, header=str(org_b.pk))
        assert AuthenticatedProbeView.resolved == org_b.pk

        _dispatch(AuthenticatedProbeView, user, header=str(org_a.pk))
        assert AuthenticatedProbeView.resolved == org_a.pk

    def test_a_header_naming_a_third_organization_is_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)
        third = Organization.objects.create(name="Reorder Org C")

        response = _dispatch(AuthenticatedProbeView, user, header=str(third.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestAHeaderNamingNoOrganizationAtAll:
    """``any | present, names no organization`` -> **403**, at every membership count.

    Refused identically to a header naming a real organization the caller is not
    an active member of, and deliberately so: three different answers for "no
    such organization" / "not your organization" / "your membership is
    deactivated" would tell an authenticated caller which ids are taken.

    This is the row the pk-to-slug translation exists for. The package resolves
    by slug and treats a missing slug as "the caller named nothing"; if
    ``get_organization_slug`` answered ``None`` here, every assertion below would
    become a 200 (single membership) or a 400 (several) instead. That claim is
    not left as prose: ``TestRevertingTheSentinelToNoneReopensIt`` below runs the
    same requests through a view that does answer ``None``, and asserts the 200
    and the 400.
    """

    @staticmethod
    def _absent_id() -> int:
        """An id no organization holds. Derived, not hardcoded, so it stays true."""
        highest = Organization.objects.order_by("-pk").values_list("pk", flat=True).first()
        return (highest or 0) + 1_000

    def test_with_zero_memberships(self, user: Any) -> None:
        response = _dispatch(AuthenticatedProbeView, user, header=str(self._absent_id()))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert AuthenticatedProbeView.ran is False

    def test_with_exactly_one_membership(self, user: Any, org_a: Organization) -> None:
        """The dangerous one. Read as an absent header, this resolves to
        organization A and answers 200 -- serving the caller's only organization
        for a request that explicitly named a different one."""
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=str(self._absent_id()))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert AuthenticatedProbeView.ran is False

    def test_with_two_memberships(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """403, not the 400 an absent header would have produced here."""
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(AuthenticatedProbeView, user, header=str(self._absent_id()))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_deleted_organization_is_the_same_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The realistic way a header comes to name a missing id: the client
        cached it and the organization went away underneath."""
        _membership(user, org_a)
        stale_id = org_b.pk
        org_b.delete()

        response = _dispatch(AuthenticatedProbeView, user, header=str(stale_id))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_opt_out_suppresses_it_like_any_other_refusal(
        self, user: Any, org_a: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(OptOutProbeView, user, header=str(self._absent_id()))

        assert response.status_code == status.HTTP_200_OK
        assert OptOutProbeView.ran is True
        assert OptOutProbeView.resolved is None


@pytest.mark.django_db
class TestUnresolvedOrganizationIsWhatMakesThatRowRefusable:
    """The invariant the 403s above rest on, asserted against the package.

    ``get_organization_slug`` answers ``UNRESOLVED_ORGANIZATION`` -- ``0.4.0``'s
    public singleton for "an identifier was supplied and matched nothing" -- for
    a pk that names no organization. It replaced a 279-character string sentinel
    this repo invented back when the only thing the resolver would accept was a
    *slug*, and which therefore had to be argued impossible to store (longer
    than ``varchar(255)``, and rejected by ``validate_organization_slug``). Those
    two arguments are gone with the string; what replaces them is stronger,
    because it pins behaviour rather than a shape:

    1. It is not ``None``, so it is distinguishable from "the caller named
       nothing" -- the distinction the whole row depends on.
    2. It is not a ``str``, so no ``Organization.slug`` -- a ``CharField``, whose
       every value is a string -- can equal it. No length or format argument is
       needed for a value of the wrong type.
    3. The package short-circuits on it *by identity*, before it reads a
       membership, and refuses. Asserted for the single-membership caller
       specifically: that is the caller for whom ``None`` would have succeeded,
       so it is the one that shows the sentinel is not merely inert.
    """

    def test_it_is_distinguishable_from_naming_nothing(self) -> None:
        assert UNRESOLVED_ORGANIZATION is not None

    def test_no_stored_slug_could_ever_equal_it(self) -> None:
        """``Organization.slug`` is a ``CharField``; the sentinel is not a string."""
        assert isinstance(Organization._meta.get_field("slug"), models.CharField)
        assert not isinstance(UNRESOLVED_ORGANIZATION, str)

    def test_the_resolver_refuses_it_for_a_caller_naming_nothing_would_have_served(
        self, user: Any, org_a: Organization
    ) -> None:
        _membership(user, org_a)

        assert memberships.resolve_for_user(user, None) is not None
        with pytest.raises(OrganizationAccessDeniedError):
            memberships.resolve_for_user(user, UNRESOLVED_ORGANIZATION)

    def test_the_resolver_reads_no_membership_row_before_refusing(
        self, user: Any, org_a: Organization
    ) -> None:
        """By identity, not by a query that happens to match nothing.

        A sentinel the resolver had to *look up* would be one schema change away
        from matching; this one is compared with ``is`` before any SQL runs.
        """
        _membership(user, org_a)

        with CaptureQueriesContext(connection) as captured:
            with pytest.raises(OrganizationAccessDeniedError):
                memberships.resolve_for_user(user, UNRESOLVED_ORGANIZATION)

        assert captured.captured_queries == []

    def test_optional_resolution_turns_it_into_gated_rather_than_refused(
        self, user: Any, org_a: Organization
    ) -> None:
        """The mechanism behind ``test_the_opt_out_suppresses_it_like_any_other_refusal``."""
        _membership(user, org_a)

        assert memberships.resolve_for_user(user, UNRESOLVED_ORGANIZATION, strict=False) is None


@pytest.mark.django_db
class TestRevertingTheSentinelToNoneReopensIt:
    """Proof that the class two above is not vacuous.

    ``NoSentinelProbeView`` differs from ``AuthenticatedProbeView`` in nothing
    but the answer given for a pk that names no organization: ``None`` instead of
    ``UNRESOLVED_ORGANIZATION``. If it did not *serve* the requests
    ``TestAHeaderNamingNoOrganizationAtAll`` refuses, those refusals would be
    coming from somewhere other than the sentinel -- a missing membership, the
    permission class, an unrelated 403 -- and the row would prove nothing.
    """

    def test_the_single_membership_caller_is_served_someone_elses_answer(
        self, user: Any, org_a: Organization
    ) -> None:
        """403 -> **200**, resolved against the organization they never named.

        The exact silent widening the sentinel exists to prevent: the header
        named an organization that does not exist, and the caller is handed
        their only one instead.
        """
        _membership(user, org_a)
        absent_id = TestAHeaderNamingNoOrganizationAtAll._absent_id()

        response = _dispatch(NoSentinelProbeView, user, header=str(absent_id))

        assert response.status_code == status.HTTP_200_OK
        assert NoSentinelProbeView.ran is True
        assert NoSentinelProbeView.resolved == org_a.pk

    def test_the_multi_membership_caller_is_downgraded_to_the_ambiguity_400(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """403 -> **400**. A different code and a different body, both wrong."""
        _membership(user, org_a)
        _membership(user, org_b)
        absent_id = TestAHeaderNamingNoOrganizationAtAll._absent_id()

        response = _dispatch(NoSentinelProbeView, user, header=str(absent_id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"detail": "X-Organization-Id header required."}

    def test_the_zero_membership_caller_is_downgraded_to_gated(self, user: Any) -> None:
        """403 -> **200**, gated. Still an oracle: the refusal disappears."""
        response = _dispatch(
            NoSentinelProbeView, user, header=str(TestAHeaderNamingNoOrganizationAtAll._absent_id())
        )

        assert response.status_code == status.HTTP_200_OK
        assert NoSentinelProbeView.ran is True
        assert NoSentinelProbeView.resolved is None

    def test_the_mutant_still_refuses_a_real_organization_it_should(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The control on the mutant itself.

        It changes *only* the not-found answer. A header naming a real
        organization the caller does not belong to is still a 403 through it --
        so the three 200/400 results above are attributable to the sentinel and
        to nothing else the subclass did.
        """
        _membership(user, org_a)

        response = _dispatch(NoSentinelProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestTheRefusalBodiesAreOursNotThePackages:
    """The bodies, not merely the codes.

    ``vinta_orgs.drf`` translates ``AmbiguousOrganizationError`` into a DRF
    ``ValidationError`` and ``OrganizationAccessDeniedError`` into a DRF
    ``PermissionDenied``, each carrying *the package's* message ("Several
    organizations match this request; name one explicitly." /  "You are not an
    active member of this organization."). Our clients match on ours, so
    ``TenantScopedViewMixin.resolve_organization`` puts them back. Pinning only
    the status code would let a package upgrade rewrite the body silently.
    """

    def test_the_ambiguity_400(self, user: Any, org_a: Organization, org_b: Organization) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(AuthenticatedProbeView, user)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"detail": "X-Organization-Id header required."}

    def test_the_non_member_403(self, user: Any, org_a: Organization, org_b: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {
            "detail": (
                "X-Organization-Id header names an organization you are not an active member of."
            )
        }

    def test_the_no_such_organization_403_carries_the_same_body(
        self, user: Any, org_a: Organization
    ) -> None:
        """Identical to the row above -- which is the point: the body must not
        distinguish "does not exist" from "not yours" either."""
        _membership(user, org_a)
        absent_id = TestAHeaderNamingNoOrganizationAtAll._absent_id()

        response = _dispatch(AuthenticatedProbeView, user, header=str(absent_id))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {
            "detail": (
                "X-Organization-Id header names an organization you are not an active member of."
            )
        }


@pytest.mark.django_db
class TestTheResolverPrecedesEvenADenyingPermissionClass:
    """The other live consequence of the reorder, on every mixin endpoint whose
    permission class is not ``IsAuthenticated``: the ``2+ / absent`` row now
    raises *before* ``IsOrganizationAdmin.has_permission`` gets a chance to run
    -- so a non-admin, multi-organization caller who omits the header is
    answered the resolver's ambiguity 400, not the permission class's 403.
    Pre-3.5, ``check_permissions`` ran first and this exact request was a 403.
    """

    def test_a_non_admin_multi_organization_caller_with_no_header_is_400_not_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        OrganizationMembership.objects.create(
            user=user, organization=org_a, role=OrganizationRole.MEMBER, is_active=True
        )
        OrganizationMembership.objects.create(
            user=user, organization=org_b, role=OrganizationRole.MEMBER, is_active=True
        )

        response = _dispatch(AdminGatedResolutionProbeView, user)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert AdminGatedResolutionProbeView.ran is False


@pytest.mark.django_db
class TestTheOptOuts:
    def test_the_class_level_opt_out_suppresses_the_400(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(OptOutProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert OptOutProbeView.resolved is None

    def test_the_class_level_opt_out_suppresses_the_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(OptOutProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OptOutProbeView.resolved is None

    def test_the_class_level_opt_out_with_zero_memberships_is_still_gated(
        self, user: Any, org_a: Organization
    ) -> None:
        """The opt-out with no memberships at all. Without it this exact
        request is the 403 row (``TestZeroActiveMemberships
        .test_a_header_naming_any_organization_is_403``); the opt-out
        suppresses that too, resolving to gated rather than refusing."""
        response = _dispatch(OptOutProbeView, user, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OptOutProbeView.ran is True
        assert OptOutProbeView.resolved is None

    def test_the_class_level_opt_out_still_resolves_a_matching_header(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The opt-out only suppresses the 400/403 rows -- it does not gate a
        request that would have resolved cleanly anyway. A 2+ membership
        caller whose header names an org they actually belong to still
        resolves to it, not to ``None``."""
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(OptOutProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OptOutProbeView.resolved == org_b.pk

    def test_the_per_action_opt_out_applies_to_the_listed_action_only(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        lenient = _dispatch(PerActionProbeViewSet, user, actions={"get": "lenient"})
        assert lenient.status_code == status.HTTP_200_OK
        assert PerActionProbeViewSet.resolved is None

        strict = _dispatch(PerActionProbeViewSet, user, actions={"get": "strict"})
        assert strict.status_code == status.HTTP_400_BAD_REQUEST

    def test_the_per_action_opt_out_still_suppresses_the_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(
            PerActionProbeViewSet, user, header=str(org_b.pk), actions={"get": "lenient"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert PerActionProbeViewSet.resolved is None

    def test_the_per_action_opt_out_also_suppresses_the_400_for_a_non_integer_header(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """A non-integer header falls through to the absent-header rule (see
        ``TestTwoOrMoreActiveMemberships.test_a_non_integer_header_is_400_too``),
        so the opted-out action must suppress that 400 exactly as it suppresses
        the literal absent-header one."""
        _membership(user, org_a)
        _membership(user, org_b)

        lenient = _dispatch(
            PerActionProbeViewSet, user, header="not-an-integer", actions={"get": "lenient"}
        )

        assert lenient.status_code == status.HTTP_200_OK
        assert PerActionProbeViewSet.resolved is None


@pytest.mark.django_db
class TestUnauthenticatedIsAnsweredFirst:
    """401 before 400 and 403.

    Each test sends a request an *authenticated* caller would have been given a
    400 or a 403 for, without credentials. The organizations exist and the
    memberships exist; only the credential is missing. Answering anything but
    401 would tell an anonymous caller something about them.
    """

    def test_a_header_naming_a_real_organization_is_401_not_403(
        self, db: Any, org_a: Organization
    ) -> None:
        response = _dispatch(AuthenticatedProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_no_header_is_401_not_400_even_for_an_ambiguous_user(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The user this request *would* have been resolved for has two active
        memberships -- the 400 row of the table. Anonymously, it is a 401."""
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(AuthenticatedProbeView, user=None)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_the_authenticated_versions_of_those_two_requests_are_403_and_400(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The control. Without it, the two tests above would pass on a view
        that answered 401 to everything."""
        _membership(user, org_a)
        _membership(user, org_b)
        third = Organization.objects.create(name="Reorder Org C")

        forbidden = _dispatch(AuthenticatedProbeView, user, header=str(third.pk))
        ambiguous = _dispatch(AuthenticatedProbeView, user)

        assert forbidden.status_code == status.HTTP_403_FORBIDDEN
        assert ambiguous.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_bad_credential_is_401_before_the_resolver_ever_runs(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The other branch off ``super().perform_authentication``: a *raised*
        ``AuthenticationFailed`` (a bad credential, not merely a missing one)
        must also reach the caller as 401 before the resolver's ambiguous-header
        400 gets a chance to run. ``user`` has the two active memberships that
        make the 400 row live -- proof that they were never consulted, not that
        there was nothing to be ambiguous about.
        """
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(BadCredentialProbeView, user=None)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_the_resolver_itself_does_nothing_for_an_anonymous_request(
        self, db: Any, org_a: Organization
    ) -> None:
        """Why the 401 wins for an *absent* credential:
        ``memberships.resolve_for_user`` returns ``None`` for an anonymous user
        before it reads a single membership, so the request reaches
        ``check_permissions`` with nothing raised and nothing resolved.

        Asserted on a view with no authentication gate at all, so the 401 is not
        what is doing the hiding.
        """
        response = _dispatch(OpenProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OpenProbeView.ran is True
        assert OpenProbeView.resolved is None

    def test_an_anonymous_header_bearing_request_reads_no_organization_row(
        self, db: Any, org_a: Organization
    ) -> None:
        """...and it does not pay for a query to do nothing.

        The package evaluates ``get_organization_slug`` **eagerly**, as an
        argument to ``memberships.resolve_for_user``, so it runs *before* that
        function's ``is_anonymous`` short-circuit. Without the guard at the top
        of our override, every anonymous request carrying the header would spend
        an ``Organization`` lookup translating a pk the resolver was then going
        to ignore -- and, since resolution also precedes ``check_throttles``,
        would spend it before any throttle bucket was consulted. An
        unauthenticated pre-throttle database round trip is exactly what a
        request flood wants.

        Asserted on the view with no authentication classes so the count is the
        resolver's alone; ``test_no_organization_row_is_read_before_the_401``
        below makes the same point on the real 401 path, where the count is not
        purely ours.
        """
        with CaptureQueriesContext(connection) as captured:
            response = _dispatch(OpenProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert len(captured.captured_queries) == 0, captured.captured_queries

    def test_no_organization_row_is_read_before_the_401(self, db: Any, org_a: Organization) -> None:
        """The same claim on the path a real caller takes: 401, and no org read.

        ``AuthenticatedProbeView`` carries the project's authentication and
        permission classes, so a raw query count here would also cover whatever
        they do. Assert on the table instead: no statement touching
        ``Organization`` may run for a request that is about to be told it is not
        logged in.
        """
        organization_table = Organization._meta.db_table

        with CaptureQueriesContext(connection) as captured:
            response = _dispatch(AuthenticatedProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        touching_organizations = [
            query["sql"]
            for query in captured.captured_queries
            if organization_table in query["sql"]
        ]
        assert touching_organizations == []


@pytest.mark.django_db
class TestAHeaderTooWideForThePrimaryKey:
    """``any | present, integer too wide for the pk column`` -> **403**, never a 500.

    The header is caller-controlled and ``get_organization_slug`` hands whatever
    ``int()`` accepts straight to ``filter(pk=...)``. ``Organization.pk`` is a
    ``BigAutoField``, so an integer past ``bigint`` is a value the column
    provably cannot hold -- and the failure mode worth pinning is that the
    database is asked anyway. Postgres answers ``NumericValueOutOfRange`` when a
    parameter is *coerced* to an integer type it overflows, which would surface
    as a **500** on all ~26 endpoints carrying this mixin, reachable by editing
    one character of a header.

    It does not happen here, and these tests are what keeps that true rather than
    incidental: psycopg 3 adapts a Python ``int`` wider than ``bigint`` as
    ``numeric``, and ``bigint = numeric`` is a legal comparison that simply
    matches nothing. So the value falls through to the ordinary "names no
    organization" road -- ``UNRESOLVED_ORGANIZATION``, and the same 403
    as any unused id. A narrowing of the column, a change of driver, or a
    hand-written cast could each put the 500 back silently; hence the pin rather
    than a range check in the resolver, which would guard nothing today.
    """

    @pytest.mark.parametrize(
        "header",
        [
            str(2**63),  # one past ``bigint``
            str(2**64),
            "9" * 40,
            "9" * 4000,  # just under CPython's ``int()`` digit limit
        ],
    )
    def test_an_integer_too_wide_for_the_column_is_the_same_403(
        self, user: Any, org_a: Organization, header: str
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=header)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {
            "detail": (
                "X-Organization-Id header names an organization you are not an active member of."
            )
        }
        assert AuthenticatedProbeView.ran is False

    @pytest.mark.parametrize("header", ["0", "-1", str(-(2**64))])
    def test_zero_and_negatives_are_the_same_403_too(
        self, user: Any, org_a: Organization, header: str
    ) -> None:
        """A sequence-backed primary key is never <= 0, so these name no
        organization -- including the ones too negative for the column."""
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header=header)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_header_past_the_int_parsing_limit_is_read_as_absent(
        self, user: Any, org_a: Organization
    ) -> None:
        """The upper bound on the input is ``int()``'s, not the resolver's.

        CPython refuses to parse a decimal string longer than
        ``sys.get_int_max_str_digits()`` (4300 by default), raising ``ValueError``
        -- which ``get_organization_slug`` already handles as "the caller named
        nothing". That is what stops a caller from making the process do
        unbounded integer parsing, so no length check of our own is needed; this
        test is what says so. Note the answer differs from the row above: absent,
        not refused, so a single-membership caller resolves and gets 200.
        """
        _membership(user, org_a)

        response = _dispatch(AuthenticatedProbeView, user, header="9" * 100_000)

        assert response.status_code == status.HTTP_200_OK
        assert AuthenticatedProbeView.resolved == org_a.pk

    def test_the_primary_key_column_is_still_the_wide_one(self, db: Any) -> None:
        """The premise the class rests on, asserted rather than assumed.

        ``bigint`` is why ``2**63`` is "too wide" above and why every legitimate
        id is comfortably inside the column. Narrowing the primary key would not
        break the 403 tests -- ``integer = numeric`` compares fine too -- so
        nothing else here would notice; this is the line that would.
        """
        _, ceiling = connection.ops.integer_field_range(
            Organization._meta.pk.get_internal_type()  # type: ignore[union-attr]
        )

        assert ceiling == 2**63 - 1
