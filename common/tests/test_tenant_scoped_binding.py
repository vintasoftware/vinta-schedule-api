"""``TenantScopedViewMixin``: the resolution table, and the binding lifecycle.

Two contracts live here, and they are tested together because the second is only
meaningful given the first.

**The resolution table** (unchanged by Phase 2b -- 0 / 1 / 2+ active memberships
crossed with header absent / present-and-matching / present-and-not-a-member,
plus the two opt-outs) is restated here rather than deferred to
``organizations/tests/test_org_resolution.py``, which covers the same table
through real endpoints. The difference is what each can see: those tests observe
the *response*, these observe what the view body ran with -- so a row of the
table that resolved correctly but bound the wrong organization (or bound one at
all, on a gated row) fails here and nowhere else.

**The binding lifecycle.** The mixin binds the resolved organization for the
duration of ``dispatch`` and releases it in a ``finally``. A binding that
outlived the request would be read by the *next* request the worker serves --
a WSGI worker thread reuses its context -- so every organization-scoped model's
default manager would answer with the previous caller's organization. Four exit
paths are exercised, and each asserts nothing is bound afterwards:

1. a normal 200,
2. an exception DRF renders itself (``APIException`` -> 404 response),
3. an exception DRF re-raises (``RuntimeError``, which never reaches
   ``finalize_response``),
4. a raise from ``initial()`` -- both the 400/403 rows of the table, which raise
   *before* the bind, and a subclass that raises *after* it, which is the one
   that would actually leak.

**Why these tests dispatch the view directly** rather than through
``APIClient``. ``PublicApiSystemUserMiddleware`` now wraps every request in an
``organization_context(...)`` of its own, whose ``__exit__`` would reset a
binding this mixin leaked -- so a leak would be invisible through the full
stack. Calling ``View.as_view()(request)`` runs the whole of ``dispatch``
(``initial`` -> handler -> ``finalize_response`` / ``handle_exception``) with
nothing above it to clean up after the mixin.

**This file was mutation-tested**: with the ``finally`` in
``TenantScopedViewMixin.dispatch`` removed, ``TestNoBindingSurvivesTheResponse``
fails on the ``RuntimeError`` and ``raises-after-binding`` paths (the ones DRF
does not funnel through ``finalize_response``), which is exactly the pair a
placement in ``finalize_response`` would have missed.
"""

from typing import Any

from django.contrib.auth import get_user_model

import pytest
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from common.organization_context import get_current_organization, organization_context
from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization, OrganizationMembership


User = get_user_model()


class _RecordingMixin:
    """Capture what was bound while the view body ran."""

    #: Set by the handler on every dispatch, so a test can distinguish "the body
    #: saw no organization" from "the body never ran".
    observed: Any = None
    ran: bool = False

    def _observe(self) -> None:
        type(self).ran = True
        organization = get_current_organization()
        type(self).observed = None if organization is None else organization.pk


class ProbeView(_RecordingMixin, TenantScopedViewMixin, APIView):
    """Answers 200 and records the organization bound during the handler."""

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._observe()
        return Response({"ok": True})


class OptionalProbeView(ProbeView):
    """``ProbeView`` with the class-level opt-out from the 400 and the 403."""

    active_org_resolution_optional = True


