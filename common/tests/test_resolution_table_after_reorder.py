"""The resolution table, restated against the reordered ``initial()``.

Phase 3.5 moved organization resolution from *after* the whole of
``APIView.initial`` to between ``perform_authentication`` and
``check_permissions``. Two things had to survive that move, and this file is
where each is asserted:

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
   no credentials. If the resolver were not gated on ``is_authenticated``, an
   anonymous request would be told which organizations exist (403) or that its
   caller belongs to several (400) before being told it is not logged in at all.
   ``TestUnauthenticatedIsAnsweredFirst`` sets up requests that *would* resolve
   to a 400 or a 403 if the resolver ran for anonymous users, and asserts 401.
"""

from typing import Any

from django.contrib.auth import get_user_model

import pytest
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization, OrganizationMembership


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
    active_org_resolution_optional = True


class PerActionProbeViewSet(_RecordingMixin, TenantScopedViewMixin, ViewSet):
    active_org_optional_actions = ("lenient",)

    def strict(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._record(request)
        return Response({"ok": True})

    def lenient(self, request: Any, *args: Any, **kwargs: Any) -> Response:
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

    def test_the_resolver_itself_does_nothing_for_an_anonymous_request(
        self, db: Any, org_a: Organization
    ) -> None:
        """Why the 401 wins: the resolver's whole body is behind an
        ``is_authenticated`` check, so an anonymous request reaches
        ``check_permissions`` with nothing raised and nothing resolved.

        Asserted on a view with no authentication gate at all, so the 401 is not
        what is doing the hiding.
        """
        response = _dispatch(OpenProbeView, user=None, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OpenProbeView.ran is True
        assert OpenProbeView.resolved is None
