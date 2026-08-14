"""The resolution table, restated against the reordered ``initial()``.

Phase 3.5 moved organization resolution from *after* the whole of
``APIView.initial`` to between ``perform_authentication`` and
``check_permissions``, and then handed the seam and the table themselves to
``vinta_orgs.drf.OrganizationScopedAPIViewMixin`` /
``vinta_orgs.helpers.memberships.resolve_membership_for_user``. **This file is
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
   reached at all, and for a merely *absent* one ``resolve_membership_for_user``
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
from django.core.exceptions import ValidationError as DjangoValidationError

import pytest
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from common.utils.view_utils import UNMATCHABLE_ORGANIZATION_SLUG, TenantScopedViewMixin
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permissions import IsOrganizationAdmin
from organizations.slug_validation import validate_organization_slug


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
    become a 200 (single membership) or a 400 (several) instead.
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

    def test_the_unmatchable_slug_could_never_name_a_real_organization(self) -> None:
        """The invariant the 403 above rests on.

        ``get_organization_slug`` returns a sentinel *slug* for a missing id, so
        the package's ``organization__slug=`` filter is what produces the
        refusal. If that sentinel could ever equal a stored slug, this row would
        resolve to somebody else's organization rather than refusing.

        Two independent reasons it cannot, and both are asserted: it is longer
        than the column, and it violates the slug format rule.
        """
        max_length = Organization._meta.get_field("slug").max_length

        assert max_length is not None
        assert len(UNMATCHABLE_ORGANIZATION_SLUG) > max_length
        with pytest.raises(DjangoValidationError):
            validate_organization_slug(UNMATCHABLE_ORGANIZATION_SLUG)


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
        ``resolve_membership_for_user`` returns ``None`` for an anonymous user
        before it reads a single membership, so the request reaches
        ``check_permissions`` with nothing raised and nothing resolved.

        Asserted on a view with no authentication gate at all, so the 401 is not
        what is doing the hiding.
        """
        response = _dispatch(OpenProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OpenProbeView.ran is True
        assert OpenProbeView.resolved is None