class ProbeViewSet(_RecordingMixin, TenantScopedViewMixin, ViewSet):
    """Two actions, one of which is listed in ``active_org_optional_actions``."""

    active_org_optional_actions = ("lenient",)

    def strict(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._observe()
        return Response({"ok": True})

    def lenient(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._observe()
        return Response({"ok": True})


class AnonymousProbeView(ProbeView):
    """``ProbeView`` reachable without authentication.

    The project default is ``IsAuthenticated``, which would answer 401 before
    ``_resolve_active_organization`` ever runs -- and the row being pinned here
    is what the *resolver* does with an anonymous request, not what the
    permission stack does.
    """

    # ``type: ignore[assignment]``: DRF annotates this as a ``list``.
    authentication_classes = ()  # type: ignore[assignment]
    permission_classes = (AllowAny,)


class ApiExceptionView(ProbeView):
    """Raises an exception DRF renders into a response of its own."""

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._observe()
        raise NotFound("nope")


class UnhandledExceptionView(ProbeView):
    """Raises an exception DRF re-raises out of ``dispatch``.

    ``handle_exception`` finds no response for a bare ``RuntimeError`` and calls
    ``raise_uncaught_exception``, so ``finalize_response`` never runs -- the path
    an unbind placed there would miss.
    """

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        self._observe()
        raise RuntimeError("boom")


class RaisesAfterBindingView(ProbeView):
    """Raises from ``initial()`` *after* the organization has been bound.

    The 400/403 rows of the resolution table raise before the bind, so they
    cannot leak whatever the unbind does. This one can.
    """

    def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        assert get_current_organization() is not None  # noqa: S101 -- guards the test itself
        raise RuntimeError("boom after binding")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dispatch(view_class: Any, user: Any, header: str | None = None, **initkwargs: Any) -> Any:
    """Run ``view_class`` end to end, with no middleware above it."""
    view_class.ran = False
    view_class.observed = None
    extra: dict[str, Any] = {"HTTP_X_ORGANIZATION_ID": header} if header is not None else {}
    request = APIRequestFactory().get("/probe/", None, **extra)
    force_authenticate(request, user=user)
    return view_class.as_view(**initkwargs)(request)


def _membership(user: Any, organization: Organization, *, is_active: bool = True):
    return OrganizationMembership.objects.create(
        user=user, organization=organization, is_active=is_active
    )


@pytest.fixture
def org_a(db: Any) -> Organization:
    return Organization.objects.create(name="Binding Org A")


@pytest.fixture
def org_b(db: Any) -> Organization:
    return Organization.objects.create(name="Binding Org B")


# ---------------------------------------------------------------------------
# The resolution table, observed from inside the view body
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheResolutionTableStillHolds:
    def test_zero_memberships_is_gated_and_binds_nothing(self, user: Any) -> None:
        response = _dispatch(ProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert ProbeView.ran is True
        assert ProbeView.observed is None

    def test_one_membership_no_header_binds_it(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(ProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert ProbeView.observed == org_a.pk

    def test_one_membership_matching_header_binds_it(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(ProbeView, user, header=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK
        assert ProbeView.observed == org_a.pk

    def test_two_memberships_header_picks_the_named_one(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        _dispatch(ProbeView, user, header=str(org_a.pk))
        assert ProbeView.observed == org_a.pk

        _dispatch(ProbeView, user, header=str(org_b.pk))
        assert ProbeView.observed == org_b.pk

    def test_two_memberships_no_header_is_400_and_the_body_never_runs(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(ProbeView, user)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert ProbeView.ran is False

    def test_header_naming_a_non_member_organization_is_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(ProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert ProbeView.ran is False

    def test_header_naming_an_inactive_membership_is_403(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b, is_active=False)

        response = _dispatch(ProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_non_integer_header_is_treated_as_absent(
        self, user: Any, org_a: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(ProbeView, user, header="not-an-integer")

        assert response.status_code == status.HTTP_200_OK
        assert ProbeView.observed == org_a.pk

    def test_a_non_integer_header_still_ambiguous_for_a_multi_org_caller(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(ProbeView, user, header="not-an-integer")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_class_level_opt_out_suppresses_the_400_and_binds_nothing(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(OptionalProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert OptionalProbeView.observed is None

    def test_class_level_opt_out_suppresses_the_403_and_binds_nothing(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        response = _dispatch(OptionalProbeView, user, header=str(org_b.pk))

        assert response.status_code == status.HTTP_200_OK
        assert OptionalProbeView.observed is None

    def test_per_action_opt_out_applies_only_to_the_listed_action(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)
        _membership(user, org_b)

        lenient = _dispatch(ProbeViewSet, user, actions={"get": "lenient"})
        assert lenient.status_code == status.HTTP_200_OK
        assert ProbeViewSet.observed is None

        strict = _dispatch(ProbeViewSet, user, actions={"get": "strict"})
        assert strict.status_code == status.HTTP_400_BAD_REQUEST

    def test_an_unauthenticated_request_binds_nothing(self, db: Any) -> None:
        request = APIRequestFactory().get("/probe/")
        AnonymousProbeView.ran = False
        AnonymousProbeView.observed = None

        response = AnonymousProbeView.as_view()(request)

        assert response.status_code == status.HTTP_200_OK
        assert AnonymousProbeView.ran is True
        assert AnonymousProbeView.observed is None
        assert get_current_organization() is None


# ---------------------------------------------------------------------------
# The binding lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNoBindingSurvivesTheResponse:
    """Every exit path out of ``dispatch`` leaves the context as it found it."""

    def test_normal_response(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(ProbeView, user)

        assert response.status_code == status.HTTP_200_OK
        assert ProbeView.observed == org_a.pk, "the body must have run bound"
        assert get_current_organization() is None

    def test_drf_handled_exception(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        response = _dispatch(ApiExceptionView, user)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert ApiExceptionView.observed == org_a.pk
        assert get_current_organization() is None

    def test_unhandled_exception(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        with pytest.raises(RuntimeError, match="boom"):
            _dispatch(UnhandledExceptionView, user)

        assert UnhandledExceptionView.observed == org_a.pk
        assert get_current_organization() is None

    def test_a_raise_from_initial_after_the_binding(self, user: Any, org_a: Organization) -> None:
        _membership(user, org_a)

        with pytest.raises(RuntimeError, match="boom after binding"):
            _dispatch(RaisesAfterBindingView, user)

        assert RaisesAfterBindingView.ran is False, "the handler must not have run"
        assert get_current_organization() is None

    def test_a_raise_from_initial_before_the_binding(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        """The 400 row -- nothing was bound, and nothing is bound afterwards."""
        _membership(user, org_a)
        _membership(user, org_b)

        response = _dispatch(ProbeView, user)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert get_current_organization() is None


@pytest.mark.django_db
class TestTheBindingRestoresRatherThanClears:
    """A request dispatched inside an outer binding leaves that binding intact.

    ``reset``, not ``clear``: a Celery task or a test that wrapped a client call
    in ``organization_context(...)`` must still be bound after the call returns.
    """

    def test_an_outer_binding_survives_a_request_that_resolved_another_organization(
        self, user: Any, org_a: Organization, org_b: Organization
    ) -> None:
        _membership(user, org_a)

        with organization_context(org_b):
            _dispatch(ProbeView, user)
            assert ProbeView.observed == org_a.pk, "the request's own organization wins inside"
            still_bound = get_current_organization()

        assert still_bound is not None
        assert still_bound.pk == org_b.pk

    def test_an_outer_binding_survives_a_gated_request(
        self, user: Any, org_b: Organization
    ) -> None:
        """A gated caller binds ``None`` -- and must not clear the outer binding either."""
        with organization_context(org_b):
            _dispatch(ProbeView, user)
            assert ProbeView.observed is None
            still_bound = get_current_organization()

        assert still_bound is not None
        assert still_bound.pk == org_b.pk
